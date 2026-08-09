#!/usr/bin/env python3
"""Measure and compare generated clips, so quality claims rest on numbers.

Bitrate alone was enough to reject the Turbo LoRA (9.8 Mbps of speckle against a
clean 2.8) and to show the 362-frame clip was drifting rather than corrupting
(per-frame bitrate FELL). It is not enough to separate an upscaler from a
resampler, which is what this adds.

    python scripts/compare-clips.py out/A.mp4 out/B.mp4 --ref input/first_frame_768x1344.png

Metrics, and why each is here:

  sharpness      mean variance-of-Laplacian per frame. Higher = more high-frequency
                 detail. This is what separates ESRGAN from Lanczos.

                 ONLY COMPARABLE BETWEEN CLIPS OF THE SAME RESOLUTION. The Laplacian
                 is a per-pixel operator, so spreading identical detail over more
                 pixels lowers its variance: the same clip at 768x1344 scores 1181.8
                 and at 1080x1920 scores 366.5. That drop is the metric, not a loss
                 of quality. Compare upscalers against each other at one output size;
                 never use this to judge whether upscaling "cost" sharpness.
  shimmer        coefficient of variation (std/mean) of that series across frames.
                 Per-frame upscalers invent detail independently, so their
                 sharpness wobbles frame to frame even when the scene is steady.
                 Dimensionless, so a sharper clip is not penalised for being sharp.
  motion         mean |frame[i] - frame[i-1]|. Real motion plus any temporal noise.
  drift          mean |frame[i] - frame[0]| over the final 10% of the clip. This is
                 the measurement that exposed 362-frame wandering; a clip that
                 holds its composition stays low.
  ref_delta      mean |frame[0] - reference image|, after fitting the reference to
                 the clip's canvas. How faithfully the first frame honours the
                 conditioning image.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

try:
    import cv2
    import numpy as np
except ImportError:
    sys.exit("Requires opencv-python and numpy")


def probe(path: str) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", path],
        capture_output=True, text=True, check=True,
    ).stdout
    d = json.loads(out)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    num, den = (v["r_frame_rate"].split("/") + ["1"])[:2]
    return {
        "width": v["width"],
        "height": v["height"],
        "fps": round(int(num) / int(den), 3),
        "duration": round(float(d["format"]["duration"]), 3),
        "size_bytes": int(d["format"]["size"]),
        "bitrate_mbps": round(int(d["format"]["bit_rate"]) / 1e6, 2),
        "audio": None if a is None else f'{a["codec_name"]} {a["sample_rate"]}Hz {a["channels"]}ch',
    }


def cover_fit(img: np.ndarray, w: int, h: int) -> np.ndarray:
    """Centre cover-crop then resize - matches ImageScale(crop="center")."""
    sh, sw = img.shape[:2]
    if sw / sh > w / h:
        nw = round(sh * w / h)
        img = img[:, (sw - nw) // 2:(sw - nw) // 2 + nw]
    else:
        nh = round(sw * h / w)
        img = img[(sh - nh) // 2:(sh - nh) // 2 + nh, :]
    return cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)


def analyse(path: str, ref: str | None) -> dict:
    info = probe(path)
    cap = cv2.VideoCapture(path)

    sharp: list[float] = []
    motion: list[float] = []
    drift: list[float] = []
    first_gray = prev_gray = None
    ref_delta = None
    n = 0

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        sharp.append(float(cv2.Laplacian(gray, cv2.CV_64F).var()))
        if prev_gray is not None:
            motion.append(float(np.mean(np.abs(gray.astype(np.int16) - prev_gray.astype(np.int16)))))
        if first_gray is None:
            first_gray = gray
            if ref:
                r = cv2.imread(ref, cv2.IMREAD_COLOR)
                if r is None:
                    sys.exit(f"cannot read reference {ref}")
                r = cv2.cvtColor(cover_fit(r, info["width"], info["height"]), cv2.COLOR_BGR2GRAY)
                ref_delta = float(np.mean(np.abs(gray.astype(np.int16) - r.astype(np.int16))))
        else:
            drift.append(float(np.mean(np.abs(gray.astype(np.int16) - first_gray.astype(np.int16)))))
        prev_gray = gray
        n += 1
    cap.release()

    tail = drift[int(len(drift) * 0.9):] if drift else [0.0]
    s = np.array(sharp) if sharp else np.array([0.0])
    return {
        **info,
        "frames": n,
        "sharpness": round(float(s.mean()), 1),
        "shimmer": round(float(s.std() / s.mean()), 4) if s.mean() else 0.0,
        "motion": round(float(np.mean(motion)) if motion else 0.0, 2),
        "drift": round(float(np.mean(tail)), 2),
        "ref_delta": None if ref_delta is None else round(ref_delta, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("clips", nargs="+")
    ap.add_argument("--ref", help="conditioning image to compare frame 0 against")
    a = ap.parse_args()

    rows = [(c, analyse(c, a.ref)) for c in a.clips]

    hdr = ["clip", "size", "frames", "dur", "Mbps", "sharpness", "shimmer", "motion", "drift", "ref_d"]
    print(f"{hdr[0]:<34}{hdr[1]:>11}{hdr[2]:>7}{hdr[3]:>7}{hdr[4]:>7}"
          f"{hdr[5]:>11}{hdr[6]:>9}{hdr[7]:>8}{hdr[8]:>8}{hdr[9]:>8}")
    for name, r in rows:
        short = name.replace("\\", "/").split("/")[-1]
        dims = "{}x{}".format(r["width"], r["height"])
        refd = "-" if r["ref_delta"] is None else r["ref_delta"]
        print(f"{short:<34}{dims:>11}{r['frames']:>7}{r['duration']:>7}"
              f"{r['bitrate_mbps']:>7}{r['sharpness']:>11}{r['shimmer']:>9}{r['motion']:>8}"
              f"{r['drift']:>8}{refd:>8}")
    print()
    for name, r in rows:
        print(f"{name.replace(chr(92), '/').split('/')[-1]}: audio {r['audio']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
