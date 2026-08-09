#!/usr/bin/env python3
"""Generate on RunPod, upscale locally. One command, end to end.

    python scripts/pipeline.py --batch batches/vertical.json
    python scripts/pipeline.py -w workflows/minimax_h3_i2v_vertical_640x1120_api.json \
                               -f input/first_frame_640x1120.png --seeds 11,12,13

WHY THIS EXISTS
---------------
A job's execution time splits roughly as:

    ~250 s   staging 50-62 GiB of weights off the network volume
    ~200-350 s   actually sampling and decoding

Running clips one at a time pays the staging cost *every single time* — it is over
half the bill. It is also why 640x1120 measured 488.7 s against the old 768x1024's
411 s despite having fewer pixels: that 411 s was on an already-warm worker.

So jobs that share a checkpoint are submitted together. With workersMax=1 they queue
and execute back to back on one worker, staging the weights once. Three clips go from
~3x(250+300) s to ~250+3x300 s — roughly a third off.

Upscaling never runs on the worker. An in-graph ImageScale allocates a second
full-resolution float32 batch (3.09 GB at 124 frames) and was the direct cause of the
OOMs documented in DEPLOYMENT.md. Lanczos and ESRGAN both run here for free.

Between checkpoints the endpoint is cycled to zero workers: a job asking for a
different `unet_name` than the worker last loaded OOMs during model load, because
safetensors read off the volume sit in page cache that counts against the container's
memory limit.

Requires RUNPOD_API_KEY in the environment.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENDPOINT = os.environ.get("MINIMAX_ENDPOINT_ID", "t9u2wy8o9ucnpz")
KEY = os.environ.get("RUNPOD_API_KEY")
RATE_PER_HR = 1.585                      # RTX 5090 serverless, from billing records
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}
ESRGAN = os.environ.get(
    "ESRGAN_MODEL",
    r"C:/roop-unleashed_win_v2.7.0/roop-unleashed/roop-unleashed/models/CodeFormer/realesrgan/RealESRGAN_x2plus.pth",
)


def log(msg: str) -> None:
    print(msg, flush=True)


# ---------------------------------------------------------------- RunPod API

def _req(url: str, method: str = "GET", body: dict | None = None, timeout: int = 60):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Authorization": f"Bearer {KEY}",
                                        "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.load(resp)


def set_max_workers(n: int) -> None:
    _req(f"https://rest.runpod.io/v1/endpoints/{ENDPOINT}", "PATCH", {"workersMax": n})


def worker_count() -> int:
    """Total workers in any state. /health is the stable public endpoint for this —
    the REST v1 /endpoints/{id}/workers path does not exist and v2 redirects to docs."""
    try:
        w = _req(f"https://api.runpod.ai/v2/{ENDPOINT}/health", timeout=30)["workers"]
    except Exception:
        return -1
    # "ready" overlaps idle/running rather than being a separate pool, so counting it
    # double-reports. Exclude it.
    return sum(v for k, v in w.items() if isinstance(v, int) and k != "ready")


def cycle_workers() -> None:
    """Scale to zero and wait for reaping, so the next job loads its checkpoint clean."""
    log("  cycling workers to zero (checkpoint change)")
    set_max_workers(0)
    for _ in range(40):
        if worker_count() == 0:
            break
        time.sleep(5)
    set_max_workers(1)
    # The un-pause takes 30-60 s to propagate; submit() retries on ENDPOINT_PAUSED.


def submit(workflow: dict, files: list[str]) -> str:
    payload = {"input": {"workflow": workflow}}
    if files:
        payload["input"]["images"] = [
            # worker-comfyui base64-decodes each entry into ComfyUI's input directory
            # under the given name without checking it is an image, which is how a
            # reference .mp3 reaches LoadAudio.
            {"name": os.path.basename(f),
             "image": base64.b64encode(open(f, "rb").read()).decode()}
            for f in files
        ]
    blob = json.dumps(payload)
    if len(blob) > 10 * 1024 * 1024:
        raise SystemExit(f"payload {len(blob)/1e6:.1f} MB exceeds RunPod's 10 MB /run cap")

    for attempt in range(12):
        try:
            return _req(f"https://api.runpod.ai/v2/{ENDPOINT}/run", "POST", payload)["id"]
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:200]
            if "ENDPOINT_PAUSED" in detail and attempt < 11:
                time.sleep(15)
                continue
            raise SystemExit(f"submit failed: {detail}")
    raise SystemExit("submit failed: endpoint stayed paused")


def poll(jobs: dict[str, str]) -> dict[str, dict]:
    """jobs: name -> job id. Returns name -> final status payload."""
    final, seen = {}, {}
    start = time.time()
    while len(final) < len(jobs):
        for name, jid in jobs.items():
            if name in final:
                continue
            try:
                d = _req(f"https://api.runpod.ai/v2/{ENDPOINT}/status/{jid}", timeout=30)
            except Exception as e:
                log(f"  [{int(time.time()-start)}s] {name}: poll error {type(e).__name__}")
                continue
            st = d.get("status", "?")
            if st != seen.get(name):
                log(f"  [{int(time.time()-start)}s] {name}: {st}")
                seen[name] = st
            if st in TERMINAL:
                final[name] = d
        if len(final) < len(jobs):
            time.sleep(20)
    return final


# ---------------------------------------------------------------- local steps

def download(status: dict, dest_dir: str, name: str) -> list[str]:
    out = []
    for o in (status.get("output") or {}).get("images") or []:
        if o.get("type") != "s3_url":
            log(f"  !! {name}: unexpected output type {o.get('type')} — S3 env vars unset?")
            continue
        dest = os.path.join(dest_dir, f"{name}_native.mp4")
        urllib.request.urlretrieve(o["data"], dest)
        log(f"  saved {dest} ({os.path.getsize(dest):,} bytes)")
        out.append(dest)
    return out


def upscale(src: str, dst: str, method: str) -> bool:
    if method == "none":
        return True
    if method == "esrgan":
        cmd = [sys.executable, os.path.join(ROOT, "scripts", "upscale-esrgan.py"),
               src, dst, "--model", ESRGAN]
    else:
        cmd = ["bash", os.path.join(ROOT, "scripts", "upscale-to-1080.sh"), src, dst]
    log(f"  upscaling ({method}) -> {os.path.basename(dst)}")
    return subprocess.run(cmd).returncode == 0


# ---------------------------------------------------------------- orchestration

def checkpoint_of(wf: dict) -> str:
    for node in wf.values():
        if node.get("class_type") == "UNETLoader":
            return node["inputs"]["unet_name"]
    return "?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", help="JSON list of {name, workflow, files, seed}")
    ap.add_argument("-w", "--workflow")
    ap.add_argument("-f", "--files", default="", help="comma-separated inputs")
    ap.add_argument("--seeds", default="", help="comma-separated; one clip per seed")
    ap.add_argument("--name", default="clip")
    ap.add_argument("--out", default=os.path.join(ROOT, "out"))
    ap.add_argument("--upscale", choices=["esrgan", "lanczos", "none"], default="esrgan")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the plan and payload sizes; touch nothing and spend nothing")
    a = ap.parse_args()

    if not KEY:
        return "RUNPOD_API_KEY is not set"

    if a.batch:
        spec = json.load(open(a.batch, encoding="utf-8"))
    elif a.workflow:
        files = [x for x in a.files.split(",") if x]
        seeds = [int(s) for s in a.seeds.split(",") if s] or [None]
        spec = [{"name": f"{a.name}_s{s}" if s is not None else a.name,
                 "workflow": a.workflow, "files": files, "seed": s} for s in seeds]
    else:
        return "give --batch or --workflow"

    # Load workflows and apply seed overrides, then group by checkpoint so each
    # group runs back to back on one warm worker.
    groups: dict[str, list[dict]] = {}
    for job in spec:
        wf = json.load(open(os.path.join(ROOT, job["workflow"]), encoding="utf-8"))
        if job.get("seed") is not None:
            for node in wf.values():
                if node.get("class_type") == "RandomNoise":
                    node["inputs"]["noise_seed"] = job["seed"]
        job["_wf"] = wf
        job["_files"] = [os.path.join(ROOT, f) for f in job.get("files", [])]
        groups.setdefault(checkpoint_of(wf), []).append(job)

    os.makedirs(a.out, exist_ok=True)
    log(f"{len(spec)} job(s) in {len(groups)} checkpoint group(s)")

    if a.dry_run:
        for ckpt, jobs in groups.items():
            log(f"\n== {ckpt}  ({len(jobs)} job(s), one warm worker)")
            for job in jobs:
                n5 = next((x for x in job["_wf"].values()
                           if str(x.get("class_type", "")).startswith("MiniMaxH3")), {})
                i = n5.get("inputs", {})
                seed = next((x["inputs"]["noise_seed"] for x in job["_wf"].values()
                             if x.get("class_type") == "RandomNoise"), "?")
                mb = len(json.dumps({"input": {"workflow": job["_wf"], "images": [
                    {"name": os.path.basename(f),
                     "image": base64.b64encode(open(f, "rb").read()).decode()}
                    for f in job["_files"]]}})) / 1e6
                log(f"   {job['name']:<22} {i.get('width')}x{i.get('height')} "
                    f"{i.get('length')}f ({(i.get('length') or 0)/24:.2f}s) seed={seed} "
                    f"payload={mb:.2f} MB")
                for f in job["_files"]:
                    log(f"       {'OK ' if os.path.exists(f) else 'MISSING'} {f}")
        log(f"\ndry run: nothing submitted. upscale would be '{a.upscale}' (local, free).")
        return 0

    results, t0 = [], time.time()
    try:
        for ckpt, jobs in groups.items():
            log(f"\n== {ckpt}  ({len(jobs)} job(s))")
            cycle_workers()

            ids = {}
            for job in jobs:
                ids[job["name"]] = submit(job["_wf"], job["_files"])
                log(f"  submitted {job['name']} -> {ids[job['name']]}")

            final = poll(ids)

            # Release the worker before the local upscales, which take minutes and
            # would otherwise bill idle time. Downloads use presigned S3 URLs, so
            # they do not need the worker alive.
            set_max_workers(0)

            for job in jobs:
                st = final[job["name"]]
                secs = (st.get("executionTime") or 0) / 1000
                if st.get("status") != "COMPLETED":
                    log(f"  FAILED {job['name']}: {str(st.get('error'))[:200]}")
                    results.append({"name": job["name"], "ok": False, "exec_s": secs})
                    continue
                natives = download(st, a.out, job["name"])
                finals = []
                for n in natives:
                    dst = n.replace("_native.mp4", "_1080x1920.mp4")
                    finals.append(dst if upscale(n, dst, a.upscale) else None)
                results.append({"name": job["name"], "ok": True, "exec_s": secs,
                                "native": natives, "final": [f for f in finals if f]})
    finally:
        # Always leave the endpoint at zero. A worker left available can spin up
        # unprompted and bill with nothing queued — observed in practice.
        try:
            set_max_workers(0)
            log("\nendpoint scaled to 0 workers")
        except Exception as e:
            log(f"\n!! could not scale endpoint to 0: {e} — check it manually")

    wall = time.time() - t0
    gpu_s = sum(r["exec_s"] for r in results)
    log("\n" + "=" * 68)
    log(f"{'clip':<26}{'status':>10}{'GPU s':>10}{'est cost':>12}")
    for r in results:
        log(f"{r['name']:<26}{('ok' if r['ok'] else 'FAILED'):>10}"
            f"{r['exec_s']:>10.1f}{'$%.4f' % (r['exec_s']/3600*RATE_PER_HR):>12}")
    log("-" * 68)
    log(f"{'TOTAL':<26}{'':>10}{gpu_s:>10.1f}{'$%.4f' % (gpu_s/3600*RATE_PER_HR):>12}")
    log(f"wall clock {wall/60:.1f} min; upscaling ({a.upscale}) ran locally at no GPU cost")
    log("Billed time also includes container start and the 60 s idle timeout, so the")
    log("real figure is higher; check /v1/billing/endpoints once records post.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
