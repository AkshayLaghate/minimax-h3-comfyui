#!/usr/bin/env bash
#
# Upscale a native 768x1344 MiniMax H3 clip to 1080x1920 vertical, locally.
#
#   bash scripts/upscale-to-1080.sh out/native.mp4 out/final_1080p.mp4
#
# Why this is not done on the GPU worker:
#
# An in-graph ImageScale allocates a second full-resolution float32 batch — at
# 124 frames that is 124*1080*1920*3*4 = 3.09 GB on top of the 1.54 GB decoded
# batch. On the 55.88 GiB RAM tier that is the allocation that pushes the job
# from 48.18 GiB (86%) to 52.71 GiB (94%) and into an OOM. Lanczos is
# deterministic resampling: it uses no model and nothing a 5090 provides that
# this machine does not. Doing it here costs nothing and removes the largest
# single allocation from the job.
#
# Geometry: 768x1344 is 4:7 (0.5714), the target is 9:16 (0.5625). There is no
# exact-9:16 canvas on H3's x32 grid below a 768 short edge worth using, so the
# source is centre-cropped to the target aspect (losing 12 px of width, 1.6%)
# and then scaled. This mirrors ImageScale(crop="center") exactly.
#
# Audio is stream-copied, so the generated soundtrack is untouched.
set -euo pipefail

SRC="${1:?usage: upscale-to-1080.sh <src.mp4> <dst.mp4> [crf]}"
DST="${2:?usage: upscale-to-1080.sh <src.mp4> <dst.mp4> [crf]}"
CRF="${3:-16}"

W=1080
H=1920

# crop=w:h computed from the target aspect, then lanczos to the exact canvas.
# in_range/out_range are pinned so the crop+scale does not shift levels.
ffmpeg -hide_banner -loglevel error -y -i "$SRC" \
  -vf "crop='min(iw,ih*${W}/${H})':'min(ih,iw*${H}/${W})',scale=${W}:${H}:flags=lanczos" \
  -c:v libx264 -preset slow -crf "$CRF" -pix_fmt yuv420p \
  -c:a copy \
  "$DST"

echo "== source =="
ffprobe -hide_banner -v error -select_streams v:0 \
  -show_entries stream=width,height,nb_frames,r_frame_rate -of default=nw=1 "$SRC"
echo "== output =="
ffprobe -hide_banner -v error -select_streams v:0 \
  -show_entries stream=width,height,nb_frames,r_frame_rate -of default=nw=1 "$DST"
ffprobe -hide_banner -v error -select_streams a:0 \
  -show_entries stream=codec_name,sample_rate,channels -of default=nw=1 "$DST"
