#!/usr/bin/env python3
"""Adapter from the Python reference assembler to the harness protocol (assembler-template/PORTING.md).

Snapshot bytes on stdin. Exit 0 with {"payload": base64 or null, "trace": {...}} on stdout when assembled or refused;
exit 2 when the snapshot is rejected before assembly (the problems on stderr); exit 3 when it names a tokenizer or
renderer this assembler does not provide. Run it with the assembler's environment, which `cwabench setup` builds.

Adapted from assembler-demo/adapters/python_adapter.py so the benchmark does not depend on the demo. That adapter looked
the tokenizer and renderer up before loading the snapshot, so a snapshot that breaks its schema and also names an
unknown or blank tokenizer exited 3 instead of 2; conformance/README.md (Running a case, step 1) validates first, as the
assembler's own Snapshot.from_json does, and the other three adapters defer to their assemblers the same way.
"""
import base64
import json
import sys

from cwa import Snapshot, SnapshotError, UnsupportedComponentError, assemble


def main() -> int:
    raw = sys.stdin.buffer.read()
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        print(f"not a JSON document: {error}", file=sys.stderr)
        return 2
    try:
        result = assemble(Snapshot.from_json(document))
    except SnapshotError as error:
        print("; ".join(error.problems), file=sys.stderr)
        return 2
    except UnsupportedComponentError as error:
        print(f"{error.component} {error.id} is not provided", file=sys.stderr)
        return 3
    payload = base64.b64encode(result.payload).decode("ascii") if result.payload is not None else None
    sys.stdout.write(json.dumps({"payload": payload, "trace": result.trace}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
