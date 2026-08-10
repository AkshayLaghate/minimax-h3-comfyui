# MiniMax H3: kie.ai vs self-hosted on RunPod

Compiled 2026-08-10. Our figures are measured from this deployment, not modelled;
kie.ai's are their published prices as of today.

## Verdict

**Below ~27 clips a month, kie.ai is cheaper. Above it, we are — and the gap widens
fast with clip length.** At 100 clips/month we cost $0.35 against their $0.58.

But cost is not the whole decision: **kie.ai can produce true 2K, which our weights
cannot do at all.** The open FL2VA checkpoint is 768p-class; MiniMax's 2K path is
`H3-Regenerate-2K`, an upscaling module they did not open-source. Our 1080×1920 is a
640×1120 render upscaled locally with Real-ESRGAN — competent, but not the same thing.

## kie.ai published pricing

Billing is `Unit Price × (generated duration + input video duration) + extra images`.
Input **audio is free**; the first **5 input images are free**, then $0.055 each.
1 credit = $0.005 (22.5 credits = $0.1125). No bulk credit tiers are published.

| Tier | Credits/s | USD/s | Their quoted Official/Fal price | Discount |
|---|---|---|---|---|
| 768p — text/image/reference/video-to-video | 22.5 | **$0.1125** | $0.18 | −37.5% |
| 2K — text/image/reference/video-to-video | 36.5 | **$0.1825** | $0.26 | −29.8% |

Notably the price is flat across T2V / I2V / Ref2VA — reference conditioning is not
surcharged, and neither is our most-used mode.

## Our measured cost

RTX 5090 serverless at **$1.585/hr** (from `/v1/billing/endpoints`, not the pod rate).

- **floor** = GPU execution time only — what a perfectly batched warm worker costs.
- **realistic** = floor × 1.241, the observed ratio of billed time (5085.3 s) to summed
  execution time (4098.1 s) on 2026-08-09. The gap is image load, container start, the
  60 s idle timeout and crash-loop billing after failures.

| Run (measured) | Video | GPU s | Floor | Realistic | $/video-second |
|---|---|---|---|---|---|
| I2V 640×1120, best | 5.17 s | 408 | $0.1795 | $0.2228 | $0.0431 |
| I2V 640×1120, worst | 5.17 s | 489 | $0.2152 | $0.2670 | $0.0517 |
| I2V 768×1024 baseline | 5.17 s | 411 | $0.1810 | $0.2246 | $0.0435 |
| Ref2VA 640×1120 | 10.13 s | 595 | $0.2620 | $0.3251 | $0.0321 |
| I2V stitched ×2 | 14.58 s | 770 | $0.3390 | $0.4207 | $0.0288 |

Plus a standing **$8.40/month** for the 120 GB network volume, paid whether or not
anything is generated.

## Head to head, marginal cost only

| Clip | Ours (realistic) | kie.ai 768p | kie.ai 2K | They cost |
|---|---|---|---|---|
| 5.17 s I2V | $0.2670 | $0.5813 | $0.9430 | **2.2×** more |
| 10.13 s Ref2VA | $0.3251 | $1.1391 | $1.8478 | **3.5×** more |
| 14.58 s stitched | $0.4207 | $1.6406 | $2.6614 | **3.9×** more |

## Why the multiple grows with length

The two cost structures have different shapes:

- **kie.ai is purely linear** — every second costs $0.1125, forever.
- **Ours is fixed-cost dominated.** Roughly 250 s of every job stages 50–62 GiB of
  weights off the network volume before sampling starts; only the remainder scales with
  clip length. Our per-second cost therefore *falls* from $0.052 at 5 s to $0.029 at 15 s.

So the longer the clip, the better we look. On short clips their linear pricing is
competitive; on long ones it is not close.

The same logic is why `scripts/pipeline.py` batches: several clips on one warm worker pay
that ~250 s once instead of once each, pushing our floor down further.

## Break-even on volume

Marginal cost $0.2670/clip against their $0.5813, with $8.40/month of volume to absorb:

| Clips/month | Ours all-in | kie.ai | Cheaper |
|---|---|---|---|
| 5 | $1.9470 | $0.5813 | kie.ai |
| 10 | $1.1070 | $0.5813 | kie.ai |
| 25 | $0.6030 | $0.5813 | kie.ai |
| **~27** | **$0.5813** | **$0.5813** | **break-even** |
| 50 | $0.4350 | $0.5813 | ours |
| 100 | $0.3510 | $0.5813 | ours |
| 200 | $0.3090 | $0.5813 | ours |

Break-even falls if clips are longer: at 10 s the marginal gap is $0.81/clip, so the
volume is absorbed by **~11 clips/month**.

## What the money does not capture

**In kie.ai's favour**

- **True 2K.** We cannot produce it. Our ceiling is a 768 px short edge (1.03 MP), and in
  practice 640×1120 because the 55.88 GiB RAM tier cannot hold the full checkpoint at
  larger canvases. Their 2K at $0.1825/s has no equivalent here at any price.
- **No capacity risk.** We were throttled waiting for 5090s twice on 2026-08-09, once for
  ~30 minutes. Throttling does not bill, but it does block.
- **No operational burden.** Of ~11 job attempts on 2026-08-09, 6 failed — OOMs, a
  poisoned queue, a crash-looping worker. Roughly $0.90 of the day's $2.24 bought nothing.
  That was experimentation rather than steady state, but it is the real cost of owning it.
- **No standing fee**, no volume to keep populated, no image to rebuild.

**In our favour**

- **Marginal cost collapses at volume** — $0.309/clip at 200/month, half their price.
- **Total control**: seeds, prompts, samplers, EasyCache, frame counts, checkpoints. The
  stitched 14.58 s clip and the 10.13 s Ref2VA were only possible because we could pick
  frame counts on the 17k+5 grid and chain conditioning frames.
- **Upscaling is free** — Real-ESRGAN on the local RTX 3060 costs nothing per clip.
- **No per-clip cost on failures** beyond GPU seconds; a rejected take costs ~$0.22, not
  a full-price generation.

## Recommendation

**Use both, split by job.**

- **kie.ai for anything needing 2K**, for one-off or low-volume work, and for bursts where
  our 5090 capacity is throttled. Below ~27 clips/month it is simply cheaper, and it is
  the only route to genuine 2K.
- **Self-host for volume and for long clips**, where the fixed staging cost amortises and
  the 2.2–3.9× gap compounds. Always batch through `scripts/pipeline.py`.

A concrete reality check: the two delivered clips on 2026-08-09 (5.17 s I2V + 10.13 s
Ref2VA) would have cost **$1.72** on kie.ai at 768p. Their marginal cost here was
**$0.59** — but the day actually billed **$2.24** because of the experimentation around
them. Steady state favours us; discovery does not.

If the volume is not going to reach ~27 clips/month, the honest move is to delete the
network volume ($8.40/month) and buy from kie.ai.

## Sources and caveats

- kie.ai pricing read directly from <https://kie.ai/pricing> on 2026-08-10. It blocks
  automated fetches (HTTP 403); figures were read from the rendered page.
- Third-party cross-check: WaveSpeed quotes MiniMax H3 2K at $0.13/s, *below* kie.ai's
  $0.1825/s — so kie.ai is not the cheapest reseller and the market is worth re-checking
  before committing.
- Our $1.585/hr is measured from billing records, not the advertised pod rate (which is
  $0.69/hr — serverless bills 2.30× that).
- The 1.241 overhead multiplier comes from a single day that included heavy
  experimentation; a steadier day would land closer to the floor.
- Prices on both sides move. Re-derive before making a standing commitment.
