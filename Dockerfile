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

# SageAttention, opt-in via COMFY_EXTRA_ARGS (see start.sh) rather than baked on.
#
# Deliberately the PyPI package, which is SageAttention *1.x* — Triton-based, with
# no FP8 PV kernel. That matters: ComfyUI issue #15263 documents `--use-sage-attention`
# auto-dispatching to an FP8 PV kernel on sm_120 (our RTX PRO 6000), where accumulation
# error turns MiniMax H3 output into pure noise past ~160k tokens — silently, after a
# full sampling run. SageAttention 2.x would reintroduce that kernel and would also mean
# a third-party prebuilt wheel; 1.x sidesteps both by construction.
#
# ComfyUI wraps the sageattn call in try/except and falls back to PyTorch attention on
# error, so an unsupported arch degrades rather than crashes.
RUN uv pip install sageattention

# Upstream's start.sh hardcodes ComfyUI's arguments. Ours appends ${COMFY_EXTRA_ARGS}
# so flags can be toggled per-endpoint without rebuilding the image.
COPY start.sh /start.sh
RUN chmod +x /start.sh
