# Live deployment

Resources created on RunPod for this project. Region is fixed by the network volume —
a volume cannot be moved between datacenters, so changing region means re-downloading the
weights. In practice that took ~2 minutes at EU-RO-1 speeds, so it is less binding than it
sounds.

| Resource | ID | Notes |
|---|---|---|
| Network volume | `lcfgsk4z8s` | `minimax-h3-eu`, 120 GB, **EU-RO-1** |
| Template | `xmb6e99xm4` | `minimax-h3-comfyui`, 40 GB container disk |
| Endpoint | `t9u2wy8o9ucnpz` | `minimax-h3` |
| Image | `ghcr.io/akshaylaghate/minimax-h3-comfyui:0.1.0` | digest `sha256:05f79e38…` |

Endpoint URL: `https://api.runpod.ai/v2/t9u2wy8o9ucnpz/run`

## Endpoint configuration

- GPU: **RTX 5090 ($0.69/hr), EU-RO-1**
- Workers: min 0, max 1 · idle timeout 60 s · FlashBoot on
- Execution timeout: **3600 s** (a 15 s clip runs 803 s; 1800 s left too little margin)

The deployment originally sat in CA-MTL-3, which offered only two serverless GPU types and
whose A100 host was broken (see below). Moving to EU-RO-1 unlocked the RTX 5090 and cut
per-clip cost by more than half. EU-RO-1 also carries RTX PRO 6000 Blackwell and
A100-SXM4-80GB as fallbacks if 5090 capacity tightens.

## Volume contents

Verified byte-exact against the Hugging Face API after download:

```
models/diffusion_models/minimax_h3_fl2va_int8_convrot.safetensors    34038892334
models/diffusion_models/minimax_h3_ref2va_pruned_int8_convrot.safetensors 20970379616
models/text_encoders/qwen3vl_32b_minimax_h3_int8_convrot.safetensors 27141342152
models/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors    15687142551
models/vae/minimax_h3_video_vae_fp16.safetensors                      5207808496
models/vae/minimax_h3_audio_vae_fp32.safetensors                       605254808
models/upscale_models/RealESRGAN_x2plus.pth                             67061725
```

~96.6 GiB of 120 GB. Mounts at `/runpod-volume` on a serverless worker.

Two of these are dead weight and can be deleted to reclaim ~15.7 GiB if space is ever
needed: the **NVFP4 encoder** (rejected, see below) and **RealESRGAN** (upscaling moved off
the worker entirely). `minimax_h3_fl2va_pruned_int8_convrot.safetensors` was deleted on
2026-08-09 after it was measured to degrade output.

## Historical: pods did not work in CA-MTL-3 with the old volume

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

### Final configuration

| Mode | Model | GPU | Time | Cost | vs original |
|---|---|---|---|---|---|
| I2V | `fl2va_int8_convrot` | RTX 5090 | 411 s | **$0.1811** | **−55%** |
| Ref2VA | `ref2va_pruned_int8_convrot` | RTX 5090 | 386 s | **$0.1701** | **−61%** |

Both with EasyCache, 768×1024, 124 frames. At 100 clips/month that is $22.56 and $26.67
saved respectively, against a standing volume cost of $8.40/month.

### Price from billing records, not the published pod rate

Serverless bills at a **different, higher rate than the pod price** for the same card.
`GET /v1/billing/endpoints?grouping=gpuTypeId` gives the truth (amount ÷ timeBilled):

| GPU | Pod price | Actual serverless | Ratio |
|---|---|---|---|
| RTX 5090 | $0.69 | **$1.586/hr** | 2.30× |
| A100 80GB PCIe | $1.39 | $2.725/hr | 1.96× |
| RTX PRO 6000 Blackwell | $2.09 | **$3.495/hr** | 1.67× |

The multiplier is not constant, so pod prices cannot be used even for ranking. Always
price from billing.

### A40 is not usable, and would not be cheaper

Two independent disqualifications:

1. **No network volume.** A40 exists only in CA-MTL-1 and EU-SE-1, and neither supports
   network volumes. None of the 18 storage-capable datacenters offers one. The image
   carries no weights, so a volume is mandatory.
2. **It would cost more.** Against the 5090's real $1.586/hr, A40 serverless lands near
   $0.73–1.01/hr, so it breaks even only below 1.57–2.16× slower. A40 has 2.57× less
   memory bandwidth (696 vs 1792 GB/s) and 3.34× less FP16 throughput, and this workload
   is memory-bound — a 2.5–3× slowdown is realistic, making it 30–90% *more* expensive.

A40's 48 GB of VRAM *would* likely run the unpruned Ref2VA (only ~14 GiB offloaded to host
RAM versus the 5090's ~30 GiB), but that is moot given the above.

The pruned Ref2VA checkpoint is visually indistinguishable from the non-pruned one:
2.56 Mbps against 2.60, with the same detail and no artefacts. Swapping it was necessary
rather than optional — see below.

### Ref2VA does not fit on the 5090 unpruned — and the limit is host RAM, not VRAM

Ref2VA on the 5090 fails after ~231 s with `ComfyUI HTTP unreachable during websocket
reconnect`. That message is a symptom; the system log gives the cause:

```
INFO: high memory utilization - 44.39GiB / 55.88GiB (79 %)   <- I2V, survives
INFO: high memory utilization - 51.02GiB / 55.88GiB (91 %)   <- Ref2VA
WARN: container is unhealthy: triggered memory limits (OOM)
```

**55.88 GiB is system RAM, not VRAM.** Weights that do not fit in the 32 GB of VRAM are
offloaded to host RAM, and the 5090 tier allocates ~56 GiB of it. The 62.4 GiB weight set
plus Ref2VA's reference tensors exceeds that; I2V, with no reference conditioning, stays
under at 79%.

So the 5090 constraint is the RAM that comes with the tier, not the card's memory. Three
ways forward, none yet measured:

| Option | Effect |
|---|---|
| Run Ref2VA on RTX PRO 6000 ($2.09) | Known working at $0.1486; keeps 5090 for I2V |
| **Pruned Ref2VA (19.5 GiB), encoder unchanged** | **50.2 GiB total — fits, and quality is unchanged. Adopted.** |
| A tier with more RAM | Needs a per-tier RAM table, which the API does not expose |

Resolved by swapping in the pruned Ref2VA checkpoint alone: 62.4 → 50.2 GiB of weights,
which clears the ceiling. The text encoder was deliberately left at INT8 so any quality
difference would be attributable to the pruned checkpoint rather than to two changes at
once — and there is none worth reporting.

### Capacity: trust GraphQL, not the capacity endpoint

`get-capacity` reports **pod** capacity, not serverless. It advertised RTX 5090 at "Medium"
in CA-MTL-3 where serverless had none — an endpoint pinned there sat at zero workers
indefinitely. The GraphQL `dataCenters.gpuAvailability` field was correct.

The reliable check is a throwaway probe endpoint: `python:3.11-slim` running
`nvidia-smi`, no volume, `workersMin: 1`. It confirms real serverless capacity for about
$0.03. RTX 5090 is serverless-available only in EU-CZ-1, EU-RO-1, EUR-IS-1 and EUR-NO-1.

Throttled workers do **not** bill — only the volumes accrued while one sat throttled.

## Clip length scaling

768×1024, EasyCache, RTX 5090:

| Clip | Frames | Time | Cost | Per second of video |
|---|---|---|---|---|
| 5.17 s | 124 | 411 s | $0.1811 | $0.0350 |
| 15.08 s | 362 | 803 s | $0.3536 | $0.0234 |

**2.92× the frames costs only 1.95× the time** — but see the quality note below; the
cheaper per-second figure is not usable in practice.

362 frames is the maximum on the 17k+5 grid within the model's trained range (124–362);
360 is not a legal value. Peak host RAM was 66.29 GiB.

### 362 frames drifts — stitch two 175-frame segments instead

At 362 frames the scene **wanders away from the conditioning image**: by 12.5 s the camera
has pulled back, the room has restyled and the costumes have changed. It is not artefact
noise — per-frame bitrate *falls* over the clip (23.6 → 18.3 kB), so the scene is getting
smoother, not corrupted. EasyCache is not the cause; it stays enabled in the working
configuration below.

At **175 frames (7.29 s)** composition holds exactly — the final frame is the input image
with the action advanced, no drift.

To reach ~15 s, generate two 175-frame segments and pass **segment 1's final frame as
segment 2's `first_frame`**. Measured: the join reproduces the handoff frame to within a
3.3% mean pixel difference.

| Approach | Length | Time | Cost | Quality |
|---|---|---|---|---|
| Single clip, 362 frames | 15.08 s | 803 s | $0.3536 | drifts badly |
| **Two 175-frame segments** | **14.58 s** | **770 s** | **$0.3394** | **holds** |

The stitched route is both cheaper and better. Caveat: each segment generates its own
audio, so the soundtrack changes character at the seam — replace with a single audio bed
if that matters.

## 1080p: the model cannot generate it, and neither can MiniMax

**H3's native canvas is a 768 px short edge, capped at 768x1344 (1.03 MP)**, rounded to a
multiple of 32 — the `EmptyMiniMaxH3LatentAV` / `MiniMaxH3ImageToVideo` defaults are
`1344x768`, and ComfyUI's tutorial says to raise Megapixels "to about 1.0 at 16:9" for full
quality.

The decisive evidence is not the documentation but what MiniMax withheld: their 2K output
comes from **`H3-Regenerate-2K`, a separate upscaling module that was not open-sourced**.
The Comfy blog's "output runs to 2K" describes the hosted product, not this checkpoint.
Even the model's authors reach 2K by upscaling a 768p render. So does this deployment.

Two consequences worth internalising:

- **768x1024 was already exactly the native canvas at 3:4.** Nothing was being left on the
  table. More native pixels are only available by going wider — 768x1344 at 9:16, or
  1344x768 at 16:9, both ~1.03 MP.
- There is no exact-9:16 canvas on the x32 grid below a 768 short edge worth using: the
  largest is 576x1024 (0.59 MP), *fewer* pixels than 768x1024. Use **768x1344** (4:7) and
  let `ImageScale` with `crop="center"` shave 1.6% of the width on the way to 1080x1920.
  One node, no distortion, no letterboxing.

`scripts/validate-workflows.py` now warns when a workflow exceeds the native canvas.

### 768x1344 does not fit on a small-RAM 5090 host with the unpruned checkpoint

The first attempt at 768x1344 / 124 frames OOMed after 292 s:

```
INFO: high memory utilization - 46.4GiB / 55.88GiB (83 %)
INFO: high memory utilization - 52.44GiB / 55.88GiB (93 %)
WARN: very high memory utilization: 53.67GiB / 55.88GiB (96 %)
WARN: container is unhealthy: triggered memory limits (OOM)
```

Three things this cost, all avoidable:

1. **A second `SaveVideo` was the wrong idea.** It was added to emit the native 768x1344
   master alongside the 1080p deliverable, but it pins the decoded batch (1.54 GB) alive
   next to the upscaled one (3.10 GB) and runs a second encode — on the job already closest
   to the ceiling. Do not add output branches to a memory-bound graph.
2. **One OOM poisons the whole queue.** Jobs B and C, submitted in the same batch, failed
   in 2.7 s and 0.5 s with `ComfyUI server (127.0.0.1:8188) not reachable`. Batching
   variants for warmth control only works if the first one survives.
3. **The dead worker crash-loops and keeps billing.** It emitted `container is unhealthy`
   every few seconds for ~3.5 minutes. Only `workersMax: 0` reaps it — and a job submitted
   into that window fails instantly against the restarting container. Watch the system log
   after any failure, and scale to zero before resubmitting.

### The upscale must happen off the GPU

Removing the in-graph `ImageScale` is what made 1080p work. Measured peaks on the
55.88 GiB tier, 768x1344 / 124 frames, pruned checkpoint:

| Graph | Sampling | Peak | Result |
|---|---|---|---|
| + `ImageScale` to 1080x1920 | 48.18 GiB (86%) | 52.71 GiB, +3.09 GB pending | **OOM** |
| native only | 48.40 GiB (86%) | **52.94 GiB (94%)** | **completed, 461.6 s** |

The failing graph sampled at 52.71 GiB and then had to allocate `ImageScale`'s
124x1080x1920x3x4 = 3.09 GB on top — 55.8 GiB against a 55.88 GiB limit. That is the
OOM, to within rounding.

**Lanczos is deterministic resampling.** It runs no model and uses nothing a 5090
provides, so paying GPU rates *and* the tightest resource in the system to do it was
simply the wrong place to put the work. `scripts/upscale-to-1080.sh` does it locally
with ffmpeg for free, and upscaling before h264 rather than after avoids a second
compression generation.

Note the native graph still peaks at **94%**. 768x1344 at 124 frames sits at the edge of
this RAM tier with no in-graph post-processing at all.

(Those runs used the pruned checkpoint, whose output was afterwards rejected on quality —
see below. The memory conclusion stands: it is about tensor allocation, not weights.)

### Direct 1080p generation does not run

The control: 1088x1920 (2.09 MP), 124 frames, same seed, prompt and conditioning image,
no in-graph upscale.

```
51.61 GiB (92 %)   <- sampling, 3.2 GiB above the native run
53.03 GiB (94 %)   <- VAE decode
triggered memory limits (OOM)
```

It burned **754.7 s of GPU time and produced nothing**. So the answer to "generate 1080p
directly or upscale a 768p render" is not merely that direct is worse — on this tier it is
not runnable, and it fails *after* paying for the full sampling pass.

### Lanczos vs Real-ESRGAN at 1080x1920

Both from the same native master, both exactly 1080x1920, so directly comparable:

| Upscaler | Sharpness | Shimmer | Motion | Mbps | Cost |
|---|---|---|---|---|---|
| Lanczos (ffmpeg) | 366.5 | 0.1554 | 3.85 | 22.43 | free, ~2 s |
| **Real-ESRGAN x2 -> INTER_AREA** | 355.1 | **0.1117** | 3.55 | 23.25 | free, ~4 min on an RTX 3060 |

**ESRGAN wins, but not for the expected reason.** It is fractionally *less* sharp, not
more — at a 1.43x upscale there is little for it to invent. What it does is cut shimmer
by 28%, because upscaling 2x and then reducing with `INTER_AREA` supersamples away the
grain that H3 puts in flat areas. Side by side at 2x zoom the wall behind the subjects is
visibly cleaner while eyelashes and petals stay equally crisp.

An in-graph `ImageUpscaleWithModel` was never viable regardless: at 2x it needs
124x1536x2688x3x4 = 6.1 GB for the output batch, on a tier that OOMs at 4.6 GB of image
tensors. `RealESRGAN_x2plus.pth` is on the volume (67,061,725 bytes) but unused; the work
happens in `scripts/upscale-esrgan.py`, which reimplements RRDBNet against plain torch so
no basicsr/spandrel dependency is needed.

### Pruned FL2VA is NOT interchangeable with the full checkpoint

The pruned Ref2VA is documented above as visually indistinguishable. **That does not
generalise to FL2VA**, and assuming it did cost a round of bad output. Single-variable
A/B — same seed, prompt, conditioning image, canvas (768x1024), steps and EasyCache
settings, only `unet_name` changed:

| Checkpoint | Mbps | Sharpness | Shimmer | ref_delta |
|---|---|---|---|---|
| `fl2va_int8_convrot` | 3.50 | 488.5 | 0.118 | 9.16 |
| `fl2va_pruned_int8_convrot` | 4.93 (+41%) | 812.1 (+66%) | 0.1428 (+21%) | 14.18 (+55%) |

From an identical seed, +66% Laplacian energy is not detail — it is noise. Zoomed on the
crown, the full checkpoint resolves coherent gold filigree with smooth gradients; the
pruned one breaks it into scratchy over-etched scribble with purple fringing on the gems
and colour noise along every edge (Laplacian variance on that crop: 1284 vs 2240). The
+55% `ref_delta` says it also honours the conditioning image less faithfully.

**Rising sharpness alongside rising bitrate is the noise signature**, the same one that
rejected the Turbo LoRA. Treat any change that increases both as suspect until a
single-variable A/B says otherwise.

The pruned checkpoint was adopted to fix an OOM — and it was not even the fix; removing
the in-graph `ImageScale` was. It remains on the volume (20,970,379,616 bytes) but no
workflow references it, and it should not be used for I2V.

### Canvas is bounded by host RAM once the full checkpoint is required

**Do not size a canvas from the `high memory utilization` lines.** They are sampled every
30 s, so they miss peaks between samples. The "44.39 GiB (79%)" recorded above for
768x1024 was one sample and was never the true peak — sizing 704x1216 from it predicted
84% and the run OOMed at 98%. Measured peaks are much closer together than that figure
suggests:

| Run | Peak | Result |
|---|---|---|
| pruned @ 768x1024 | 54.38 GiB (97%) | survived |
| unpruned @ 704x1216, **cold worker** | 54.85 GiB (98%) | **OOM** |

Half a gibibyte separates success from failure. Every run on this tier grazes the ceiling
and which side it lands on is partly luck.

| Canvas | MP | vs 768x1024 | Upscale to 1080x1920 | Status |
|---|---|---|---|---|
| **640x1120** | **0.717** | **0.91x** | **1.71x** | **works — production default** |
| 768x1024 | 0.786 | 1.00x | 1.88x | works (3:4; a 9:16 crop loses 25% of width) |
| 704x1216 | 0.856 | 1.09x | 1.58x | **OOM** |
| 768x1344 | 1.032 | 1.31x | 1.43x | OOM |

**With the full checkpoint the 55.88 GiB tier cannot exceed ~768x1024**, so 640x1120 is
the vertical default: below the proven pixel count, already near 9:16 so the crop is
minimal, and on weights that resolve detail properly.

Reaching the full 768x1344 native canvas needs host RAM, not a canvas tweak. The untested
lever is the text encoder: `qwen3vl_32b_minimax_h3_nvfp4_awq` is 14.61 GiB against the
INT8 build's 25.28 GiB, freeing ~10.7 GiB — far more than any canvas change — and NVFP4
has native tensor-core support on the 5090's sm_120. Quantising the *text encoder* affects
prompt understanding rather than pixel rendering, so it is much less likely to reproduce
the pruned-checkpoint speckle.

### Changing checkpoint needs a cold worker

A job that asks for a different `unet_name` than the worker last loaded OOMs during model
load (~100 s in, well before sampling). A restarted container still showed **44.42 GiB
(79%) before loading a single weight** — safetensors read off the network volume land in
page cache, which counts against the container's cgroup limit. Scale the endpoint to
`workersMax: 0`, confirm zero workers, then resubmit.

### The 1080x1920 vertical recipe

1. Generate `workflows/minimax_h3_i2v_vertical_640x1120_api.json` — 640x1120, 124 frames,
   **full** `fl2va_int8_convrot`, EasyCache. Measured: **407.8 s, $0.180.**
2. `python scripts/upscale-esrgan.py <native>.mp4 <out>.mp4 --model RealESRGAN_x2plus.pth`
   — free, local, ~4 min on an RTX 3060.

Conditioning images must be pre-fitted to the exact canvas with
`scripts/prep-first-frame.py`; `first_frame` stretches rather than fits. Note a 9:16 crop
of a landscape source keeps only ~31% of its width.

| Route | GPU time | Cost | Output |
|---|---|---|---|
| **Native + local upscale** | ~460 s | **~$0.20** | 1080x1920, clean |
| Direct 1088x1920 | 754.7 s | $0.332 | **nothing — OOM** |

## The pipeline: generate remote, upscale local

```bash
python scripts/pipeline.py --batch batches/vertical-example.json          # several clips
python scripts/pipeline.py -w workflows/minimax_h3_i2v_vertical_640x1120_api.json \
       -f input/first_frame_640x1120.png --seeds 11,12,13                 # N takes, one worker
python scripts/pipeline.py --batch batches/vertical-example.json --dry-run  # spend nothing
```

Submits, polls, downloads, upscales locally and prints a cost table.

### Where the money actually goes

Not the upscale — that has run locally since the in-graph `ImageScale` was removed, and
costs nothing. A job's execution time splits roughly:

```
~250 s   staging 50-62 GiB of weights off the network volume
~200-350 s   sampling and decoding
```

The 768x1344 run makes it visible: container start 07:57:43, first memory activity
08:01:50 — **247 s before sampling began**. It is also why 640x1120 measured 488.7 s
against the old 768x1024's 411 s despite having *fewer* pixels: the 411 s was on an
already-warm worker.

**Running clips one at a time pays that staging cost every time.** Jobs sharing a
checkpoint are therefore submitted together; with `workersMax: 1` they queue and run back
to back on one worker. Three clips go from ~3x(250+300) s to ~250+3x300 s — roughly a
third off. Grouping is automatic, keyed on each workflow's `UNETLoader.unet_name`.

Measured 2026-08-09: 5085.3 s billed at $1.585/hr = $2.2393, against ~2328 s of successful
generation. The gap is staging, cold starts, failed runs and idle time.

### Baking weights into the image does not pay — measured, not modelled

Image tag `0.3.0` baked the two VAEs (+5.41 GiB, 22.7 -> 28.1 GiB) purely to measure how
cached image load scales with size. Result:

```
22.7 GiB  ->  41-44 s   (three observations)
28.1 GiB  ->  64 s      (15:32:01 "loading container image from cache"
                         -> 15:33:05 "Loaded image")
```

| Rate | MB/s |
|---|---|
| Image load, **average** over 22.7 GiB | 567 |
| Image load, **marginal** for the 5.41 GiB added | **276** |
| Weight staging from the network volume | **264** |
| Image pull from GHCR, uncached host | ~69 |

**The marginal rate and the volume rate are the same.** Baking 5.41 GiB moved it from a
22.0 s volume read to a 21.0 s image load — a **1 second** saving. Extrapolated:

| Baked | Saving | Penalty on an uncached host |
|---|---|---|
| 5.41 GiB (VAEs) | +1.0 s | +84 s |
| 20.02 GiB (encoder + VAEs) | +3.6 s | +312 s |
| 30.69 GiB (full shared stack) | +5.5 s | +478 s |

So baking is not "roughly break-even" — it is a straight loss. **The earlier estimate that
it saved ~38 s per 20 GiB was wrong**: it compared the image's *average* rate (567 MB/s,
which amortises fixed setup across the whole image) against the volume rate, when what
matters is the *marginal* rate of adding bytes. Load also scaled slightly **worse** than
linearly — 64 s observed against 53.2 s predicted.

Reverted to `0.2.0`. The probe Dockerfile stage is retained, commented out, so the
measurement can be repeated if RunPod's storage characteristics change.

Buildability was never the real obstacle, and two claims made earlier were wrong: RunPod
Pods **can** build images (official Bazel tutorial; `dockerd` runs when the Pod is
privileged), and GitHub Actions can reach **60+ GB** with `easimon/maximize-build-space`
rather than the ~46 GB the hand-rolled cleanup manages. A 53 GiB image was buildable all
along — it simply would not have helped.

**Batching is the lever instead** — one warm worker per batch pays staging once. That is
what `scripts/pipeline.py` does.

### NVFP4 text encoder: rejected, it OOMs where INT8 succeeds

`qwen3vl_32b_minimax_h3_nvfp4_awq` is 14.61 GiB against INT8's 25.28 GiB, so it looked like
a free 10.67 GiB of host RAM — enough to reach the 768x1344 native canvas. ComfyUI supports
it (`quant_format == "nvfp4"`) and `supports_nvfp4_compute` needs compute capability >= 10,
which the RTX 5090's 12.0 clears.

It does not work. Single-variable A/B at 640x1120, seed 7, only `clip_name` changed:

```
14:20:47  42.85 GiB (76 %)   <- loading: LOWER than INT8's ~48 GiB, as expected
14:21:47  52.51 GiB (93 %)   <- sampling: HIGHER than INT8, which completes this config
14:23:47  55.39 GiB (99 %)
          triggered memory limits (OOM)
```

**It saves at load and costs more at compute**, ending in an OOM on the exact configuration
INT8 runs successfully in 407.8 s. The mechanism was not captured — the worker was reaped
to stop the crash-loop billing before its container log was read, which was the wrong order.
A dequantisation fallback would explain it but is unverified.

INT8 stays. The NVFP4 file remains on the volume unused; no workflow references it.

Incidental: staging (container start -> first memory line) was 180 s with the smaller
encoder against 192-247 s with INT8. Directionally consistent with byte-bound staging but
inside the existing noise band, so it does not rescue the baking case either.

### Two things the pipeline does that are easy to forget

1. **Cycles workers to zero between checkpoint groups.** A job asking for a different
   `unet_name` than the worker last loaded OOMs during model load.
2. **Leaves the endpoint at zero workers**, in a `finally` block. A worker left available
   was observed spinning up unprompted with nothing queued, billing for nothing. It also
   drops the worker before the local upscale, which takes minutes.

`--dry-run` prints the grouping, canvas, frame count, seed, payload size and input
existence for every job without touching the endpoint.

### 10 s Ref2VA fits where 10 s I2V would not

Ref2VA loads the **pruned** Ref2VA checkpoint (19.53 GiB) rather than the full FL2VA
(31.70 GiB), so its weight set is 50.22 GiB against I2V's 62.39 GiB. That 12.17 GiB is
exactly the headroom a long clip needs, and it is why memory projections calibrated on
I2V are far too pessimistic for Ref2VA.

Measured: **640x1120, 243 frames (10.125 s), 595.0 s** on the 55.88 GiB tier. The same
frame count on I2V weights would not fit.

243 is on the 17k+5 grid and lands at 10.125 s, which matches a 10.083 s reference audio
almost exactly — worth choosing the frame count from the audio length rather than rounding
to whole seconds.

Reference assets travel in the same `input.images` list as conditioning images:
worker-comfyui base64-decodes each entry into ComfyUI's input directory under the given
name without checking it is an image, so an `.mp3` reaches `LoadAudio` the same way.
`scripts/submit-job.ps1 -Files` takes several.

Note `ref_image_size: "match"` resizes reference images to the generation canvas, so a
landscape reference on a vertical canvas gets squashed — pre-fit references with
`scripts/prep-first-frame.py` exactly as for `first_frame`.

### `drift` cannot tell camera motion from scene wandering

The 10 s Ref2VA scored `drift` 51.52, against 52.02 for the 362-frame clip that was
rejected for wandering — but it is fine. A filmstrip at 0 / 2.5 / 5 / 7.5 / 10 s shows
characters, costumes, colours and room all consistent; what the metric measured was the
slow push-in the prompt asked for.

`drift` is mean |frame[i] - frame[0]|, so any sustained camera move inflates it. Use it to
*flag* clips for inspection, never to condemn them. The 362-frame failure was confirmed by
looking — the room had restyled and the costumes had changed — not by the number alone.

### Worker RAM varies by host, not by GPU tier

The same RTX 5090 type in the same datacenter reported **55.88 GiB** on one host and
**85.68 GiB** on another. The 15 s clip needed 66.29 GiB — it would have OOMed on the
smaller host. Treat long clips and unpruned checkpoints as **not reliably schedulable**:
they depend which machine the worker lands on. This is also why pruned Ref2VA is the right
default even though the unpruned one would fit on a large-RAM host.

## S3 output (Cloudflare R2)

Verified: outputs return `type: "s3_url"` with a working presigned link.

| Variable | Value |
|---|---|
| `BUCKET_ENDPOINT_URL` | `https://<account>.r2.cloudflarestorage.com` — **service root only** |
| `BUCKET_NAME` | `runpod` |
| `AWS_DEFAULT_REGION` | `auto` |
| `BUCKET_ACCESS_KEY_ID` / `BUCKET_SECRET_ACCESS_KEY` | R2 API token |

Object key is `{bucket}/{job_id}/{uuid8}.mp4`; the URL is presigned for **7 days**
(`X-Amz-Expires=604800`).

### Four traps, all of which fail silently

1. **The bucket name is date-derived.** worker-comfyui calls
   `rp_upload.upload_image(job_id, path)` with no bucket, and the SDK falls back to
   `time.strftime("%m-%y")` — writing to a bucket named `08-26`, then `09-26` from
   1 September. There is no `BUCKET_NAME` in the SDK, so the Dockerfile patches the call
   site; the build asserts the patch applied.
2. **`BUCKET_ENDPOINT_URL` must not include the bucket.** boto3 appends the bucket itself,
   so a bucket-qualified endpoint produces `/runpod/runpod/<key>` and NoSuchBucket.
3. **R2 needs `AWS_DEFAULT_REGION`.** The SDK derives the region by string-matching the
   endpoint for `.s3.` or `.digitaloceanspaces.com`. An R2 host matches neither, so it
   passes `region_name=None` and boto3 raises `NoRegionError`. Setting the env var is
   enough; no code change.
4. **RunPod's own S3 storage cannot be used here.** Its API does not support presigned
   URLs, so `put_object` would succeed and the handler would return a link that 403s —
   success reported, output unreachable.

`HEAD` on the returned URL returns 403 and that is correct: it is signed for `get_object`
only. Use `GET`. Content-Type is `image/mp4` because the SDK hardcodes
`"image/" + extension`; harmless for storage, but browsers download rather than preview.

## Outstanding

1. **GHCR package visibility** — the image is private, so workers cannot pull it.
   Flip at
   `https://github.com/users/AkshayLaghate/packages/container/minimax-h3-comfyui/settings`
   → Change visibility → Public. Repo stays private.
2. ~~S3 environment variables~~ — **done**, see below.

## Quota note

RunPod caps total max-workers at 10 across all endpoints. To make room,
`comfy_worker_wan2.2_rapid` (`fplnve432lbwo7`) was reduced from max 2 → 1. It still
works; it just cannot run two jobs concurrently. Reverse with:

```bash
curl -X PATCH https://rest.runpod.io/v1/endpoints/fplnve432lbwo7 \
  -H "Authorization: Bearer $RUNPOD_API_KEY" \
  -H "Content-Type: application/json" -d '{"workersMax":2}'
```
