"""Writing results: every document names its schema and is validated before it is written (domain-1-plan.md, 12.1).

Documents carry "$schema": "cwa-bench-d1/<kind>/v1", and schemas/<kind>.v1.schema.json defines each kind. The
published conformance reports are the spec's own format and are validated against the spec's schema instead.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
_SCHEMA_NAME = re.compile(r"^cwa-bench-d1/([a-z][a-z0-9-]*)/v(\d+)$")


class OutputError(Exception):
    pass


def schema_name(kind: str, version: int = 1) -> str:
    return f"cwa-bench-d1/{kind}/v{version}"


@lru_cache(maxsize=None)
def validator(kind: str, version: int = 1) -> Draft202012Validator:
    path = SCHEMA_DIR / f"{kind}.v{version}.schema.json"
    if not path.is_file():
        raise OutputError(f"no schema for {schema_name(kind, version)} at {path}")
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def kind_of(document: dict) -> tuple[str, int]:
    name = document.get("$schema") if isinstance(document, dict) else None
    match = _SCHEMA_NAME.match(name) if isinstance(name, str) else None
    if not match:
        raise OutputError(f"document has no cwa-bench-d1 $schema: {str(name)[:80]!r}")
    return match.group(1), int(match.group(2))


def validate(document: dict) -> None:
    kind, version = kind_of(document)
    errors = sorted(validator(kind, version).iter_errors(document), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        where = "/" + "/".join(str(p) for p in first.absolute_path)
        raise OutputError(f"{schema_name(kind, version)} invalid at {where}: {first.message[:300]}")


def dumps(document, compact: bool = False) -> str:
    """JSON that is I-JSON safe: no NaN or Infinity, UTF-8 text, stable member order as built."""
    if compact:
        return json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2) + "\n"


def _umask() -> int:
    current = os.umask(0)
    os.umask(current)
    return current


_FILE_MODE = 0o666 & ~_umask()  # what open() would give; mkstemp's 0600 would hide results from other readers


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temporary, _FILE_MODE)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def write_json(path: Path, document: dict, validate_with=None) -> None:
    """Validate, then write. `validate_with` is a validator for a document without our $schema."""
    if validate_with is None:
        validate(document)
    else:
        errors = list(validate_with.iter_errors(document))
        if errors:
            raise OutputError(f"{path.name} invalid: {errors[0].message[:300]}")
    _atomic_write(path, dumps(document))


def write_jsonl(path: Path, rows: list[dict]) -> None:
    for i, row in enumerate(rows):
        try:
            validate(row)
        except OutputError as error:
            raise OutputError(f"{path.name} row {i}: {error}") from None
    _atomic_write(path, "".join(dumps(row, compact=True) + "\n" for row in rows))
