#!/usr/bin/env python3
"""In-process timing loop for the Python assembler (domain-1-plan.md, 8.3, method 3).

Snapshot bytes on stdin; arguments: warm-up runs, then timed runs. Each run goes from the snapshot's bytes to the
assembler's payload and trace, as the adapter does, without starting a process. Prints one JSON line:
{"outcome": "assembled" | "refused" | "rejected" | "unsupported", "samples_ns": [...]}.
"""
import json
import sys
import time

from cwa import Snapshot, SnapshotError, UnsupportedComponentError, assemble


def once(raw: bytes) -> str:
    try:
        result = assemble(Snapshot.from_json(json.loads(raw.decode("utf-8"))))
    except SnapshotError:
        return "rejected"
    except UnsupportedComponentError:
        return "unsupported"
    return "refused" if result.payload is None else "assembled"


def main() -> int:
    warmup, runs = int(sys.argv[1]), int(sys.argv[2])
    raw = sys.stdin.buffer.read()
    outcome = None
    for _ in range(warmup):
        outcome = once(raw)
    samples = []
    for _ in range(runs):
        started = time.perf_counter_ns()
        outcome = once(raw)
        samples.append(time.perf_counter_ns() - started)
    print(json.dumps({"outcome": outcome, "samples_ns": samples}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
