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
    "KSamplerSelect": {"sampler_name"},
    "BasicScheduler": {"model", "scheduler", "steps", "denoise"},
    "RandomNoise": {"noise_seed"},
    "SamplerCustomAdvanced": {"noise", "guider", "sampler", "sigmas", "latent_image"},
    "VAEDecode": {"samples", "vae"},
    "VAEDecodeAudio": {"samples", "vae"},
    "CreateVideo": {"images", "fps"},
    "SaveVideo": {"video", "filename_prefix", "format", "codec"},
}

# Exactly what scripts/populate-volume.sh puts on the network volume. A workflow
# naming anything else shows up as an empty loader dropdown on the worker.
ON_VOLUME = {
    "minimax_h3_fl2va_int8_convrot.safetensors",
    "minimax_h3_ref2va_int8_convrot.safetensors",
    "qwen3vl_32b_minimax_h3_int8_convrot.safetensors",
    "minimax_h3_video_vae_fp16.safetensors",
    "minimax_h3_audio_vae_fp32.safetensors",
}

# Ref2VA's reference inputs are Autogrow TemplatePrefix, whose generated names are
# ZERO-indexed (`f"{prefix}{i}" for i in range(max)`), so the wire names are
# ref_image_0..8, ref_video_0..2, ref_video_audio_0..2, ref_audio_0..2. The prompt
# tags, confusingly, are 1-based: <Picture 1> refers to ref_image_0.
AUTOGROW_LIMITS = {
    "ref_image_": 9,
    "ref_video_": 3,
    "ref_video_audio_": 3,
    "ref_audio_": 3,
}


def check(path: str) -> list[str]:
    with open(path, encoding="utf-8") as fh:
        wf = json.load(fh)
    errs: list[str] = []

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
            elif isinstance(val, str) and val.endswith(".safetensors") and val not in ON_VOLUME:
                errs.append(f"{nid}.{key}: {val!r} is not on the network volume")

        if ct == "MiniMaxH3ReferenceToVideo":
            # ref_video_audio_N is index-paired with ref_video_N by the node, so a
            # soundtrack without its video is silently ignored rather than erroring.
            for key in inputs:
                for prefix, limit in AUTOGROW_LIMITS.items():
                    if key.startswith(prefix) and key[len(prefix):].isdigit():
                        idx = int(key[len(prefix):])
                        if idx >= limit:
                            errs.append(f"{nid}.{key}: index {idx} exceeds max {limit - 1} for {prefix}*")
                        break
            for key in inputs:
                if key.startswith("ref_video_audio_"):
                    n = key[len("ref_video_audio_"):]
                    if f"ref_video_{n}" not in inputs:
                        errs.append(f"{nid}.{key}: no matching ref_video_{n}; it will be ignored")

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

    saves = [n for n, x in wf.items() if x.get("class_type") == "SaveVideo"]
    if len(saves) != 1:
        errs.append(f"expected exactly 1 SaveVideo node, found {len(saves)}")

    return errs


def main() -> int:
    root = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir)
    paths = sorted(glob.glob(os.path.join(root, "workflows", "*.json")))
    if not paths:
        print("no workflows found", file=sys.stderr)
        return 1

    failed = False
    for path in paths:
        errs = check(path)
        name = os.path.basename(path)
        if errs:
            failed = True
            print(f"FAIL {name}")
            for e in errs:
                print(f"   - {e}")
        else:
            print(f"PASS {name}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
