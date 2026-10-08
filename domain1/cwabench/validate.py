"""Check a run directory after the fact: every file it lists exists and validates, and every blob matches its digest."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import output
from .contract import Contract


def validate_run(run_dir: Path) -> list[str]:
    problems: list[str] = []

    def load(relative: str):
        try:
            return json.loads((run_dir / relative).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            problems.append(f"{relative}: {error}")
            return None

    index = load("index.json")
    manifest = load("manifest.json")
    if index is None or manifest is None:
        return problems
    for document, name in ((index, "index.json"), (manifest, "manifest.json")):
        try:
            output.validate(document)
        except output.OutputError as error:
            problems.append(f"{name}: {error}")

    contract = None
    try:
        contract = Contract(Path(manifest["contract"]["path"]), manifest["contract"]["commit"], allow_dirty=True)
    except Exception as error:  # the spec checkout may have moved since the run; report, don't stop
        problems.append(f"cannot load the run's contract to check spec-format files: {error}")

    for entry in index["files"]:
        path = run_dir / entry["path"]
        if not path.is_file():
            problems.append(f"{entry['path']}: listed in index.json but missing")
            continue
        if entry["kind"] == "config":
            continue
        if entry["path"].endswith(".jsonl"):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
                try:
                    output.validate(json.loads(line))
                except (json.JSONDecodeError, output.OutputError) as error:
                    problems.append(f"{entry['path']} row {n}: {error}")
                    break
        elif entry["kind"] == "conformance-draft":
            if entry["path"].endswith(".json"):
                try:
                    document = json.loads(path.read_bytes().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    problems.append(f"{entry['path']}: {error}")
                    continue
                if entry["schema"] and contract is not None:
                    errors = list(contract.validator(entry["schema"]).iter_errors(document))
                    if errors:
                        problems.append(f"{entry['path']}: {errors[0].message[:300]}")
        elif entry["kind"] == "conformance-report":
            if contract is not None:
                errors = list(contract.validator("conformance_report.schema.json").iter_errors(load(entry["path"])))
                if errors:
                    problems.append(f"{entry['path']}: {errors[0].message[:300]}")
        else:
            document = load(entry["path"])
            if document is not None:
                try:
                    output.validate(document)
                except output.OutputError as error:
                    problems.append(f"{entry['path']}: {error}")

    blob_index = run_dir / index["blobs"]["index"]
    for line in blob_index.read_text(encoding="utf-8").splitlines() if blob_index.is_file() else []:
        entry = json.loads(line)
        path = run_dir / entry["path"]
        if not path.is_file():
            problems.append(f"blob {entry['digest']} missing at {entry['path']}")
            continue
        data = path.read_bytes()
        if f"sha256:{hashlib.sha256(data).hexdigest()}" != entry["digest"] or len(data) != entry["bytes"]:
            problems.append(f"blob {entry['digest']} does not match its content")
    return problems
