#!/usr/bin/env python3
"""Upscale a native H3 clip to 1080x1920 with Real-ESRGAN x2, locally.

The counterpart to scripts/upscale-to-1080.sh (Lanczos). Both produce exactly
1080x1920 from a 768x1344 source so the two can be compared like for like.

Why this runs here and not on the worker: an in-graph ImageUpscaleWithModel at
2x allocates 124*1536*2688*3*4 = 6.1 GB for the output batch on top of the 1.54 GB
decoded batch. The 55.88 GiB RAM tier OOMs at 4.6 GB of image tensors, so it never
had a chance. A local RTX 3060 does it for free.

RRDBNet is reimplemented here rather than pulling in basicsr/spandrel: it is a
fixed, well-known architecture and the weights are a plain state_dict, so a
dependency would buy nothing.

    python scripts/upscale-esrgan.py out/native.mp4 out/esrgan_1080p.mp4 \
        --model "C:/.../RealESRGAN_x2plus.pth"
"""
from __future__ import annotations

import argparse
import subprocess
import sys

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

W, H = 1080, 1920


class ResidualDenseBlock(nn.Module):
    def __init__(self, nf=64, gc=32):
        super().__init__()
        self.conv1 = nn.Conv2d(nf, gc, 3, 1, 1)
        self.conv2 = nn.Conv2d(nf + gc, gc, 3, 1, 1)
        self.conv3 = nn.Conv2d(nf + 2 * gc, gc, 3, 1, 1)
        self.conv4 = nn.Conv2d(nf + 3 * gc, gc, 3, 1, 1)
        self.conv5 = nn.Conv2d(nf + 4 * gc, nf, 3, 1, 1)
        self.lrelu = nn.LeakyReLU(0.2, inplace=True)

    def forward(self, x):
        x1 = self.lrelu(self.conv1(x))
        x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
        x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
        x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
        x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
        return x5 * 0.2 + x


class RRDB(nn.Module):
    def __init__(self, nf, gc=32):
        super().__init__()
        self.rdb1, self.rdb2, self.rdb3 = (ResidualDenseBlock(nf, gc) for _ in range(3))

    def forward(self, x):
        return self.rdb3(self.rdb2(self.rdb1(x))) * 0.2 + x


class RRDBNet(nn.Module):
    """scale=2 variant: input is pixel-unshuffled by 2, then upsampled 4x."""

    def __init__(self, in_ch=3, out_ch=3, nf=64, nb=23, gc=32):
        super().__init__()
        self.conv_first = nn.Conv2d(in_ch * 4, nf, 3, 1, 1)
        self.body = nn.Sequential(*[RRDB(nf, gc) for _ in range(nb)])
        self.conv_body = nn.Conv2d(nf, nf, 3, 1, 1)
        self.conv_up1 = nn.Conv2d(nf, nf, 3, 1, 1)
        self.conv_up2 = nn.Conv2d(nf, nf, 3, 1, 1)
        self.conv_hr = nn.Conv2d(nf, nf, 3, 1, 1)
        self.conv_last = nn.Conv2d(nf, out_ch, 3, 1, 1)
        self.lrelu = nn.LeakyReLU(0.2, inplace=True)

    def forward(self, x):
        feat = F.pixel_unshuffle(x, 2)
        feat = self.conv_first(feat)
        feat = feat + self.conv_body(self.body(feat))
        feat = self.lrelu(self.conv_up1(F.interpolate(feat, scale_factor=2, mode="nearest")))
        feat = self.lrelu(self.conv_up2(F.interpolate(feat, scale_factor=2, mode="nearest")))
        return self.conv_last(self.lrelu(self.conv_hr(feat)))


def center_crop_to_aspect(img: np.ndarray, w: int, h: int) -> np.ndarray:
    sh, sw = img.shape[:2]
    if sw / sh > w / h:
        nw = (round(sh * w / h) // 2) * 2      # keep even for pixel_unshuffle
        x = (sw - nw) // 2
        return img[:, x:x + nw]
    nh = (round(sw * h / w) // 2) * 2
    y = (sh - nh) // 2
    return img[y:y + nh, :]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--model", required=True)
    ap.add_argument("--crf", default="16")
    a = ap.parse_args()

    dev = "cuda" if torch.cuda.is_available() else "cpu"
    sd = torch.load(a.model, map_location="cpu", weights_only=True)
    sd = sd.get("params_ema") or sd.get("params") or sd

    net = RRDBNet().eval()
    net.load_state_dict(sd, strict=True)      # strict: a silent mismatch would be garbage
    net = net.to(dev).half() if dev == "cuda" else net.to(dev)
    print(f"loaded {a.model} on {dev}", file=sys.stderr)

    cap = cv2.VideoCapture(a.src)
    fps = cap.get(cv2.CAP_PROP_FPS) or 24

    proc = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
         "-i", a.src, "-map", "0:v:0", "-map", "1:a:0?",
         "-c:v", "libx264", "-preset", "slow", "-crf", a.crf, "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-shortest", a.dst],
        stdin=subprocess.PIPE,
    )

    n = 0
    with torch.no_grad():
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            crop = center_crop_to_aspect(frame, W, H)
            t = torch.from_numpy(crop[:, :, ::-1].copy()).permute(2, 0, 1).unsqueeze(0)
            t = (t.float() / 255.0).to(dev)
            if dev == "cuda":
                t = t.half()
            out = net(t).clamp(0, 1).float().squeeze(0).permute(1, 2, 0).cpu().numpy()
            out = (out[:, :, ::-1] * 255.0).round().astype(np.uint8)   # RGB->BGR
            # 2x then down to the exact canvas: INTER_AREA is the correct
            # filter for a reduction and suppresses the shimmer ESRGAN adds.
            out = cv2.resize(out, (W, H), interpolation=cv2.INTER_AREA)
            proc.stdin.write(out.tobytes())
            n += 1
            if n % 25 == 0:
                print(f"  {n} frames", file=sys.stderr)
    cap.release()
    proc.stdin.close()
    proc.wait()
    print(f"{n} frames -> {a.dst}", file=sys.stderr)
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
