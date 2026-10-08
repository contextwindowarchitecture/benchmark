#!/usr/bin/env python3
"""A fake assembler for testing the harness: it looks the snapshot up in the conformance corpus by its digest and
answers with the expected result, or with one specific fault (CWA_FAKE_MODE), so each test knows what S1 must say.

    CWA_FAKE_CORPUS  the spec checkout's conformance/ directory
    CWA_FAKE_MODE    oracle | volatile | flip-payload | drop-excluded | swap-included | int-bool | accept-rejections
                     | crash | hang | garbage | unsupported-optional | unsupported-required
                     | flaky (wrong about a third of the time, at random) | tz (wrong unless TZ is unset or UTC)
                     | needs-home (crashes without HOME) | network (crashes when a socket cannot even be opened)
                     | reject-all (rejects every snapshot, generated ones too)
                     | crash-on-nul (crashes on a snapshot holding U+0000, rejects every other)
"""
import base64
import hashlib
import json
import os
import random
import socket
import sys
import time
from pathlib import Path


def find(corpus: Path, digest: str) -> tuple[str, Path] | None:
    for kind in ("cases", "rejections"):
        for directory in sorted((corpus / kind).iterdir()):
            snapshot = directory / "snapshot.json"
            if snapshot.is_file() and hashlib.sha256(snapshot.read_bytes()).hexdigest() == digest:
                return kind, directory
    return None


def main() -> int:
    raw = sys.stdin.buffer.read()
    mode = os.environ.get("CWA_FAKE_MODE", "oracle")
    if mode in ("reject-all", "crash-on-nul"):  # answer any snapshot, not only the corpus's
        if mode == "crash-on-nul" and b"\\u0000" in raw:
            raise RuntimeError("a NUL in a body")
        print("rejected", file=sys.stderr)
        return 2
    found = find(Path(os.environ["CWA_FAKE_CORPUS"]), hashlib.sha256(raw).hexdigest())
    if found is None:
        print("snapshot not in the corpus", file=sys.stderr)
        return 70
    kind, directory = found

    if mode == "crash":
        raise RuntimeError("deliberate crash")
    if mode == "needs-home" and "HOME" not in os.environ:
        raise RuntimeError("HOME is not set")
    if mode == "network":
        try:  # what an assembler that phoned home would do; refused is fine, forbidden is not
            socket.create_connection(("127.0.0.1", 9), timeout=1).close()
        except ConnectionRefusedError:
            pass
    if mode == "hang":
        time.sleep(30)
    if mode == "garbage":
        sys.stdout.write("this is not JSON")
        return 0
    if mode.startswith("unsupported"):
        component = "renderer cwa-message-blocks/v1" if mode == "unsupported-optional" else "renderer fixture-xml/v1"
        print(f"{component} is not provided", file=sys.stderr)
        return 3

    if kind == "rejections":
        if mode == "accept-rejections":
            sys.stdout.write(json.dumps({"payload": None, "trace": {"refused": {"bool": True, "reason": "x"}}}))
            return 0
        print("rejected", file=sys.stderr)
        return 2

    trace = json.loads((directory / "expected.trace.json").read_text(encoding="utf-8"))
    payload_path = directory / "expected.payload.txt"
    payload = payload_path.read_bytes() if payload_path.exists() else None

    if mode == "volatile":  # fields the comparison must ignore (R-23)
        trace["trace_id"] = "another-id"
        trace["timings"] = {"admission_ms": 1.5}
        if isinstance(trace.get("recovery"), dict):
            trace["recovery"]["detail"] = "free text"
    elif mode == "flip-payload" and payload:
        payload = payload[:-1] + bytes([payload[-1] ^ 1])
    elif mode == "drop-excluded" and trace.get("excluded"):
        trace["excluded"] = trace["excluded"][1:]
    elif mode == "swap-included" and len(trace.get("included", [])) >= 2:
        trace["included"][0], trace["included"][1] = trace["included"][1], trace["included"][0]
    elif (mode == "flaky" and random.random() < 0.34) or (mode == "tz" and os.environ.get("TZ") not in (None, "", "UTC")):
        trace["context"]["assembly_time"] = "1999-12-31T23:59:59Z"
    elif mode == "int-bool":  # refused.bool as 0/1: equal in Python, wrong in JSON
        trace["refused"]["bool"] = int(trace["refused"]["bool"])

    encoded = base64.b64encode(payload).decode() if payload is not None else None
    sys.stdout.write(json.dumps({"payload": encoded, "trace": trace}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
