# syntax=docker/dockerfile:1
#
# MiniMax H3 worker image — ComfyUI only, NO model weights.
#
# The ~62 GiB of MiniMax H3 weights live on a RunPod network volume, populated by
# scripts/populate-volume.sh. That split exists for a hard reason: RunPod Pods
# cannot run a Docker daemon ("you can't directly build Docker containers or use
# Docker Compose on a GPU Pod" — RunPod docs), so there is nowhere on RunPod to
# assemble a 75 GiB baked image. Keeping the image small makes it buildable on a
# stock GitHub Actions runner.
#
# NOTE ON THE BASE IMAGE
# ----------------------
# BASE_IMAGE is NOT a published `runpod/worker-comfyui:*-base` tag. Every one of
# those predates the model: 5.8.6 shipped 2026-06-17, while MiniMax H3 arrived in
# ComfyUI v0.30.0 on 2026-08-03. .github/workflows/build.yml therefore builds
# worker-comfyui's `base` target from a pinned upstream SHA with
# COMFYUI_VERSION=0.30.0, and this Dockerfile stacks on that.
ARG BASE_IMAGE=worker-comfyui-base:comfy-0.30.0
FROM ${BASE_IMAGE}

# Fail loudly if BASE_IMAGE was built against a ComfyUI that predates MiniMax H3.
RUN test -f /comfyui/comfy_extras/nodes_minimax_h3.py \
    || (echo "FATAL: base image has no comfy_extras/nodes_minimax_h3.py — its ComfyUI is older than v0.30.0." >&2 && exit 1)

# Upstream's src/extra_model_paths.yaml maps only the legacy `unet:` and `clip:`
# keys. ComfyUI's folder_paths.map_legacy() rewrites those key NAMES to
# diffusion_models/text_encoders, but the PATHS it registers stay models/unet/
# and models/clip/ — so a volume laid out to mirror the Hugging Face repo would
# silently not be found, and UNETLoader would show an empty dropdown. Our version
# adds the two explicit keys.
COPY extra_model_paths.yaml /comfyui/extra_model_paths.yaml

# Let BUCKET_NAME choose the S3 bucket.
#
# worker-comfyui calls rp_upload.upload_image(job_id, path) with no bucket, and the
# RunPod SDK then falls back to `time.strftime("%m-%y")` — so it writes to a bucket
# literally named "08-26" and silently starts looking for "09-26" on the 1st of next
# month. There is no BUCKET_NAME support in the SDK, so patch the call site: the env
# var wins when set, and an unset value passes None, preserving upstream behaviour.
#
# The build fails loudly if upstream ever changes that line, rather than shipping an
# image where S3 quietly reverts to date-named buckets.
RUN sed -i \
      's|rp_upload.upload_image(job_id, temp_file_path)|rp_upload.upload_image(job_id, temp_file_path, bucket_name=os.environ.get("BUCKET_NAME") or None)|' \
      /handler.py \
    && grep -q 'bucket_name=os.environ.get("BUCKET_NAME")' /handler.py \
    || (echo "FATAL: BUCKET_NAME patch did not apply — upstream handler.py changed" >&2 && exit 1)

# Sanity-check the patched file still parses.
RUN python -c "import ast,sys; ast.parse(open('/handler.py').read())" \
    && echo "handler.py patched and parses"

# ---------------------------------------------------------------------------
# COLD-START PROBE (0.3.0) — bake the two VAEs, 5.41 GiB.
#
# The point is measurement, not the saving itself. A cold start is ~43 s of image
# load plus ~250 s staging weights off the network volume. Whether baking the full
# 30.69 GiB shared stack (text encoder + VAEs) is worth it turns on a number that
# has never been measured: does cached image load time scale with image size?
#
#   22.7 GiB image -> "Loaded image" in 41-44 s (three observations, 2026-08-09)
#
# If 28.1 GiB still loads in ~43 s, image load is mostly fixed cost and baking the
# rest is a clear win. If it rises to ~53 s, extraction is linear at ~530 MB/s and
# baking roughly breaks even — the bytes saved off the volume (264 MB/s) are paid
# back on any host that has to pull the image from GHCR (~69 MB/s).
#
# These copies win over the ones on the volume: extra_model_paths.yaml adds its
# paths with is_default=false, which APPENDS to the search list, and
# folder_paths.get_full_path() returns the first match — so /comfyui/models/vae is
# consulted before /runpod-volume/models/vae.
RUN python <<'PY'
import os, urllib.request

BASE = "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae"
# Exact sizes from the HF API. A truncated download otherwise surfaces as a
# baffling safetensors parse error on a live worker; fail the build instead.
WANT = {
    "minimax_h3_video_vae_fp16.safetensors": 5207808496,
    "minimax_h3_audio_vae_fp32.safetensors": 605254808,
}
os.makedirs("/comfyui/models/vae", exist_ok=True)
for name, size in WANT.items():
    dest = f"/comfyui/models/vae/{name}"
    urllib.request.urlretrieve(f"{BASE}/{name}", dest)
    got = os.path.getsize(dest)
    if got != size:
        raise SystemExit(f"FATAL {name}: got {got} bytes, want {size}")
    print(f"OK {name} {got}")
PY
