# Live deployment

Resources created on RunPod for this project. Region is fixed by the network
volume — a volume cannot be moved between datacenters, so changing region means
re-downloading the 62.4 GiB of weights.

| Resource | ID | Notes |
|---|---|---|
| Network volume | `wy4cyuwk18` | `minimax-h3-models`, 120 GB, **CA-MTL-3** |
| Template | `xmb6e99xm4` | `minimax-h3-comfyui`, 40 GB container disk |
| Endpoint | `t9u2wy8o9ucnpz` | `minimax-h3` |
| Image | `ghcr.io/akshaylaghate/minimax-h3-comfyui:0.1.0` | digest `sha256:05f79e38…` |

Endpoint URL: `https://api.runpod.ai/v2/t9u2wy8o9ucnpz/run`

## Endpoint configuration

- GPUs: RTX PRO 6000 Blackwell Server Edition ($2.09/hr), A100 80GB PCIe ($1.39/hr)
- Workers: min 0, max 1 · idle timeout 60 s · FlashBoot on
- Execution timeout: 1800 s (the 600 s default is too short for 768p)

CA-MTL-3 is capacity-constrained: only those two GPU types are offered and it has
no CPU pod availability at all (the volume was filled with an A100 pod for want of
a cheaper option). If workers become hard to schedule, that is the reason — and the
fix is a new volume in a different datacenter plus a re-download.

## Volume contents

Verified byte-exact against the Hugging Face API after download:

```
models/diffusion_models/minimax_h3_fl2va_int8_convrot.safetensors   34038892334
models/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors 27141342152
models/vae/minimax_h3_video_vae_fp16.safetensors                     5207808496
models/vae/minimax_h3_audio_vae_fp32.safetensors                       605254808
```

Mounts at `/runpod-volume` on a serverless worker, `/workspace` on a Pod.

## Verified smoke test

640×384, 124 frames, 20 steps, `res_multistep` — returned as base64 (no S3 yet):

```
video: h264  640x384  24 fps  124 frames  5.167s
audio: aac   32000 Hz  2ch stereo         5.167s
537,627 bytes
```

Timings on RTX PRO 6000 Blackwell Server Edition:

| Phase | Time |
|---|---|
| Cold start (22.7 GiB image pull + ~62 GiB weights off volume) | 865 s |
| Generation | 247 s |

The cold start is paid once per worker; it recurs whenever the worker scales to zero,
which is why `idleTimeout` is 60 s rather than the 5 s default. For latency-sensitive
use, `workersMin: 1` avoids it entirely at the cost of a permanently billed worker.

A first worker came up **unhealthy**: the endpoint was created while the GHCR package
was still private, so the pull failed and RunPod kept the worker in that state rather
than retrying once the image went public. Recycling `workersMax` 1 → 0 → 1 dropped it
and provisioned a healthy replacement. If the image is ever re-tagged or its visibility
changes, expect to do the same.

## Measured performance

RTX PRO 6000 Blackwell Server Edition, 20 steps, `res_multistep`, 124 frames (5.17 s):

| Job | Pixels | Queue delay | Generation | Output |
|---|---|---|---|---|
| 640×384 T2V, cold worker | 246 k | 865 s | 247 s | 0.5 MB |
| 768×1024 I2V, warm worker | 786 k | **10 s** | 419 s | 1.7 MB |

Two things to take from this:

- **Warm vs cold is 865 s → 10 s.** The cold start is the image pull plus ~62 GiB of
  weights read off the network volume. It recurs every time the worker scales to zero.
  `workersMin: 1` removes it entirely at the cost of a continuously billed worker.
- **Generation scales sub-linearly with pixels**: 3.2× the pixels cost only 1.7× the
  time. Higher resolutions are better value than the pixel count suggests. Extrapolating,
  768p×15 s (≈3× the frames) lands near 20 min — comfortably inside the 1800 s execution
  timeout, but not by much. Raise it before going longer.

Base64 return is viable further than expected: the 768×1024 clip came back as 2.4 MB of
base64 for a 1.7 MB file, still well inside the 10 MB `/run` cap. S3 becomes necessary
around 768p/15 s.

## Outstanding

1. **GHCR package visibility** — the image is private, so workers cannot pull it.
   Flip at
   `https://github.com/users/AkshayLaghate/packages/container/minimax-h3-comfyui/settings`
   → Change visibility → Public. Repo stays private.
2. **S3 environment variables** — add `BUCKET_ENDPOINT_URL`, `BUCKET_ACCESS_KEY_ID`,
   `BUCKET_SECRET_ACCESS_KEY` to the endpoint in the RunPod console. Without them the
   handler returns base64, capped at 10 MB (`/run`) / 20 MB (`/runsync`). A 384p 5 s
   smoke test fits under that, so the pipeline can be validated before S3 is set up,
   but anything larger needs it.

## Quota note

RunPod caps total max-workers at 10 across all endpoints. To make room,
`comfy_worker_wan2.2_rapid` (`fplnve432lbwo7`) was reduced from max 2 → 1. It still
works; it just cannot run two jobs concurrently. Reverse with:

```bash
curl -X PATCH https://rest.runpod.io/v1/endpoints/fplnve432lbwo7 \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" -d '{"workersMax":2}'
```
