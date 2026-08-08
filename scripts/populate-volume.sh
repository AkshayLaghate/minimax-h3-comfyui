#!/usr/bin/env bash
#
# Download the MiniMax H3 weights onto a RunPod network volume.
#
# Run this ON A RUNPOD POD that has the network volume attached. No Docker is
# involved — RunPod Pods cannot run a Docker daemon, which is exactly why the
# weights live on a volume instead of inside the image.
#
#   bash populate-volume.sh
#
# The volume mounts at /workspace on a Pod and at /runpod-volume on a serverless
# worker. Only the mount point differs; the layout below is what
# extra_model_paths.yaml expects in both cases.
#
set -euo pipefail

VOLUME="${VOLUME:-/workspace}"
REPO="Comfy-Org/MiniMax-H3"
MODELS="${VOLUME}/models"

# Mirrors the Hugging Face repo layout exactly, so `--local-dir "$MODELS"` places
# every file where ComfyUI expects it with no renaming.
# FL2VA drives T2V / I2V / first-last-frame. Ref2VA is a separate checkpoint for
# reference conditioning — it is the only one that accepts reference AUDIO.
# Set REF2VA=0 to skip it and save 31.70 GiB.
FILES=(
  "vae/minimax_h3_audio_vae_fp32.safetensors"
  "vae/minimax_h3_video_vae_fp16.safetensors"
  "text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors"
  "diffusion_models/minimax_h3_fl2va_int8_convrot.safetensors"
)
if [ "${REF2VA:-1}" = "1" ]; then
  FILES+=("diffusion_models/minimax_h3_ref2va_int8_convrot.safetensors")
fi

# Exact byte sizes from the HF API. A truncated download otherwise surfaces as a
# baffling safetensors parse error on the first inference.
declare -A SIZES=(
  ["vae/minimax_h3_audio_vae_fp32.safetensors"]=605254808
  ["vae/minimax_h3_video_vae_fp16.safetensors"]=5207808496
  ["text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors"]=27141342152
  ["diffusion_models/minimax_h3_fl2va_int8_convrot.safetensors"]=34038892334
  ["diffusion_models/minimax_h3_ref2va_int8_convrot.safetensors"]=34038894550
)

if [ ! -d "$VOLUME" ]; then
  echo "FATAL: $VOLUME does not exist — is the network volume attached to this Pod?" >&2
  exit 1
fi

echo "==> target: $MODELS"
df -h "$VOLUME" | tail -1

# 62.4 GiB without Ref2VA, 94.1 GiB with it. Leave headroom.
need_gb=70
[ "${REF2VA:-1}" = "1" ] && need_gb=105
avail_gb=$(df -BG --output=avail "$VOLUME" | tail -1 | tr -dc '0-9')
if [ "${avail_gb:-0}" -lt "$need_gb" ]; then
  echo "FATAL: only ${avail_gb}G free on $VOLUME; need ~${need_gb}G." >&2
  echo "       (set REF2VA=0 to skip the reference checkpoint and need ~70G)" >&2
  exit 1
fi

# hf_transfer gives multi-threaded chunked downloads, which matters when one file
# is 31 GiB. Downloads resume automatically, so a re-run after an interruption
# picks up where it stopped rather than restarting.
pip install --quiet --upgrade "huggingface_hub<1.0" hf_transfer
export HF_HUB_ENABLE_HF_TRANSFER=1

# `huggingface-cli` is deprecated in favour of `hf`, but `hf` only exists in
# newer huggingface_hub. Pick whichever this environment actually has.
if command -v hf >/dev/null 2>&1; then
  HF_DL=(hf download)
else
  HF_DL=(huggingface-cli download)
fi

mkdir -p "$MODELS"

for f in "${FILES[@]}"; do
  dest="${MODELS}/${f}"
  want="${SIZES[$f]}"
  if [ -f "$dest" ] && [ "$(stat -c%s "$dest")" = "$want" ]; then
    echo "==> already complete: $f"
    continue
  fi
  echo "==> downloading $f"
  "${HF_DL[@]}" "$REPO" "$f" --local-dir "$MODELS"
done

rm -rf "${MODELS}/.cache"

echo
echo "==> verifying sizes"
fail=0
for f in "${FILES[@]}"; do
  dest="${MODELS}/${f}"
  want="${SIZES[$f]}"
  got=$(stat -c%s "$dest" 2>/dev/null || echo 0)
  if [ "$got" = "$want" ]; then
    printf '  OK   %-62s %s\n' "$f" "$got"
  else
    printf '  FAIL %-62s got=%s want=%s\n' "$f" "$got" "$want"
    fail=1
  fi
done
[ "$fail" = 0 ] || { echo "One or more files are incomplete — re-run to resume." >&2; exit 1; }

echo
echo "==> layout:"
find "$MODELS" -name '*.safetensors' -printf '  %p (%s bytes)\n'
du -sh "$MODELS"
echo
echo "Done. Attach this volume to the serverless endpoint; it mounts at /runpod-volume."
