#!/usr/bin/env python3
"""The reference assembler with one deliberate defect (CWA_BUGGY_MODE), so a test can show that S4 or S7 catches it.
Run it with the reference assembler's environment (.build/python-venv, which `cwabench setup` builds).

    batch-order         the trace's snapshot_digest depends on which batch came first, which MR1 must catch
    float-crash         crashes on any number written with a fraction, which MR4's integer spelling must catch
    budget-plus-one     assembles as if budget.input were one token larger, which S7's predictions, threshold search
                        and audit must catch
    truncate-protected  drops the last word of the protected instructions from the payload, which S7's protected
                        preservation and audit must catch
    slow                sleeps 10 s on a snapshot of more than 50 items, which S7 must report as a timeout and use to
                        skip larger cells
    variant-method      names every selected variant's method as "summary", not the method it was frozen with, which
                        S11's R-18 method check must catch
    rewrite-variant     drops the last word of every selected variant from the payload, text no producer supplied,
                        which the audit (A5, A8) must catch in S11
    flaky-budget        on about a third of invocations assembles as if budget.input were one token smaller, which
                        S11's repeated answers must catch
    none                no defect
"""
import base64
import hashlib
import json
import os
import random
import sys
import time

from cwa import Snapshot, SnapshotError, UnsupportedComponentError, assemble


def floats(value) -> bool:
    if isinstance(value, float):
        return True
    if isinstance(value, dict):
        return any(floats(v) for v in value.values())
    if isinstance(value, list):
        return any(floats(v) for v in value)
    return False


def main() -> int:
    mode = os.environ.get("CWA_BUGGY_MODE", "")
    document = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    if mode == "float-crash" and floats(document):
        raise TypeError("a number with a fraction where an integer was expected")
    if mode == "slow" and sum(len(b.get("items", [])) for b in document.get("batches", [])) > 50:
        time.sleep(10)
    if mode == "budget-plus-one":
        document["budget"]["input"] += 1
    flaky = mode == "flaky-budget" and random.random() < 1 / 3
    if flaky:
        document["budget"]["input"] -= 1
    try:
        result = assemble(Snapshot.from_json(document))
    except SnapshotError as error:
        print("; ".join(error.problems), file=sys.stderr)
        return 2
    except UnsupportedComponentError as error:
        print(f"{error.component} {error.id} is not provided", file=sys.stderr)
        return 3
    trace = result.trace
    if mode == "batch-order" and document["batches"]:
        first = document["batches"][0]["producer"]["id"]
        trace["context"]["snapshot_digest"] = hashlib.sha256(first.encode()).hexdigest()
    if mode == "budget-plus-one":
        trace["budget"]["input"] -= 1
    if flaky:
        trace["budget"]["input"] += 1
    if mode == "variant-method":
        for row in trace["compressed"]:
            row["method"] = "summary"
    payload = result.payload
    if mode == "rewrite-variant" and payload is not None:
        for row in trace["compressed"]:
            body = next(v["body"] for b in document["batches"] for i in b["items"] if i.get("id") == row["item_id"]
                        for v in i.get("variants") or [] if v["id"] == row["variant_id"])
            if " " in body:
                payload = payload.replace(body.encode(), body.rsplit(" ", 1)[0].encode(), 1)
    if mode == "truncate-protected" and payload is not None:
        instructions = next(i["body"] for b in document["batches"] for i in b["items"]
                            if i.get("slot") == "governance.instructions")
        payload = payload.replace(instructions.encode(), instructions.rsplit(" ", 1)[0].encode(), 1)
    payload = base64.b64encode(payload).decode("ascii") if payload is not None else None
    sys.stdout.write(json.dumps({"payload": payload, "trace": trace}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
