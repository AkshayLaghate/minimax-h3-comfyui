# Live deployment

Resources created on RunPod for this project. Region is fixed by the network
volume — a volume cannot be moved between datacenters, so changing region means
re-downloading the 62.4 GiB of weights.

| Resource | ID | Notes |
|---|---|---|
| Network volume (active) | `lcfgsk4z8s` | `minimax-h3-eu`, 80 GB, **EU-RO-1** — I2V weights only |
| Network volume (old) | `wy4cyuwk18` | `minimax-h3-models`, 120 GB, CA-MTL-3 — retained, includes Ref2VA + LoRA |
| Template | `xmb6e99xm4` | `minimax-h3-comfyui`, 40 GB container disk |
| Endpoint | `t9u2wy8o9ucnpz` | `minimax-h3` |
| Image | `ghcr.io/akshaylaghate/minimax-h3-comfyui:0.1.0` | digest `sha256:05f79e38…` |

Endpoint URL: `https://api.runpod.ai/v2/t9u2wy8o9ucnpz/run`

## Endpoint configuration

- GPU: **RTX 5090 ($0.69/hr), EU-RO-1**
- Workers: min 0, max 1 · idle timeout 60 s · FlashBoot on
- Execution timeout: 1800 s (the 600 s default is too short for 768p)

CA-MTL-3 is capacity-constrained: only those two GPU types are offered and it has
no CPU pod availability at all (the volume was filled with an A100 pod for want of
a cheaper option). If workers become hard to schedule, that is the reason — and the
fix is a new volume in a different datacenter plus a re-download.

## Volume contents

Verified byte-exact against the Hugging Face API after download:

```
models/diffusion_models/minimax_h3_fl2va_int8_convrot.safetensors    34038892334
models/diffusion_models/minimax_h3_ref2va_int8_convrot.safetensors   34038894550
models/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors 27141342152
models/vae/minimax_h3_video_vae_fp16.safetensors                      5207808496
models/vae/minimax_h3_audio_vae_fp32.safetensors                       605254808
```

~94 GiB of 120 GB. Mounts at `/runpod-volume` on a serverless worker.

## Pods do not work in CA-MTL-3 with this volume

Attaching a network volume restricts placement to hosts that can mount it, and in
CA-MTL-3 that resolved to a single machine with a broken GPU device node. Every Pod
crash-looped identically:

```
error creating device nodes: mount src=/dev/dri/card4 ... no such file or directory
```

Five attempts across A100 / Blackwell / 4090-class all failed the same way; CPU pods
returned "no instances available". This is a RunPod infrastructure fault, not a config
error — and the first volume fill (before this) succeeded on a different host, so it
appeared mid-project.

**Workaround that works:** serverless placement finds healthy hosts on the same volume.
To run a one-off command against the volume, scale the production endpoint to
`workersMax: 0` (to stay inside the 10-worker quota), create a throwaway serverless
endpoint whose template's `dockerStartCmd` does the work and then sleeps, watch it with
`stream-worker-logs`, then delete it and restore. The Ref2VA download took 55 s that way
(~620 MB/s to the volume). Worth retrying a plain Pod first — the broken host may be
repaired.

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
| 768×1024 Ref2VA (image + audio ref), warm | 786 k | 1 s | 450 s | 1.4 MB |

Ref2VA cost only ~7% more than I2V at the same size, despite reference tokens riding
through every sampling step and a 34 GiB checkpoint swap off the volume — with
`ref_image_size: "match"`. The `"max"` setting (2048 px short edge) is documented as
several times slower.

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

## Speed-up experiments

All three runs use an identical seed, prompt and first frame at 768×1024 / 124 frames,
so only the variable under test differs.

| Variant | Steps | Generation | vs baseline | Output | Verdict |
|---|---|---|---|---|---|
| I2V baseline (`res_multistep`) | 20 | 419 s | — | 1.7 MB | reference |
| **I2V + EasyCache** (threshold 0.2) | 20 | **302 s** | **−28%** | 2.1 MB | **clean, adopted** |
| Turbo LoRA (`euler`+`beta`, str 2.0) | 8 | 302 s | −28% | 6.1 MB | **rejected — visible noise** |
| Ref2VA baseline | 20 | 450 s | — | 1.4 MB | reference |
| **Ref2VA + EasyCache** | 20 | **256 s** | **−43%** | 1.7 MB | **clean, adopted** |

Cost at $2.09/hr (RTX PRO 6000 Blackwell):

| Run | Time | Cost | Saving |
|---|---|---|---|
| I2V baseline | 419 s | $0.2433 | — |
| I2V + EasyCache | 302 s | $0.1753 | $0.068/run |
| Ref2VA baseline | 450 s | $0.2612 | — |
| Ref2VA + EasyCache | 256 s | $0.1486 | **$0.113/run** |

**EasyCache pays off nearly twice as well on Ref2VA (−43%) as on I2V (−28%).** The likely
reason is that reference conditioning makes consecutive denoising steps more similar, so
the reuse threshold fires more often — but that is inference from two data points, not a
measured mechanism. Either way the output stays clean: 2.60 Mbps against the baseline's
2.05, nowhere near the 9.79 Mbps of the noisy Turbo LoRA run.

### Sampling is only half the runtime

Two points with the same everything except step count give the cost model:

```
per-step cost  :   9.8 s
fixed overhead : 223.8 s   (53% of the baseline run)
```

Fixed overhead is weight staging, text encoding through a 32B Qwen3-VL, and VAE-decoding
124 frames. **It sets a hard floor of ~224 s no matter how few steps you sample**, so
step-count reductions have sharply diminishing returns:

| Steps | Predicted |
|---|---|
| 20 | 419 s |
| 8 | 302 s |
| 4 | 263 s |
| 0 | 224 s |

This is why the Turbo LoRA disappoints here despite a genuine ~2.5× reduction in sampling
work: it can only attack the 47% that is sampling. EasyCache reaches the same wall-clock
without touching quality, because it skips redundant steps adaptively rather than
shortening the schedule.

### Turbo LoRA quality

At strength 2.0 / 8 steps the output carries heavy speckle across every frame — the 6.1 MB
file at 9.8 Mbps is noise, not detail (the clean baseline is ~2.8 Mbps). Composition and
motion survive; the texture does not. Lower strength (~1.0) and 10 steps might recover it,
but the ceiling is ~283 s against EasyCache's already-clean 302 s, so there is little to
win. The LoRA stays on the volume for future experiments.

### Not pursued: SageAttention

`--use-sage-attention` is actively dangerous on this deployment. ComfyUI issue
[#15263](https://github.com/Comfy-Org/ComfyUI/issues/15263) documents the global flag
auto-dispatching to an FP8 PV kernel on sm_120 — our GPU — whose accumulation error turns
output into pure noise past ~160k tokens, silently, after full sampling time. The safe
route is KJNodes' `Patch Sage Attention KJ` pinned to `sageattn_qk_int8_pv_fp16_cuda`,
which needs a custom node plus a third-party sm_120 wheel. (SageAttention 1.x from PyPI
would avoid both, being Triton-based with no FP8 PV kernel at all.)

It was dropped anyway, on arithmetic rather than risk. Stacked on EasyCache the runtime is
already 224 s fixed + ~78 s sampling, so even a 30% attention win returns roughly 279 s
against 302 s — about 8%. Not worth an image rebuild and a non-default attention path.
The same fixed-overhead ceiling caps every sampling-side optimisation.

## GPU cost comparison

Identical workload (768×1024, 124 frames, EasyCache, same seed/prompt/image):

| GPU | VRAM | $/hr | Time | Cost | Quality |
|---|---|---|---|---|---|
| RTX PRO 6000 Blackwell | 96 GB | $2.09 | 302 s | $0.1753 | 3.46 Mbps |
| **RTX 5090** | **32 GB** | **$0.69** | 411 s | **$0.0788** | 3.36 Mbps |

**The 5090 is 1.36× slower but 55% cheaper** — $0.097 saved per generation. Break-even was
3.03×, so it wins comfortably. Quality is indistinguishable by bitrate.

Notably it runs the full 62.4 GiB of weights on 32 GB of VRAM, streaming through ComfyUI's
dynamic VRAM loader. No pruned checkpoint or NVFP4 encoder needed, and no image change —
the 5090 is sm_120, the same architecture as the RTX PRO 6000, so the cu128 build works
unmodified.

### Capacity: trust GraphQL, not the capacity endpoint

`get-capacity` reports **pod** capacity, not serverless. It advertised RTX 5090 at "Medium"
in CA-MTL-3 where serverless had none — an endpoint pinned there sat at zero workers
indefinitely. The GraphQL `dataCenters.gpuAvailability` field was correct.

The reliable check is a throwaway probe endpoint: `python:3.11-slim` running
`nvidia-smi`, no volume, `workersMin: 1`. It confirms real serverless capacity for about
$0.03. RTX 5090 is serverless-available only in EU-CZ-1, EU-RO-1, EUR-IS-1 and EUR-NO-1.

Throttled workers do **not** bill — only the volumes accrued while one sat throttled.

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
