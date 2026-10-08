#!/usr/bin/env python3
"""Runs inside the benchmark container: every snapshot through every adapter in every requested cell, recording raw
answers only. The host classifies them, so there is one implementation of the adapter protocol's rules.

    python3 /opt/cwa/runner.py /work/job.json

job.json:
    {"snapshots": [{"index": 0, "path": "snapshots/0.json"}, ...],
     "adapters": {"go": {"argv": [...], "env": {...}, "probe": [...]}, ...},
     "cells": [{"name": "linux:faketime-30y", "repetitions": 1, "env": {...},
                "faketime": "-10950d" | null, "strace": false}],
     "timeout": 60, "concurrency": 8, "base_env": {...}}

Writes, under /work/out: results.jsonl (one line per answer), raw/ (stdout and stderr files), strace/, probes.json.
Standard library only, so it needs nothing the assemblers do not bring.
"""
import glob
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

STRACE = ["strace", "-f", "-qq", "-s", "256", "-e", "signal=none",
          "-e", "trace=%network,open,openat,openat2,execve,readlink,readlinkat"]


def faketime_library():
    found = sorted(glob.glob("/usr/lib/*/faketime/libfaketime.so.1"))
    return found[0] if found else None


def cell_env(base, adapter, cell, library):
    env = dict(base)
    env.update(cell.get("env") or {})
    env.update(adapter.get("env") or {})
    if cell.get("faketime"):
        env.update({"LD_PRELOAD": library, "FAKETIME": cell["faketime"], "FAKETIME_DONT_FAKE_MONOTONIC": "1",
                    "FAKETIME_NO_CACHE": "1"})
    return env


def probe(argv, env):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env)
        return int(done.stdout.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def main(job_path):
    work = Path(job_path).parent
    job = json.loads(Path(job_path).read_text(encoding="utf-8"))
    out = work / "out"
    (out / "raw").mkdir(parents=True, exist_ok=True)
    (out / "strace").mkdir(parents=True, exist_ok=True)
    library = faketime_library()
    base = job["base_env"]

    # Whether a clock shift reaches each adapter's runtime: run its probe with and without the shift.
    probes = {}
    for cell in job["cells"]:
        if not cell.get("faketime"):
            continue
        for name, adapter in job["adapters"].items():
            real = probe(adapter["probe"], cell_env(base, adapter, {}, library))
            shifted = probe(adapter["probe"], cell_env(base, adapter, cell, library)) if library else None
            effective = real is not None and shifted is not None and abs(shifted - real) > 365 * 86400
            probes.setdefault(cell["name"], {})[name] = {"real": real, "shifted": shifted, "effective": effective,
                                                        "library": library}
    (out / "probes.json").write_text(json.dumps(probes, indent=2), encoding="utf-8")

    jobs = []
    for cell in job["cells"]:
        for repetition in range(cell["repetitions"]):
            for name in job["adapters"]:
                for snapshot in job["snapshots"]:
                    jobs.append((cell, repetition, name, snapshot))
    lock = threading.Lock()
    results = open(out / "results.jsonl", "w", encoding="utf-8")

    def work_one(item):
        cell, repetition, name, snapshot = item
        adapter = job["adapters"][name]
        key = f"{cell['name'].replace(':', '_').replace('/', '_')}/{name}/{snapshot['index']}-{repetition}"
        for sub in ("raw", "strace"):
            (out / sub / key).parent.mkdir(parents=True, exist_ok=True)
        argv = list(adapter["argv"])
        strace_path = None
        if cell.get("strace"):
            strace_path = f"strace/{key}.txt"
            argv = STRACE + ["-o", str(out / strace_path), "--"] + argv
        applied = True
        reason = None
        if cell.get("faketime"):
            status = probes.get(cell["name"], {}).get(name, {})
            applied = bool(status.get("effective"))
            reason = None if applied else "the clock shift does not reach this runtime"
        data = (work / snapshot["path"]).read_bytes()
        started = time.perf_counter()
        timed_out, code, stdout, stderr = False, None, b"", b""
        try:
            done = subprocess.run(argv, input=data, capture_output=True, timeout=job["timeout"],
                                  env=cell_env(base, adapter, cell, library), cwd="/tmp")
            code, stdout, stderr = done.returncode, done.stdout, done.stderr
        except subprocess.TimeoutExpired as expired:
            timed_out, stdout, stderr = True, expired.stdout or b"", expired.stderr or b""
        except OSError as error:
            stderr = str(error).encode()
        wall = (time.perf_counter() - started) * 1000
        (out / "raw" / f"{key}.out").write_bytes(stdout)
        (out / "raw" / f"{key}.err").write_bytes(stderr)
        line = {"cell": cell["name"], "repetition": repetition, "adapter": name, "snapshot": snapshot["index"],
                "exit_code": code, "timed_out": timed_out, "wall_ms": wall, "stdout": f"raw/{key}.out",
                "stderr": f"raw/{key}.err", "strace": strace_path, "applied": applied, "not_applied_reason": reason}
        with lock:
            results.write(json.dumps(line) + "\n")

    with ThreadPoolExecutor(max_workers=job["concurrency"]) as pool:
        list(pool.map(work_one, jobs))
    results.close()

    versions = {}
    for path in sorted(glob.glob("/opt/cwa/*.version")):
        versions[Path(path).stem] = Path(path).read_text(encoding="utf-8").strip()
    (out / "versions.json").write_text(json.dumps(versions, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
