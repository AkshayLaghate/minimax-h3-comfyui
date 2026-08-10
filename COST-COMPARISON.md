# MiniMax H3: kie.ai vs WaveSpeed vs self-hosted on RunPod

Compiled 2026-08-10. Our figures are measured from this deployment; provider figures are
their published prices read from the live pages today.

## Verdict

**No single winner — it splits by mode, resolution and volume.**

| Need | Cheapest | Why |
|---|---|---|
| 480p, any volume | **WaveSpeed 480p** ($0.04/s) | $0.20 for 5 s — below our marginal cost. Self-hosting never wins here. |
| 768p I2V, under ~36 clips/mo | **WaveSpeed 768p** ($0.10/s) | 11% under kie.ai |
| **Ref2VA** | **kie.ai** ($0.1125/s) | Reference audio is free; WaveSpeed charges $0.125/s **plus** $0.02/audio and $0.02/image |
| 2K | **WaveSpeed 2K** ($0.14/s) | 23% under kie.ai's $0.1825/s. Neither is reproducible here at any price. |
| 768p at volume, or long clips | **self-host** | Our per-second cost falls with length; theirs never does |

## Provider pricing

### kie.ai

1 credit = **$0.005** exactly — verified across 11 rows and 4 model families
(`USD ÷ credits = 0.0050` every time; Qwen image at 12 credits = $0.06 confirms it).
Billing is `Unit Price × (generated + input video duration) + extra images`.
**Input audio free; first 5 images free**, then $0.055 each.

| Tier | Credits/s | USD/s | Their quoted Fal price | Discount |
|---|---|---|---|---|
| 768p — T2V / I2V / Ref2VA / V2V | 22.5 | **$0.1125** | $0.18 | −37.5% |
| 2K — same four modes | 36.5 | **$0.1825** | $0.26 | −29.8% |

Flat across modes — reference conditioning is not surcharged.

### WaveSpeed

Splits exactly along the line our own research found: `wavespeed-ai/minimax-h3/*` are
labelled **"Open Weights"** and cap at 768p — the same checkpoint we run — while
`minimax/h3/*` is the official API passthrough that reaches 2K.

| Endpoint | 480p | 768p | 2K |
|---|---|---|---|
| `wavespeed-ai/…/image-to-video` (open weights) | **$0.04/s** | **$0.10/s** | — |
| `wavespeed-ai/…/reference-to-video` (open weights) | **$0.05/s** | **$0.125/s** | — |
| `minimax/h3/image-to-video` (official) | — | $0.10/s | **$0.14/s** |

Ref2VA extras: **$0.02 per reference image, $0.02 per reference audio**, reference video
billed at the output rate.

Verified by internal consistency on the live pages: `$0.10 × 5 = $0.50`, `× 10 = $1.00`,
`× 15 = $1.50`; `$0.14 × 15 = $2.10`. All as printed.

## Our measured cost

RTX 5090 serverless at **$1.585/hr** (from billing records, not the $0.69/hr pod rate —
serverless bills 2.30×). Figures below are execution time × **1.241**, the observed ratio
of billed to executed seconds on 2026-08-09 (image load, container start, 60 s idle,
crash-loop billing after failures).

| Clip | Video | GPU s | Cost | $/video-second |
|---|---|---|---|---|
| I2V 640×1120 | 5.17 s | 489 | **$0.267** | $0.0517 |
| Ref2VA 640×1120 | 10.13 s | 595 | **$0.325** | $0.0321 |
| I2V stitched ×2 | 14.58 s | 770 | **$0.421** | $0.0288 |

Plus **$8.40/month** for the 120 GB volume, paid whether or not anything is generated.

Note our output is **640×1120** — a 640 px short edge, sitting *between* WaveSpeed's 480p
and 768p tiers. We cannot reach 768p at 124 frames on the 55.88 GiB RAM tier with the full
checkpoint, so the fair comparison is arguably nearer their 480p price than their 768p one.

## Head to head, I2V, marginal cost only

| Duration | Ours | WS 480p | WS 768p | kie 768p | WS 2K | kie 2K |
|---|---|---|---|---|---|---|
| 5 s | **$0.267** | $0.200 | $0.500 | $0.5625 | $0.700 | $0.9125 |
| 10 s | **$0.325** | $0.400 | $1.000 | $1.1250 | $1.400 | $1.8250 |
| 15 s | **$0.421** | $0.600 | $1.500 | $1.6875 | $2.100 | $2.7375 |

Against the cheapest 768p provider (WaveSpeed): they cost **1.87× at 5 s, 3.08× at 10 s,
3.57× at 15 s**.

**WaveSpeed 480p at 5 s ($0.200) is cheaper than our marginal cost ($0.267)** — before the
volume is even counted. At that resolution self-hosting cannot win.

## Ref2VA is the exception

Our real job — 10 s, one reference image, one reference audio:

| | Cost | Note |
|---|---|---|
| **Ours** | **$0.325** | |
| WaveSpeed 480p | $0.540 | 10×$0.05 + $0.02 + $0.02 |
| **kie.ai 768p** | **$1.125** | audio and first 5 images free |
| WaveSpeed 768p | $1.290 | 10×$0.125 + $0.02 + $0.02 |

**kie.ai beats WaveSpeed on Ref2VA** despite losing on I2V, because WaveSpeed charges a
premium for the reference-to-video endpoint ($0.125 vs $0.10) *and* meters audio. If
Ref2VA is a large share of the workload, that reverses the provider choice.

## Break-even, including the $8.40/month volume

| Against | 5 s clips | 10 s clips |
|---|---|---|
| WaveSpeed 480p | **never** — cheaper than our marginal cost | ~112/month |
| WaveSpeed 768p | ~36/month | ~12/month |
| kie.ai 768p | ~28/month | ~11/month |

Longer clips shift break-even down sharply, because our fixed staging cost amortises while
provider pricing stays linear.

## Why our per-second cost falls and theirs does not

Roughly **250 s of every job** stages 50–62 GiB of weights off the network volume before
sampling begins; only the remainder scales with clip length. So our per-second cost drops
from $0.052 at 5 s to $0.029 at 15 s, while every provider charges a flat rate per second
forever. This is also why `scripts/pipeline.py` batches: several clips on one warm worker
pay that 250 s once rather than once each.

## What the money does not capture

**For the providers**

- **True 2K is unreachable here at any price.** The open checkpoint is 768p-class;
  MiniMax's 2K comes from `H3-Regenerate-2K`, never open-sourced. Our 1080×1920 is a
  640×1120 render upscaled with Real-ESRGAN.
- **No capacity risk.** We were throttled waiting for 5090s twice on 2026-08-09, once for
  ~30 minutes.
- **No operational burden.** 6 of ~11 job attempts that day failed — OOMs, a poisoned
  queue, a crash-looping worker — and ~$0.90 of the day's $2.24 bought nothing.
- **No standing fee.**

**For self-hosting**

- **Marginal cost collapses at volume and length** — $0.421 for 14.58 s against $1.50.
- **Total control**: seeds, samplers, EasyCache, frame counts on the 17k+5 grid, chained
  conditioning frames. The stitched 14.58 s clip is not something an endpoint exposes.
- **Upscaling is free** on the local RTX 3060.
- **Rejected takes cost ~$0.27, not full price.**

## Recommendation

1. **Move Ref2VA to kie.ai** for one-offs — free audio makes it the cheapest hosted route
   for our exact use case.
2. **Use WaveSpeed for hosted I2V** — 11% under kie.ai at 768p, 23% under at 2K, and it is
   the only one offering 480p.
3. **Self-host for volume above ~30 clips/month and for anything over 10 s**, always
   batched.
4. **If volume will stay under ~30 clips/month, delete the network volume** ($8.40/month)
   and buy from WaveSpeed. Two 5 s clips a month costs $1.00 hosted against $8.93 self-hosted.

Reality check: the two clips delivered on 2026-08-09 (5.17 s I2V + 10.13 s Ref2VA) would
have cost **$1.63** on WaveSpeed 768p, **$1.69** on kie.ai, or **$0.74** on WaveSpeed 480p.
Their marginal cost here was **$0.59** — but the day billed **$2.24** including the
experimentation around them.

## Sources and caveats

- kie.ai read from <https://kie.ai/pricing> (blocks automated fetches; read from the
  rendered page). WaveSpeed read from the live model pages.
- **The WaveSpeed blog is stale**: it quotes 2K at $0.13/s and reference video at $0.09/s;
  the live model pages say **$0.14** and **$0.125**. Trust the model pages.
- Provider durations are integer seconds (WaveSpeed supports 4–15). Our clips are 5.17 /
  10.13 / 14.58 s off the 17k+5 grid; provider columns use nominal 5 / 10 / 15.
- The 1.241 overhead multiplier comes from a single heavy-experimentation day; a steadier
  day lands nearer the floor ($0.215 / $0.262 / $0.339).
- Neither provider publishes bulk discounts. Prices on all three sides move — re-derive
  before any standing commitment.
