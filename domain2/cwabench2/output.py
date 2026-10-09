"""Domain 2's documents (domain-2-plan.md, 11): Domain 1's conventions under the prefix cwa-bench-d2.

Every document carries "$schema": "cwa-bench-d2/<kind>/v1" and is validated against schemas/<kind>.v1.schema.json
before it is written, with Domain 1's writer (cwabench.output), which reads the prefix. Kinds Domain 2 reuses keep
Domain 1's shapes; tests/test_schemas.py holds the copies to Domain 1's, prefix aside.
"""
from __future__ import annotations

from pathlib import Path

from cwabench.output import Domain, OutputError, dumps, register, validate, write_json, write_jsonl

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
D2 = register(Domain("cwa-bench-d2", SCHEMA_DIR))

__all__ = ["D2", "SCHEMA_DIR", "OutputError", "dumps", "schema_name", "validate", "write_json", "write_jsonl"]


def schema_name(kind: str, version: int = 1) -> str:
    return D2.schema_name(kind, version)
