#!/usr/bin/env python3
"""
Deployment smoke test — API level, standard library only.

    python scripts/smoke_test.py [BASE_URL]        # default http://127.0.0.1:8008

Confirms a running deployment can do real work end to end: it is alive and
ready, loads the bundled demo target, runs a pipeline job to completion
against PostgreSQL, and serves a parseable SDF. Exits non-zero on the first
failure. It does not exercise the browser UI.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8008").rstrip("/")


def call(path: str, body: dict | None = None, raw: bool = False):
    request = urllib.request.Request(
        BASE + path,
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = response.read()
    return payload.decode() if raw else json.loads(payload)


def step(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    if not ok:
        sys.exit(1)


def main() -> None:
    print(f"Smoke test against {BASE}")

    step("GET /health", call("/health") == {"status": "ok"})

    ready = call("/ready")
    step("GET /ready", ready["status"] == "ready", json.dumps(ready["checks"]))

    target = call("/api/targets/demo", {})
    step(
        "demo target loaded and validated",
        target["validation_status"] == "PASSED" and target["atom_count"] > 0,
        f"{target['name']} ({target['pdb_id']}), {target['residue_count']} residues, {target['atom_count']} atoms",
    )

    job = call("/api/jobs", {"target_id": target["target_id"], "library_limit": 25, "stage_delay_seconds": 0})
    step("job created", bool(job["job_id"]), f"{job['job_id']} status={job['status']}")

    deadline = time.time() + 120
    status = job
    while time.time() < deadline and status["status"] not in ("COMPLETE", "FAILED"):
        time.sleep(0.5)
        status = call(f"/api/jobs/{job['job_id']}")
    step("pipeline completed", status["status"] == "COMPLETE", f"status={status['status']} {status.get('failure_reason') or ''}")

    funnel = [stage["count_out"] for stage in status["stages"][1:6]]
    step("funnel reduces candidates", funnel[0] > funnel[-1] >= 1, " → ".join(map(str, funnel)))

    candidates = call(f"/api/jobs/{job['job_id']}/candidates")["candidates"]
    step("candidates persisted", len(candidates) == funnel[-1], f"{len(candidates)} candidates")

    sdf = call(f"/api/jobs/{job['job_id']}/candidates.sdf", raw=True)
    step("SDF export", sdf.count("$$$$") == len(candidates), f"{sdf.count('$$$$')} records")

    print("Smoke test passed.")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.URLError as exc:
        print(f"  [FAIL] could not reach {BASE}: {exc}")
        sys.exit(1)
