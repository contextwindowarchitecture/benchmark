"""One run's directory and the index of every run (domain-1-plan.md, 12.2)."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, output
from .blobs import BlobStore
from .config import Config

STATUS_ORDER = ("pass", "partial", "fail", "error")  # worst last


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def worst(statuses) -> str:
    statuses = list(statuses)
    return max(statuses, key=STATUS_ORDER.index) if statuses else "error"


HARNESS_ROOT = Path(__file__).resolve().parent.parent
SOURCES = ("cwabench/**/*.py", "schemas/*.json", "adapters/**/*", "container/**/*")  # Domain 1's own files


def source_digest(sources: tuple[tuple[Path, tuple[str, ...]], ...] = ((HARNESS_ROOT, SOURCES),)) -> str:
    """SHA-256 over the harness's own files: its code, schemas, adapters, timing loops and container build. A run takes
    it when it starts, so an edit made while it runs shows up as a bump in the next run's drift report. `sources` is
    (root, glob patterns) per tree: Domain 1's alone by default; a harness built on this one adds its own tree."""
    digest = hashlib.sha256()
    for root, patterns in sources:
        files = [path for pattern in patterns for path in root.glob(pattern)]
        for path in sorted(p for p in set(files) if p.is_file() and "__pycache__" not in p.parts):
            digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def load_upstream(path: Path | None) -> dict[str, dict]:
    """Where findings were reported, by finding id, from [findings].upstream (schema kind `upstream`). The file is
    kept by hand; the harness copies each finding's entry into its `upstream` field and never writes the file."""
    if path is None:
        return {}
    document = json.loads(path.read_text(encoding="utf-8"))
    output.validate(document)
    return document["findings"]


def with_upstream(row: dict, upstream: dict[str, dict]) -> dict:
    """The finding row with its upstream link, or null, as every findings file carries it."""
    return {**row, "upstream": upstream.get(row["finding_id"])}


class RunDir:
    """A run directory under `config.results_dir`. `domain` names its documents (output.D1 by default), and `sources`
    are the trees its harness digest covers; `config` needs only path, sha256, results_dir and upstream_path, so
    another domain's configuration serves as well."""

    def __init__(self, config: Config, domain: output.Domain = output.D1, sources=None):
        self.config = config
        self.domain = domain
        self.upstream = load_upstream(config.upstream_path)
        self.started_at = now()
        self.source_digest = source_digest() if sources is None else source_digest(sources)
        # time.time() advances while the host sleeps; time.monotonic() does not on macOS. Their difference over a
        # run is how long the host was suspended, which makes every wall time in the run unreliable.
        self._wall0, self._mono0 = time.time(), time.monotonic()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        base = f"{stamp}-{config.sha256[:7]}"
        run_id, n = base, 1
        while (config.results_dir / run_id).exists():
            n += 1
            run_id = f"{base}-{n}"
        self.run_id = run_id
        self.path = config.results_dir / run_id
        self.path.mkdir(parents=True)
        self.blobs = BlobStore(self.path, domain)
        self._files: list[dict] = []
        shutil.copyfile(config.path, self.path / "config.toml")
        self._record("config.toml", "config", None, "The configuration this run used")

    def _record(self, relative: str, kind: str, schema: str | None, description: str, rows: int | None = None) -> None:
        entry = {"path": relative, "kind": kind, "schema": schema, "description": description}
        if rows is not None:
            entry["rows"] = rows
        self._files = [f for f in self._files if f["path"] != relative] + [entry]

    def write_json(self, relative: str, document: dict, description: str) -> None:
        output.write_json(self.path / relative, document)
        kind, _ = output.kind_of(document)
        self._record(relative, kind, document["$schema"], description)

    def write_jsonl(self, relative: str, rows: list[dict], kind: str, description: str) -> None:
        if kind == "finding":
            rows = [with_upstream(row, self.upstream) for row in rows]
        output.write_jsonl(self.path / relative, rows)
        self._record(relative, kind, self.domain.schema_name(kind), description, rows=len(rows))

    def write_bytes(self, relative: str, data: bytes, kind: str, schema: str | None, description: str) -> None:
        """A file in another published format, written byte for byte: a conformance-case draft, whose snapshot must
        keep the exact text that failed. `schema` names the spec schema it validates against, if any."""
        path = self.path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self._record(relative, kind, schema, description)

    def write_external(self, relative: str, document: dict, validator, kind: str, schema: str, description: str) -> None:
        """A document in another published format, validated with that format's own schema."""
        output.write_json(self.path / relative, document, validate_with=validator)
        self._record(relative, kind, schema, description)

    def harness(self, version: str = __version__) -> dict:
        """The manifest's harness entry: the harness is named for its domain's prefix."""
        return {
            "name": self.domain.prefix,
            "version": version,
            "source_digest": self.source_digest,
            "python": platform.python_version(),
        }

    def manifest(self, contract, adapters: dict, status: str, finished_at: str | None, container: dict | None = None,
                 ci: dict | None = None) -> dict:
        return {
            "$schema": output.schema_name("manifest"),
            "run_id": self.run_id,
            "status": status,
            "started_at": self.started_at,
            "finished_at": finished_at,
            "command": sys.argv,
            "harness": self.harness(),
            "config": {"path": str(self.config.path), "sha256": self.config.sha256, "copy": "config.toml"},
            "contract": {
                "path": str(contract.path),
                "pinned": contract.expected_commit,
                **contract.checkout.as_json(),
                "spec_draft": contract.spec_draft,
            },
            "adapters": adapters,
            "host": host(),
            "host_suspended_seconds": self.suspended_seconds(),
            "env_cells": env_cells(self.config),
            "container": container,
            "seed": None,
            "suites": self.config.suites,
            "settings": {"timeout_s": self.config.timeout_s, "concurrency": self.config.concurrency},
            "ci": ci,
        }

    def suspended_seconds(self) -> float:
        return round(max(0.0, (time.time() - self._wall0) - (time.monotonic() - self._mono0)), 1)

    def finalize(self, status: str, suites: list[str]) -> None:
        blob_index = self.blobs.write_index()
        self._record(blob_index, "blob", self.domain.schema_name("blob"),
                     "Every stored blob: digest, path, media type, size", rows=len(self.blobs))
        index = {
            "$schema": self.domain.schema_name("run-index"),
            "run_id": self.run_id,
            "status": status,
            "suites": suites,
            "files": sorted(self._files, key=lambda f: f["path"]),
            "blobs": {
                "index": blob_index,
                "layout": "blobs/sha256/<first 2 hex>/<64 hex>.<json|txt|bin>",
                "count": len(self.blobs),
            },
        }
        output.write_json(self.path / "index.json", index)
        update_runs_index(self.config.results_dir, latest=self.run_id, domain=self.domain)


def host() -> dict:
    """The machine a run ran on, as its manifest records it."""
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "cpus": os.cpu_count(),
        "platform": platform.platform(),
    }


def update_runs_index(results_dir: Path, latest: str | None = None, link: bool = True,
                      domain: output.Domain = output.D1) -> None:
    """Rebuild results/d1/index.json from the manifests on disk: every run, newest first; the newest finished run of
    each CI profile; and the contract and adapter commits each run was made from. With `link`, also point
    results/d1/latest at the newest run (`cwabench ci` points results/d1/<profile> at a profile's run the same way,
    ci.publish). A copy of the results served without symlinks resolves `latest` and `profiles` from the index."""
    runs = []
    for manifest_path in results_dir.glob("*/manifest.json"):
        if manifest_path.parent.is_symlink():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        adapters = manifest.get("adapters") or {}
        commits = {"contract": (manifest.get("contract") or {}).get("commit")}
        commits.update({name: (adapters[name] or {}).get("commit") for name in sorted(adapters)})
        runs.append(
            {
                "run_id": manifest["run_id"],
                "status": manifest["status"],
                "started_at": manifest["started_at"],
                "finished_at": manifest.get("finished_at"),
                "path": manifest["run_id"],
                "suites": manifest.get("suites", []),
                "adapters": sorted(adapters),
                "ci_profile": (manifest.get("ci") or {}).get("profile"),
                "commits": commits,
            }
        )
    runs.sort(key=lambda r: r["run_id"], reverse=True)
    newest = latest or (runs[0]["run_id"] if runs else None)
    profiles: dict[str, str] = {}
    for run in runs:  # newest first; a run still running, or stopped by an error, is not a profile's result
        name = run["ci_profile"]
        if name and name not in profiles and run["status"] not in ("running", "error"):
            profiles[name] = run["run_id"]
    output.write_json(
        results_dir / "index.json",
        {"$schema": domain.schema_name("runs-index"), "latest": newest, "profiles": dict(sorted(profiles.items())),
         "runs": runs},
    )
    if link and newest:
        link_path = results_dir / "latest"
        temporary = results_dir / ".latest.tmp"
        temporary.unlink(missing_ok=True)
        temporary.symlink_to(newest, target_is_directory=True)
        os.replace(temporary, link_path)


def env_cells(config: Config) -> list[str]:
    """Every environment cell this run's suites use."""
    from .container import PROFILES, ContainerError, variant
    from .suites.s2_repeatability import DEFAULT_CELLS

    cells = ["baseline"]
    container_on = config.section("container").get("enabled", True)
    if "S2" in config.suites:
        cells += [c for c in config.section("s2").get("cells", DEFAULT_CELLS) if c not in cells]
        if container_on and config.section("s2").get("container", True):
            cells += [c.name for c in PROFILES["linux"].cells]
            for name in config.section("s2").get("matrix", []):
                try:
                    cells.append(variant(config, name).cell)
                except ContainerError:
                    cells.append(f"variant:{name}")
    if "S10" in config.suites:
        if config.section("s10").get("macos_sandbox", True):
            cells.append("sandbox:no-network")
        if container_on:
            cells += [c.name for c in PROFILES["linux-isolated"].cells]
    return cells
