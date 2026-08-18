#!/usr/bin/env python3
"""Structurally validate the API-format workflows in ../workflows.

Catches the mistakes that otherwise only surface on a live GPU worker after a
long cold start: renamed model files, dangling node references, a `length` that
is off MiniMax H3's 17k+5 frame grid, or a dimension that is not a multiple of
32. Run it after editing any workflow.

    python scripts/validate-workflows.py

Exits non-zero on the first failing file, so it can gate CI.
"""
from __future__ import annotations

import glob
import json
import os
import sys

# Required inputs per node, taken from the ComfyUI v0.30.0 schemas.
REQUIRED: dict[str, set[str]] = {
    "UNETLoader": {"unet_name", "weight_dtype"},
    "CLIPLoader": {"clip_name", "type"},
    "VAELoader": {"vae_name"},
    "LoadImage": {"image"},
    "LoadAudio": {"audio"},
    "MiniMaxH3ImageToVideo": {"clip", "vae", "prompt", "width", "height", "length"},
    "MiniMaxH3ReferenceToVideo": {"clip", "vae", "audio_vae", "prompt", "width", "height", "length"},
    "MiniMaxH3SigmaShift": {"model", "video_shift", "audio_shift"},
    "BasicGuider": {"model", "conditioning"},
    "EasyCache": {"model", "reuse_threshold", "start_percent", "end_percent"},
    "LazyCache": {"model", "reuse_threshold", "start_percent", "end_percent"},
    "LoraLoaderModelOnly": {"model", "lora_name", "strength_model"},
    "KSamplerSelect": {"sampler_name"},
    "BasicScheduler": {"model", "scheduler", "steps", "denoise"},
    "RandomNoise": {"noise_seed"},
    "SamplerCustomAdvanced": {"noise", "guider", "sampler", "sigmas", "latent_image"},
    "VAEDecode": {"samples", "vae"},
    "VAEDecodeAudio": {"samples", "vae"},
    "CreateVideo": {"images", "fps"},
    "SaveVideo": {"video", "filename_prefix", "format", "codec"},
    "ImageScale": {"image", "upscale_method", "width", "height", "crop"},
    "UpscaleModelLoader": {"model_name"},
    "ImageUpscaleWithModel": {"upscale_model", "image"},
}

# H3's native canvas is a 768 px short edge, capped at 768x1344 (1.03 MP) — see
# DEPLOYMENT.md. Exceeding it is not an error (the node accepts up to
# MAX_RESOLUTION) but it puts the sampler outside the trained distribution, which
# is the same failure mode as the 362-frame drift. Warn, don't fail.
NATIVE_SHORT_EDGE = 768
NATIVE_MAX_PIXELS = 768 * 1344

# Exactly what scripts/populate-volume.sh puts on the network volume. A workflow
# naming anything else shows up as an empty loader dropdown on the worker.
ON_VOLUME = {
    "minimax_h3_fl2va_int8_convrot.safetensors",
    "minimax_h3_ref2va_int8_convrot.safetensors",
    "qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
    "minimax_h3_video_vae_fp16.safetensors",
    "minimax_h3_audio_vae_fp32.safetensors",
    "RealESRGAN_x2plus.pth",
    # NOTE: the Turbo LoRA was on the deleted CA-MTL-3 volume and is NOT on the
    # EU-RO-1 volume. It was rejected for noise, so it is not re-downloaded;
    # minimax_h3_i2v_portrait_turbo_api.json will fail until it is.
    #
    # Two files are DELIBERATELY absent from this set so the validator rejects any
    # workflow that reaches for them again. Both were measured and rejected:
    #
    #   minimax_h3_fl2va_pruned_int8_convrot.safetensors
    #       +66% Laplacian energy and +41% bitrate from an identical seed - noise,
    #       not detail. Deleted from the volume 2026-08-09.
    #   qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors
    #       Still ON the volume, but OOMs at 640x1120 where INT8 completes: it
    #       loads lighter (42.85 vs ~48 GiB) and then peaks higher (55.39 GiB).
}

# Ref2VA's reference inputs are Autogrow TemplatePrefix. Two things bite here:
#
#   1. The API key is DOT-PREFIXED by the parent input id. parse_class_inputs()
#      extends the prefix with the Autogrow input's own id, then
#      finalize_prefix() joins with ".", so the wire name is
#      "ref_images.ref_image_0" — NOT "ref_image_0". Passing the bare name fails
#      at runtime with "execute() got an unexpected keyword argument". The same
#      shape shows up in stock templates as ComfyMathExpression's "values.a".
#   2. The generated indices are ZERO-based (`f"{prefix}{i}" for i in range(max)`),
#      while the prompt tags are ONE-based: <Picture 1> refers to ref_image_0.
AUTOGROW_LIMITS = {
    "ref_images.ref_image_": 9,
    "ref_videos.ref_video_": 3,
    "ref_video_audios.ref_video_audio_": 3,
    "ref_audios.ref_audio_": 3,
}

# Bare (un-prefixed) forms are silently accepted by the JSON but rejected by the
# node at execution time, so flag them explicitly.
AUTOGROW_BARE = {
    "ref_image_": "ref_images.ref_image_",
    "ref_video_audio_": "ref_video_audios.ref_video_audio_",
    "ref_video_": "ref_videos.ref_video_",
    "ref_audio_": "ref_audios.ref_audio_",
}


def check(path: str) -> tuple[list[str], list[str]]:
    with open(path, encoding="utf-8") as fh:
        wf = json.load(fh)
    errs: list[str] = []
    warns: list[str] = []

    for nid, node in wf.items():
        ct = node.get("class_type")
        if ct not in REQUIRED:
            errs.append(f"{nid}: unknown class_type {ct!r}")
            continue

        inputs = node.get("inputs", {})
        missing = REQUIRED[ct] - set(inputs)
        if missing:
            errs.append(f"{nid} ({ct}): missing required input(s) {sorted(missing)}")

        for key, val in inputs.items():
            if isinstance(val, list):
                if val[0] not in wf:
                    errs.append(f"{nid}.{key}: references non-existent node {val[0]!r}")
            elif isinstance(val, str) and val.endswith((".safetensors", ".pth")) and val not in ON_VOLUME:
                errs.append(f"{nid}.{key}: {val!r} is not on the network volume")

        if ct == "MiniMaxH3ReferenceToVideo":
            for key in inputs:
                # Bare name without the parent prefix — passes JSON validation but
                # blows up inside execute().
                for bare, correct in AUTOGROW_BARE.items():
                    if key.startswith(bare) and key[len(bare):].isdigit():
                        errs.append(f"{nid}.{key}: missing parent prefix, should be {correct}{key[len(bare):]!s}")
                        break
                else:
                    for prefix, limit in AUTOGROW_LIMITS.items():
                        if key.startswith(prefix) and key[len(prefix):].isdigit():
                            idx = int(key[len(prefix):])
                            if idx >= limit:
                                errs.append(f"{nid}.{key}: index {idx} exceeds max {limit - 1} for {prefix}*")
                            break

            # ref_video_audio_N is index-paired with ref_video_N by the node, so a
            # soundtrack without its video is silently ignored rather than erroring.
            va = "ref_video_audios.ref_video_audio_"
            for key in inputs:
                if key.startswith(va) and key[len(va):].isdigit():
                    n = key[len(va):]
                    if f"ref_videos.ref_video_{n}" not in inputs:
                        errs.append(f"{nid}.{key}: no matching ref_videos.ref_video_{n}; it will be ignored")

        if ct in ("MiniMaxH3ImageToVideo", "MiniMaxH3ReferenceToVideo"):
            length = inputs.get("length")
            if isinstance(length, int) and (length < 5 or (length - 5) % 17):
                errs.append(
                    f"{nid}: length {length} is off the 17k+5 grid "
                    f"(nearest valid: {5 + 17 * round((length - 5) / 17)})"
                )
            for dim in ("width", "height"):
                v = inputs.get(dim)
                if isinstance(v, int) and v % 32:
                    errs.append(f"{nid}: {dim} {v} is not a multiple of 32")

            w, h = inputs.get("width"), inputs.get("height")
            if isinstance(w, int) and isinstance(h, int):
                if min(w, h) > NATIVE_SHORT_EDGE:
                    warns.append(
                        f"{nid}: short edge {min(w, h)} exceeds H3's native {NATIVE_SHORT_EDGE} px "
                        f"- outside the trained canvas; upscale a native render instead"
                    )
                elif w * h > NATIVE_MAX_PIXELS:
                    warns.append(
                        f"{nid}: {w}x{h} is {w * h / 1e6:.2f} MP, above the native "
                        f"{NATIVE_MAX_PIXELS / 1e6:.2f} MP cap (768x1344)"
                    )

    # Run A saves the native master alongside the 1080p deliverable, so more than
    # one SaveVideo is legitimate. Zero is not — the handler would return nothing.
    saves = [n for n, x in wf.items() if x.get("class_type") == "SaveVideo"]
    if not saves:
        errs.append("no SaveVideo node - the job would produce no output")

    return errs, warns


def main() -> int:
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
    paths = sorted(glob.glob(os.path.join(root, "workflows", "*.json")))
    if not paths:
        print("no workflows found", file=sys.stderr)
        return 1

    failed = False
    for path in paths:
        errs, warns = check(path)
        name = os.path.basename(path)
        if errs:
            failed = True
            print(f"FAIL {name}")
            for e in errs:
                print(f"   - {e}")
        else:
            print(f"PASS {name}")
        for w in warns:
            print(f"   ! {w}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
