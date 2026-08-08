#!/usr/bin/env python3
"""Fit an image to the exact generation canvas for MiniMaxH3ImageToVideo.

This exists because of a sharp edge in the node. `first_frame` is resized with
crop mode "disabled" — the upstream comment reads "geometry anchor: plain stretch
to canvas". It does NOT preserve aspect ratio, so handing a 16:9 photo to a 3:4
canvas squeezes everything horizontally. (`last_frame` is different: it gets an
aspect-preserving centre cover-crop.)

Pre-fitting the image to the exact target size makes that stretch a no-op.

    python scripts/prep-first-frame.py input.png out/first_frame.png 768 1024
    python scripts/prep-first-frame.py input.png out/first_frame.png 576 1024 --mode pad

Modes:
  crop (default) — aspect-preserving cover crop, centred. Fills the frame, loses edges.
  pad            — fits the whole image inside the canvas, filling the remainder
                   with a blurred copy so nothing is cut. Use when a crop would
                   remove a subject.
"""
from __future__ import annotations

import argparse
import sys

try:
    from PIL import Image, ImageFilter
except ImportError:
    sys.exit("Pillow is required:  pip install Pillow")


def cover_crop(img: Image.Image, w: int, h: int) -> Image.Image:
    src_ar, dst_ar = img.width / img.height, w / h
    if src_ar > dst_ar:
        new_w = round(img.height * dst_ar)
        box = ((img.width - new_w) // 2, 0, (img.width - new_w) // 2 + new_w, img.height)
    else:
        new_h = round(img.width / dst_ar)
        box = (0, (img.height - new_h) // 2, img.width, (img.height - new_h) // 2 + new_h)
    return img.crop(box).resize((w, h), Image.LANCZOS)


def blur_pad(img: Image.Image, w: int, h: int) -> Image.Image:
    # Background: cover-crop the source, then blur it, so the bars carry the
    # scene's colour instead of dead black.
    bg = cover_crop(img, w, h).filter(ImageFilter.GaussianBlur(radius=max(w, h) // 40))
    scale = min(w / img.width, h / img.height)
    fg = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    bg.paste(fg, ((w - fg.width) // 2, (h - fg.height) // 2))
    return bg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("width", type=int)
    ap.add_argument("height", type=int)
    ap.add_argument("--mode", choices=["crop", "pad"], default="crop")
    a = ap.parse_args()

    for name, v in (("width", a.width), ("height", a.height)):
        if v % 32:
            return f"{name} {v} must be a multiple of 32"

    img = Image.open(a.src).convert("RGB")
    out = cover_crop(img, a.width, a.height) if a.mode == "crop" else blur_pad(img, a.width, a.height)
    out.save(a.dst)
    print(f"{a.src} ({img.width}x{img.height}) -> {a.dst} ({out.width}x{out.height}, {a.mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
