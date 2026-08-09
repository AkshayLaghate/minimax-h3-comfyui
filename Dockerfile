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
# COLD-START PROBE — RAN AS 0.3.0, DISABLED. Baking weights does not pay.
#
# This stage baked the two VAEs (+5.41 GiB, 22.7 -> 28.1 GiB) to measure how cached
# image load scales with image size. Measured 2026-08-09:
#
#   22.7 GiB -> 41-44 s   (three observations)
#   28.1 GiB -> 64 s
#
#   marginal rate for the added bytes : 276 MB/s
#   network volume staging rate       : 264 MB/s     <-- the same
#
# Moving 5.41 GiB from the volume into the image turned a 22.0 s volume read into a
# 21.0 s image load: a 1 second saving. There is no faster path to move bytes onto,
# so baking buys nothing while still costing ~84 s per 5.41 GiB on any host that has
# to pull the image from GHCR (~69 MB/s). Scaled to the full 30.69 GiB shared stack:
# +5.5 s saved against a +478 s penalty.
#
# (The prior estimate of ~38 s saved per 20 GiB compared the image's AVERAGE rate,
# 567 MB/s, which amortises fixed setup across the whole image. The marginal rate is
# what baking actually buys.)
#
# Kept, disabled, so the measurement can be repeated if RunPod's storage changes.
# Note when re-enabling: these copies do take precedence over the volume's, because
# extra_model_paths.yaml adds paths with is_default=false (which APPENDS) and
# folder_paths.get_full_path() returns the first match.
#
# RUN python <<'PY'
# import os, urllib.request
# BASE = "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main/vae"
# WANT = {
#     "minimax_h3_video_vae_fp16.safetensors": 5207808496,
#     "minimax_h3_audio_vae_fp32.safetensors": 605254808,
# }
# os.makedirs("/comfyui/models/vae", exist_ok=True)
# for name, size in WANT.items():
#     dest = f"/comfyui/models/vae/{name}"
#     urllib.request.urlretrieve(f"{BASE}/{name}", dest)
#     got = os.path.getsize(dest)
#     if got != size:
#         raise SystemExit(f"FATAL {name}: got {got} bytes, want {size}")
#     print(f"OK {name} {got}")
# PY
