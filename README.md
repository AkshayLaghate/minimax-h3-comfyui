# MiniMax H3 on RunPod Serverless

A [worker-comfyui](https://github.com/runpod-workers/worker-comfyui) deployment of
[MiniMax H3](https://huggingface.co/Comfy-Org/MiniMax-H3) (FL2VA) — text-to-video and
image-to-video with native 32 kHz stereo audio — as a RunPod serverless endpoint.

| | |
|---|---|
| Diffusion model | `minimax_h3_fl2va_int8_convrot.safetensors` (31.70 GiB) |
| Text encoder | `qwen3vl_32b_minimax_h3_int8_convrot.safetensors` (25.28 GiB) |
| VAEs | `minimax_h3_video_vae_fp16` (4.85 GiB) + `minimax_h3_audio_vae_fp32` (0.56 GiB) |
| ComfyUI | v0.30.0 — the release that added MiniMax H3 |
| Worker image | 22.7 GiB on disk / ~11 GiB compressed (no weights) |
| Weights | ~62.4 GiB on a RunPod network volume |
| GPU | 80 GB tier — H100 / A100 80GB / H200 |
| Output | `.mp4` (H.264 + AAC) via S3 |

## Architecture, and why it looks like this

Weights live on a **network volume**, not baked into the image. That was forced by a
hard platform constraint: RunPod Pods cannot run a Docker daemon — *"you can't directly
build Docker containers or use Docker Compose on a GPU Pod"* ([RunPod
docs](https://docs.runpod.io/tutorials/pods/build-docker-images)) — so there is nowhere
on RunPod to assemble a ~75 GiB baked image. Off-platform builders don't rescue it
either: RunPod's own GitHub integration caps images at 80 GB with a 30-minute
`docker build` timeout, and a stock CI runner has nothing like the disk for it.

Splitting weights onto a volume makes the image small enough to build on a free GitHub
Actions runner, and makes adding Ref2VA later a download rather than a rebuild.

**The image is custom-built, not pulled.** Every published
`runpod/worker-comfyui:*-base` tag predates the model: release 5.8.6 shipped
2026-06-17, while ComfyUI v0.30.0 — the first release containing
`comfy_extras/nodes_minimax_h3.py` — landed 2026-08-03. So
[`.github/workflows/build.yml`](.github/workflows/build.yml) builds upstream's `base`
target from pinned SHA `a1981e99` with `COMFYUI_VERSION=0.30.0`, then stacks a thin
overlay that swaps in our `extra_model_paths.yaml`. The Dockerfile asserts the H3 node
file exists, so a stale base fails the build instead of a live worker.

## What did *not* need changing

Worth knowing, because each looks like a blocker and isn't:

- **No custom nodes.** `MiniMaxH3ImageToVideo`, `MiniMaxH3ReferenceToVideo`,
  `MiniMaxH3SigmaShift` and `EmptyMiniMaxH3LatentAV` are ComfyUI core, as are
  `ResolutionSelector` and `ComfyMathExpression`.
- **No handler patch for video.** worker-comfyui's handler only iterates
  `node_output["images"]`, which looks like it would drop video — but `SaveVideo`
  returns `ui.PreviewVideo`, whose `as_dict()` is `{"images": [...], "animated": (True,)}`.
  The `.mp4` comes back through the existing path, extension and all. (Upstream issue
  [#232](https://github.com/runpod-workers/worker-comfyui/issues/232) is polish, not
  capability.)
- **No Hugging Face token.** `Comfy-Org/MiniMax-H3` is public and ungated.
- **No extra pip installs.** INT8 *convrot* dispatches to `quant_ops.ck.int8_linear`,
  and `ck` is `comfy-kitchen`, already pinned in ComfyUI's `requirements.txt`.

The one thing that *did* need patching is `extra_model_paths.yaml` — see below.

## Layout

```
Dockerfile                        thin overlay on the custom base
extra_model_paths.yaml            points ComfyUI at /runpod-volume
.github/workflows/build.yml       two-step image build -> GHCR
scripts/populate-volume.sh        run on a Pod to fill the network volume
scripts/validate-workflows.py     schema / frame-grid checks
scripts/test-endpoint.ps1         submit a job, resolve the S3 URL
workflows/minimax_h3_t2v_api.json text-to-video, API format
workflows/minimax_h3_i2v_api.json image-to-video (first frame), API format
test_input.json                   generated from the T2V workflow
```

### The `extra_model_paths.yaml` trap

Upstream maps only the legacy `unet:` and `clip:` keys. ComfyUI's
`folder_paths.map_legacy()` rewrites those key *names* to `diffusion_models` and
`text_encoders`, but the *paths* it registers stay `models/unet/` and `models/clip/`.
A volume laid out to mirror the Hugging Face repo would therefore be silently invisible —
an empty `UNETLoader` dropdown rather than an error. Our copy adds the two explicit keys.

## Deploy

**1. Build the image** — Actions tab → *Build worker image* → *Run workflow*.
Produces `ghcr.io/<owner>/minimax-h3-comfyui:0.1.0`. Takes ~20 min, most of it the base
build. GHCR requires the image name to be lowercase, which the workflow handles.

Because this repo is private the package is too, so either make the package public
(Packages → minimax-h3-comfyui → Package settings → Change visibility) or add GHCR
credentials to RunPod under Settings → Container Registry Auth. The image holds only
ComfyUI — no weights, no secrets.

**2. Create the network volume** — ≥ **80 GB**, in a datacenter that has 80 GB GPUs.
The endpoint is pinned to the volume's datacenter, so this choice constrains GPU
availability.

**3. Populate it** — start a cheap Pod with the volume attached, then:

```bash
bash scripts/populate-volume.sh
```

Downloads ~62.4 GiB, verifies every file against its exact byte size, and resumes if
interrupted. Terminate the Pod afterwards.

**4. Create the endpoint:**

| Setting | Value | Why |
|---|---|---|
| Image | `ghcr.io/<owner>/minimax-h3-comfyui:0.1.0` | |
| Network volume | the one from step 2 | Mounts at `/runpod-volume` |
| GPU | 80 GB — select several types | ~62 GiB of weights plus activations |
| Container disk | 40 GB | The image unpacks to 22.7 GiB; 20 GB will fail to pull |
| Execution timeout | well above the 600 s default | A 768p clip exceeds 10 minutes |
| Idle timeout | 60–120 s | Avoids re-reading weights from the volume |
| Env | `BUCKET_ENDPOINT_URL`, `BUCKET_ACCESS_KEY_ID`, `BUCKET_SECRET_ACCESS_KEY` | |

**Set the S3 variables.** Without them the handler falls back to base64 and hits
RunPod's hard 10 MB (`/run`) / 20 MB (`/runsync`) response cap — which surfaces as a
confusing truncated response rather than a clear error. Video output effectively
requires S3.

## Test

```powershell
$env:RUNPOD_API_KEY = "..."
.\scripts\test-endpoint.ps1 -EndpointId <id> -Workflow .\workflows\minimax_h3_t2v_api.json
```

Start with the shipped defaults (640×384, 124 frames ≈ 5 s) to separate "the stack
works" from "the settings are too heavy". 256p is documented as failing; 384p is the floor.

Success is `output.images[0]` with a `.mp4` filename and `type: "s3_url"`. A
`type: "base64"` means the S3 variables did not take.

Then confirm the audio actually rendered — this is what proves the
`VAEDecodeAudio` → `CreateVideo` → `SaveVideo` chain ran, rather than just that a file
appeared:

```bash
ffprobe -hide_banner -show_streams <url> 2>&1 | grep -E "codec_type|codec_name|sample_rate|channels"
```

Expect a video stream plus a 32 kHz stereo audio stream.

## Workflow notes

The upstream ComfyUI templates are UI-format and wrap the pipeline in a **subgraph**, so
they cannot be POSTed as-is. The workflows here are equivalents in API format, rebuilt
from the subgraph's internal links, with two deliberate simplifications:

- `ResolutionSelector` dropped in favour of explicit `width`/`height`.
- `ComfyMathExpression` + `PrimitiveFloat` (duration in seconds → frame count) dropped in
  favour of an explicit `length`.

Both make the workflow parameterisable over HTTP, which is the point of an API
deployment. The constraints they encoded are enforced by the validator instead:

- `length` must sit on the **17k+5 grid** (5, 22, 39, … 124, 141, …). 124 ≈ 5 s at
  24 fps; the trained range is roughly 124–362.
- `width` and `height` must be multiples of 32.

```bash
python scripts/validate-workflows.py
```

## Adding Ref2VA later

Add `diffusion_models/minimax_h3_ref2va_int8_convrot.safetensors` (31.70 GiB) to
`scripts/populate-volume.sh`, size the volume to ~120 GB, and re-run it on a Pod. No
image rebuild — which is the main dividend of the volume architecture.
