#!/usr/bin/env python3
"""Create a campaign through the HTTP API and follow it until it stops.

    python scripts/run_campaign.py --base http://127.0.0.1:8008 --count 12 --engine vina

Used for acceptance runs; prints real state from the API, nothing simulated.
"""
import argparse
import json
import sys
import time
import urllib.request


def call(base, path, body=None, method=None):
    req = urllib.request.Request(
        base + path, method=method or ("POST" if body is not None else "GET"),
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode()[:400]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8008")
    ap.add_argument("--engine", default="vina")
    ap.add_argument("--count", type=int, default=12)
    ap.add_argument("--batch-size", type=int, default=4)
    ap.add_argument("--exhaustiveness", type=int, default=8)
    ap.add_argument("--source", default="zinc20")
    ap.add_argument("--no-hyde", action="store_true")
    ap.add_argument("--grow", action="store_true", help="also generate FastGrow analogues of the top candidates")
    ap.add_argument("--keep", type=float, default=0.5)
    ap.add_argument("--label", default="CLI campaign")
    a = ap.parse_args()
    tid = call(a.base, "/api/targets/demo", {})["target_id"]
    sites = call(a.base, f"/api/targets/{tid}/sites")["sites"]
    ref = sites[0]["key"]
    job = call(a.base, "/api/jobs", {
        "target_id": tid, "label": a.label, "engine": a.engine, "site": {"mode": "ligand", "reference": ref},
        "library_source": a.source, "library_count": a.count, "batch_size": a.batch_size,
        "exhaustiveness": a.exhaustiveness, "dock_keep_fraction": a.keep, "enable_hyde": not a.no_hyde, "enable_grow": a.grow,
    })["job_id"]
    print("campaign", job)
    last = None
    while True:
        d = call(a.base, f"/api/jobs/{job}")
        line = d["status"] + " | " + " ".join(
            f"{s['key']}={s['status'][:4]}" + (f"({s['batches']['succeeded']}/{s['batches']['total']})" if s["batches"] else "")
            for s in d["stages"])
        if line != last:
            print(time.strftime("%H:%M:%S"), line)
            last = line
        if d["status"] in ("COMPLETE", "FAILED", "CANCELLED"):
            print("failure:", d["failure_reason"])
            print("outcomes:", d["molecule_outcomes"])
            break
        time.sleep(3)
    return job


if __name__ == "__main__":
    main()
