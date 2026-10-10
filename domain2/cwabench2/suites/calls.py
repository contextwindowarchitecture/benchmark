"""Asking the model for a suite (domain-2-plan.md, 9): every distinct payload, `repeats` samples each, as streams.

One call is made per distinct request and sample, however many arms share it. The calls run as one stream per key
(a conversation, or an LQ corpus), in the order given with each payload's samples back to back, so consecutive
requests share their prefix and a server's prefix cache answers most of it; `[model].concurrency` streams run at once.
The order changes no reply: a reply is cached by its request, whenever it was made. The call rows are written in the
order of payload hash and sample, so they do not depend on the order either.
"""
from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

from .. import output
from ..model import CacheMiss, EndpointError, Model


def ask(ctx, suite: str, model: Model, ordered: list[tuple[str, bytes | None]], repeats: int, concurrency: int,
        calls_path: str) -> tuple[dict[tuple[str, int], object], list[dict], list[str], int]:
    """Ask every payload of `ordered` ((stream key, payload or None), in calling order) `repeats` times. Returns the
    answers by (payload hash, sample), each a model.Reply or the error it raised; the call rows written to
    `calls_path`; the errors; and how many calls were planned."""
    payload_of: dict[str, bytes] = {}
    streams: dict[str, list[tuple[str, int]]] = {}
    for key, payload in ordered:
        if payload is None:
            continue
        sha = hashlib.sha256(payload).hexdigest()
        if sha not in payload_of:
            payload_of[sha] = payload
            streams.setdefault(key, []).extend((sha, sample) for sample in range(repeats))
    jobs = [job for stream in streams.values() for job in stream]
    ctx.log(f"{suite}: {len(ordered)} payloads, {len(payload_of)} distinct, {len(jobs)} calls in {len(streams)} "
            f"streams")
    errors: list[str] = []
    progress = {"done": 0}
    lock = threading.Lock()

    def run(stream: list[tuple[str, int]]) -> list[tuple[tuple[str, int], object]]:
        answered = []
        for job in stream:
            sha, sample = job
            try:
                answered.append((job, model.ask(payload_of[sha], sample)))
            except (CacheMiss, EndpointError) as error:
                answered.append((job, error))
            with lock:
                progress["done"] += 1
                if progress["done"] % 200 == 0:
                    ctx.log(f"{suite}: {progress['done']}/{len(jobs)} calls ({model.calls} to the endpoint)")
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
    return calls, rows, errors, len(jobs)
