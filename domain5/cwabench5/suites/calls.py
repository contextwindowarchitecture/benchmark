"""Asking the model for a suite (domain-5-plan.md, 9), as Domain 2 asks it (cwabench2/suites/calls.py).

Every distinct payload is asked `samples` times; calls run as one stream per key (an instance and arm), with a
payload's samples back to back, so consecutive requests share their prefix and a server's prefix cache answers most
of it. `[model].concurrency` streams run at once. A reply is cached by its request, whenever it was made, so the order
changes nothing; the call rows are written in the order of payload hash and sample.
"""
from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

from cwabench2.model import CacheMiss, EndpointError

from .. import output


def ask(ctx, suite: str, model, ordered: list[tuple[str, bytes, int]], concurrency: int, calls_path: str,
        label: str | None = None):
    """Ask every (stream key, payload, samples) of `ordered`. Returns the answers by (payload hash, sample), each a
    reply or the error it raised; the call rows written to `calls_path`; and the errors. `label` names the calls in
    the log (the suite by default)."""
    label = label or suite
    payload_of: dict[str, bytes] = {}
    wanted: dict[str, int] = {}
    streams: dict[str, list[tuple[str, int]]] = {}
    for key, payload, samples in ordered:
        sha = hashlib.sha256(payload).hexdigest()
        payload_of[sha] = payload
        for sample in range(wanted.get(sha, 0), samples):
            streams.setdefault(key, []).append((sha, sample))
        wanted[sha] = max(wanted.get(sha, 0), samples)
    jobs = [job for stream in streams.values() for job in stream]
    ctx.log(f"{label}: {len(ordered)} payloads, {len(payload_of)} distinct, {len(jobs)} calls in {len(streams)} "
            f"streams")
    errors: list[str] = []
    progress = {"done": 0}
    lock = threading.Lock()

    def run(stream):
        answered = []
        for job in stream:
            sha, sample = job
            try:
                answered.append((job, model.ask(payload_of[sha], sample)))
            except (CacheMiss, EndpointError) as error:
                answered.append((job, error))
            with lock:
                progress["done"] += 1
                if progress["done"] % 500 == 0:
                    ctx.log(f"{label}: {progress['done']}/{len(jobs)} calls ({model.calls} to the endpoint)")
        return answered

    calls: dict[tuple[str, int], object] = {}
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for answered in pool.map(run, list(streams.values())):
            for job, answer in answered:
                calls[job] = answer
                if isinstance(answer, Exception):
                    errors.append(f"{type(answer).__name__}: {answer}")
    rows = []
    for (sha, sample), answer in sorted(calls.items()):
        if isinstance(answer, Exception):
            continue
        rows.append({"$schema": output.schema_name("call-row"), "run_id": ctx.run.run_id, "suite": suite,
                     "key": answer.key, "request_sha256": answer.request_sha256, "payload_sha256": sha,
                     "sample": sample, "cache_hit": answer.cache_hit, "lookup_ms": answer.lookup_ms,
                     "provenance": answer.provenance})
    ctx.run.write_jsonl(calls_path, rows, "call-row", f"Provenance of every model call {suite} made or replayed: "
                                                      "one row per distinct request and sample")
    return calls, rows, errors
