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
