"""The CWA specification checkout: its contract files, schemas and conformance corpus, pinned to one commit."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from . import gitinfo, output

# conformance/README.md, Tokenizers and renderers. Every implementation provides the required ones; a case that uses
# an optional one an implementation lacks is skipped, not failed. Contract.check() confirms the README still lists
# each id, so a spec change that adds one fails loudly instead of being misjudged.
REQUIRED_COMPONENTS = {
    "tokenizer": ("fixture-whitespace/v1", "estimate-utf8/v1"),
    "renderer": ("fixture-xml/v1", "cwa-messages/v1"),
}
OPTIONAL_COMPONENTS = {
    "tokenizer": (),
    "renderer": ("cwa-message-blocks/v1",),
}

# Trace fields that may differ between runs and are removed before comparison (conformance/README.md, Running a
# case, step 4).
VOLATILE_TRACE_FIELDS = ("trace_id", "timings", "recovery.detail")


class ContractError(Exception):
    pass


def utf16_key(text: str) -> bytes:
    """Order strings by UTF-16 code units, as the spec orders every id (conformance/README.md, Ordering)."""
    return text.encode("utf-16-be", "surrogatepass")


@dataclass(frozen=True)
class Case:
    id: str
    kind: str  # "case" or "rejection"
    rules: list[str]
    description: str
    directory: Path
    snapshot_bytes: bytes
    expected_trace: dict | None
    expected_payload: bytes | None

    @property
    def expected_outcome(self) -> str:
        if self.kind == "rejection":
            return "rejected"
        if self.expected_payload is not None:
            return "assembled"
        return "refused"

    @cached_property
    def snapshot(self) -> dict | None:
        """The snapshot as JSON, or None when it does not parse (some rejections are meant not to)."""
        try:
            value = json.loads(self.snapshot_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def components(self) -> dict[str, str | None]:
        snapshot = self.snapshot or {}
        return {
            kind: snapshot.get(kind) if isinstance(snapshot.get(kind), str) else None
            for kind in ("tokenizer", "renderer")
        }


class Contract:
    def __init__(self, path: Path, expected_commit: str, allow_dirty: bool = False):
        self.path = path
        if not (path / "SPEC.md").is_file() or not (path / "conformance").is_dir():
            raise ContractError(f"{path} is not a checkout of the CWA specification")
        self.checkout = gitinfo.inspect(path)
        self.expected_commit = expected_commit
        if self.checkout.commit is None:
            raise ContractError(f"{path} is not a git checkout, so its commit cannot be pinned")
        if not self.checkout.commit.startswith(expected_commit):
            raise ContractError(
                f"{path} is at {self.checkout.commit}, but the config pins {expected_commit}; "
                "check out the pinned commit or change [contract].commit"
            )
        if self.checkout.dirty and not allow_dirty:
            raise ContractError(f"{path} has uncommitted changes; commit them or set [contract].allow_dirty")
        self.check()

    def _json(self, relative: str):
        return json.loads((self.path / relative).read_text(encoding="utf-8"))

    @cached_property
    def requirements(self) -> list[dict]:
        return self._json("contract/requirements.json")

    @cached_property
    def scopes(self) -> dict[str, dict]:
        return {entry["id"]: entry for entry in self._json("contract/assembler-scope.json")}

    @cached_property
    def reasons(self) -> list[dict]:
        return self._json("contract/reasons.json")

    @cached_property
    def slot_defaults(self) -> dict[str, dict]:
        return self._json("contract/slot-defaults.json")

    @cached_property
    def model(self) -> dict:
        return self._json("contract/model.json")

    @cached_property
    def spec_draft(self) -> str | None:
        """The draft line of SPEC.md, such as "Draft of 2026-10-05"."""
        match = re.search(r"^Draft of (\d{4}-\d{2}-\d{2})", (self.path / "SPEC.md").read_text(encoding="utf-8"), re.M)
        return match.group(1) if match else None

    def reason_template(self, code: str) -> str | None:
        """The reasons.json code a recorded reason instantiates: missing_field:relevance is missing_field:<name>."""
        known = {entry["code"] for entry in self.reasons}
        if code in known:
            return code
        if code.startswith("missing_field:") and "missing_field:<name>" in known:
            return "missing_field:<name>"
        return None

    # Schemas ----------------------------------------------------------------------------------------------------

    @cached_property
    def _schemas(self) -> dict[str, dict]:
        return {p.name: json.loads(p.read_text(encoding="utf-8")) for p in sorted((self.path / "schema").glob("*.json"))}

    @cached_property
    def _registry(self) -> Registry:
        resources = [(schema["$id"], Resource.from_contents(schema)) for schema in self._schemas.values()]
        return Registry().with_resources(resources)

    def validator(self, name: str) -> Draft202012Validator:
        """A validator for one published schema, with format assertion on (conformance/README.md, Timestamps)."""
        if "date-time" not in Draft202012Validator.FORMAT_CHECKER.checkers:
            raise ContractError("format: date-time is not asserted; install rfc3339-validator")
        return Draft202012Validator(
            self._schemas[name], registry=self._registry, format_checker=Draft202012Validator.FORMAT_CHECKER
        )

    # Corpus -----------------------------------------------------------------------------------------------------

    def _load_case(self, directory: Path, kind: str) -> Case:
        meta = json.loads((directory / "case.json").read_text(encoding="utf-8"))
        trace_path = directory / "expected.trace.json"
        payload_path = directory / "expected.payload.txt"
        return Case(
            id=meta["id"],
            kind=kind,
            rules=list(meta.get("rules", [])),
            description=meta.get("description", ""),
            directory=directory,
            snapshot_bytes=(directory / "snapshot.json").read_bytes(),
            expected_trace=json.loads(trace_path.read_text(encoding="utf-8")) if trace_path.exists() else None,
            expected_payload=payload_path.read_bytes() if payload_path.exists() else None,
        )

    @cached_property
    def cases(self) -> list[Case]:
        root = self.path / "conformance" / "cases"
        found = [self._load_case(d, "case") for d in root.iterdir() if (d / "case.json").is_file()]
        return sorted(found, key=lambda c: utf16_key(c.id))

    @cached_property
    def rejections(self) -> list[Case]:
        root = self.path / "conformance" / "rejections"
        found = [self._load_case(d, "rejection") for d in root.iterdir() if (d / "case.json").is_file()]
        return sorted(found, key=lambda c: utf16_key(c.id))

    def check(self) -> None:
        """Fail when the checkout no longer matches what this harness assumes about it."""
        readme = (self.path / "conformance" / "README.md").read_text(encoding="utf-8")
        for table in (REQUIRED_COMPONENTS, OPTIONAL_COMPONENTS):
            for ids in table.values():
                for component in ids:
                    if f"`{component}`" not in readme:
                        raise ContractError(f"conformance/README.md no longer lists {component}")
        for name in ("trace.schema.json", "snapshot.schema.json", "conformance_report.schema.json"):
            if not (self.path / "schema" / name).is_file():
                raise ContractError(f"schema/{name} is missing")

    # Documents --------------------------------------------------------------------------------------------------

    def as_document(self, run_id: str, domain: output.Domain = output.D1) -> dict:
        """contract.json: every enumeration a consumer of the results needs, so none of them hardcodes the spec. Every
        domain writes the same document under its own prefix."""
        return {
            "$schema": domain.schema_name("contract"),
            "run_id": run_id,
            "repository": self.checkout.repository,
            "commit": self.checkout.commit,
            "dirty": bool(self.checkout.dirty),
            "spec_draft": self.spec_draft,
            "requirements": [
                {
                    "id": r["id"],
                    "section": r["section"],
                    "keyword": r["keyword"],
                    "summary": r["summary"],
                    "text": r["text"],
                    "scope": self.scopes.get(r["id"], {}).get("scope"),
                    "scope_note": self.scopes.get(r["id"], {}).get("note"),
                }
                for r in self.requirements
            ],
            "reasons": [
                {"code": r["code"], "kind": r["kind"], "rule": r["rule"], "text": r["text"], "order": i}
                for i, r in enumerate(self.reasons)
            ],
            "planes": self.model["planes"],
            "slots": [
                {
                    "id": slot["id"],
                    "holds": slot["holds"],
                    "rule": slot["rule"],
                    "defaults": self.slot_defaults[slot["id"]],
                }
                for slot in self.model["slots"]
            ],
            "authority": self.model["authority"],
            "stages": self.model["stages"],
            "components": {
                "required": {k: list(v) for k, v in REQUIRED_COMPONENTS.items()},
                "optional": {k: list(v) for k, v in OPTIONAL_COMPONENTS.items()},
            },
            "corpus": {
                "cases": [{"id": c.id, "rules": c.rules, "description": c.description} for c in self.cases],
                "rejections": [{"id": c.id, "rules": c.rules, "description": c.description} for c in self.rejections],
            },
        }
