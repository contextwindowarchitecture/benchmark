"""Writing results: every document names its schema and is validated before it is written (domain-1-plan.md, 12.1).

Documents carry "$schema": "cwa-bench-d1/<kind>/v1", and schemas/<kind>.v1.schema.json defines each kind. The
published conformance reports are the spec's own format and are validated against the spec's schema instead.

Domains. Each domain's harness names its documents under its own prefix and keeps its own schemas: a `Domain`. Domain
1's is `D1`, and the module-level `schema_name` and `validator` are Domain 1's. A harness that reuses this module, the
run directory and the blob store (domain-2-plan.md, 4.2) registers its domain, and `validate` and `kind_of` then read
the prefix a document names, so a run directory of any registered domain validates with the same code.

Schema versions. Adding a field to a kind is additive: it goes into the kind's current schema as an optional property,
so documents written before it still validate, and the harness always writes it from then on. A field removed,
renamed, retyped or given a new meaning, or a constraint an older document could break, is a new major version: a new
schemas/<kind>.v<n+1>.schema.json, and "$schema" names v<n+1> from then on. A consumer reads a kind at the major
versions it knows and says so for the rest.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
_SCHEMA_NAME = re.compile(r"^([a-z][a-z0-9-]*)/([a-z][a-z0-9-]*)/v(\d+)$")


class OutputError(Exception):
    pass


@lru_cache(maxsize=None)
def _validator(schema_dir: Path, prefix: str, kind: str, version: int) -> Draft202012Validator:
    path = schema_dir / f"{kind}.v{version}.schema.json"
    if not path.is_file():
        raise OutputError(f"no schema for {prefix}/{kind}/v{version} at {path}")
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


@dataclass(frozen=True)
class Domain:
    """One domain's documents: "$schema": "<prefix>/<kind>/v<n>", defined by <schema_dir>/<kind>.v<n>.schema.json."""

    prefix: str
    schema_dir: Path

    def schema_name(self, kind: str, version: int = 1) -> str:
        return f"{self.prefix}/{kind}/v{version}"

    def validator(self, kind: str, version: int = 1) -> Draft202012Validator:
        return _validator(self.schema_dir, self.prefix, kind, version)


D1 = Domain("cwa-bench-d1", SCHEMA_DIR)
_DOMAINS: dict[str, Domain] = {D1.prefix: D1}


def register(domain: Domain) -> Domain:
    """Make a domain's documents readable by `validate` and `kind_of`. Registering the same domain again is a no-op."""
    known = _DOMAINS.setdefault(domain.prefix, domain)
    if known != domain:
        raise OutputError(f"{domain.prefix} is already registered with schemas at {known.schema_dir}")
    return domain


def schema_name(kind: str, version: int = 1) -> str:
    return D1.schema_name(kind, version)


def validator(kind: str, version: int = 1) -> Draft202012Validator:
    return D1.validator(kind, version)


def parse(document: dict) -> tuple[Domain, str, int]:
    """The registered domain, kind and major version a document's "$schema" names."""
    name = document.get("$schema") if isinstance(document, dict) else None
    match = _SCHEMA_NAME.match(name) if isinstance(name, str) else None
    domain = _DOMAINS.get(match.group(1)) if match else None
    if domain is None:
        known = ", ".join(sorted(_DOMAINS))
        raise OutputError(f"document has no $schema of a registered domain ({known}): {str(name)[:80]!r}")
    return domain, match.group(2), int(match.group(3))


def kind_of(document: dict) -> tuple[str, int]:
    _, kind, version = parse(document)
    return kind, version


def validate(document: dict) -> None:
    domain, kind, version = parse(document)
    errors = sorted(domain.validator(kind, version).iter_errors(document), key=lambda e: list(e.absolute_path))
    if errors:
        first = errors[0]
        where = "/" + "/".join(str(p) for p in first.absolute_path)
        raise OutputError(f"{domain.schema_name(kind, version)} invalid at {where}: {first.message[:300]}")


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
