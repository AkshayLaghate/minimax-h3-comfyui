#!/usr/bin/env python3
"""Submit a job and auto-retry until it lands on a big-RAM worker.

    python scripts/run-on-big-host.py --workflow WF.json --files a.png,b.mp3

WHY
---
Worker RAM is a per-host lottery the API will not let you request. The same RTX 5090
tier hands out either ~55.88 GiB or ~86.61 GiB (RunPod's "60 GB" and "93 GB"). The
unpruned Ref2VA needs the larger one -- measured 67.65 GiB peak at 640x1120 x243f --
and so does anything much above 768x1024. Drawing the small tier OOMs several minutes
in, and the only manual remedy is deleting workers until a big one appears.

LIMITATION, stated plainly
--------------------------
This retries *reactively*, after an OOM. It cannot abort early. The cgroup limit is
only visible in RunPod's system log ("high memory utilization - 46.4GiB / 55.88GiB"),
and there is no scriptable REST path for worker logs -- /v2/endpoints/{id}/workers
redirects to the docs site, /v1/.../workers 400s. Only the MCP tool can read them.
ComfyUI's own "Total VRAM .. total RAM .." line reads /proc/meminfo, which reports the
HOST's memory rather than the container's cgroup limit, so it does not reliably
discriminate either.

So each wrong draw costs a partial run (~$0.15 for Ref2VA, which OOMs around 231 s).
What this buys you is not having to watch for it.
"""
from __future__ import annotations

import argparse, base64, json, os, time, urllib.error, urllib.request

EP = os.environ.get("MINIMAX_ENDPOINT_ID", "t9u2wy8o9ucnpz")
KEY = os.environ["RUNPOD_API_KEY"]
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# The OOM surfaces as a websocket drop, not an explicit out-of-memory error.
OOM_SIGNS = ("ComfyUI HTTP unreachable", "not reachable after multiple retries",
             "Connection to remote host was lost")


def api(url, method="GET", body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Authorization": f"Bearer {KEY}",
                                        "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.load(resp)


def set_workers(mx: int) -> int:
    """PATCH and verify -- an endpoint PATCH has been seen to reset workersMax 1 -> 3,
    which would put several workers on one job, each staging weights separately."""
    for _ in range(3):
        api(f"https://rest.runpod.io/v1/endpoints/{EP}", "PATCH",
            {"workersMax": mx, "workersMin": 0})
        if api(f"https://rest.runpod.io/v1/endpoints/{EP}").get("workersMax") == mx:
            return mx
    return api(f"https://rest.runpod.io/v1/endpoints/{EP}").get("workersMax")


def live_workers() -> int:
    try:
        w = api(f"https://api.runpod.ai/v2/{EP}/health", timeout=30)["workers"]
        return sum(v for k, v in w.items() if isinstance(v, int) and k != "ready")
    except Exception:
        return -1


def submit(wf_path, files) -> str:
    payload = {"input": {"workflow": json.load(open(wf_path, encoding="utf-8"))}}
    if files:
        payload["input"]["images"] = [
            {"name": os.path.basename(f),
             "image": base64.b64encode(open(f, "rb").read()).decode()} for f in files]
    if len(json.dumps(payload)) > 10 * 1024 * 1024:
        raise SystemExit("payload over RunPod's 10 MB /run cap")
    for _ in range(16):
        try:
            return api(f"https://api.runpod.ai/v2/{EP}/run", "POST", payload)["id"]
        except urllib.error.HTTPError as e:
            if "ENDPOINT_PAUSED" in e.read().decode():
                time.sleep(15); continue
            raise
    raise SystemExit("endpoint stayed paused")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workflow", required=True)
    ap.add_argument("--files", default="")
    ap.add_argument("--max-attempts", type=int, default=5)
    ap.add_argument("--out", default=os.path.join(ROOT, "out"))
    a = ap.parse_args()
    files = [os.path.join(ROOT, f) for f in a.files.split(",") if f]

    for n in range(1, a.max_attempts + 1):
        print(f"\n=== attempt {n}/{a.max_attempts} ===", flush=True)
        print(f"  workersMax={set_workers(1)}", flush=True)
        jid = submit(os.path.join(ROOT, a.workflow), files)
        print(f"  job {jid}", flush=True)

        t0, seen = time.time(), None
        while True:
            time.sleep(20)
            st = api(f"https://api.runpod.ai/v2/{EP}/status/{jid}", timeout=30)
            s = st.get("status")
            if s != seen:
                print(f"  [{int(time.time()-t0)}s] {s}", flush=True)
                seen = s
            if s in ("COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"):
                break

        secs = (st.get("executionTime") or 0) / 1000
        if s == "COMPLETED":
            print(f"  COMPLETED exec={secs:.1f}s -- drew a big-RAM worker", flush=True)
            os.makedirs(a.out, exist_ok=True)
            for o in (st.get("output") or {}).get("images") or []:
                if o.get("type") == "s3_url":
                    dest = os.path.join(a.out, os.path.basename(o["filename"]))
                    urllib.request.urlretrieve(o["data"], dest)
                    print(f"  saved {dest} ({os.path.getsize(dest):,} bytes)", flush=True)
            set_workers(0)
            return 0

        err = str(st.get("error"))
        oom = any(k in err for k in OOM_SIGNS)
        print(f"  {s} exec={secs:.1f}s{'  (OOM signature -- small-RAM host)' if oom else ''}",
              flush=True)
        print(f"  {err[:200]}", flush=True)
        if not oom:
            print("  not an OOM -- stopping rather than burning retries", flush=True)
            set_workers(0)
            return 1

        print("  recycling workers before retry", flush=True)
        set_workers(0)
        for _ in range(40):
            if live_workers() == 0:
                break
            time.sleep(5)

    print("\ngave up -- never drew a big-RAM worker", flush=True)
    set_workers(0)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
