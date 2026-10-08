"""`cwabench fixture`: a run directory trimmed for consumers' tests (domain-1-plan.md, 12.6).

A fixture is the run as it was written, cut down: every document is kept whole except the ones that list rows, frames
or snapshots, which keep a sample; only a few sweep cells stay, with their timelines; and only the blobs the kept
files name are copied. The result validates like a run, says in its index.json what it was cut from (`fixture`), and
is listed in <fixtures>/index.json, a runs index written with no `latest` link, so a consumer points at the fixtures
directory exactly as it points at results/d1.
"""
from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from . import output
from .rundir import now, update_runs_index, with_upstream
from .validate import validate_run

DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
SWEEPS = "suites/S7/sweeps/"


def sample(items: list, count: int) -> list:
    """Up to `count` of the items, spread evenly, the first and the last always among them."""
    if count <= 0 or not items:
        return []
    if len(items) <= count:
        return list(items)
    if count == 1:
        return items[:1]
    picks = sorted({round(i * (len(items) - 1) / (count - 1)) for i in range(count)})
    return [items[i] for i in picks]


def write_fixture(run_dir: Path, out_dir: Path, *, rows: int = 12, frames: int = 16, sweeps: int = 2,
                  upstream: dict[str, dict] | None = None,
                  log: Callable[[str], None] = lambda message: None) -> tuple[Path, list[str]]:
    """Write <out_dir>/<run id> from the run at run_dir, and return it with validate_run's problems. `upstream` (the
    [findings].upstream map) refreshes each kept finding's link, as a new run would carry it."""
    run_dir = run_dir.resolve()
    index = _load(run_dir / "index.json")
    target = out_dir / run_dir.name
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    files: list[dict] = []
    texts: list[str] = []  # every kept file's text, scanned for the blobs it names
    kept_sweeps, kept_timelines = 0, set()

    def record(entry: dict, text: str, count: int | None = None) -> None:
        kept = {k: v for k, v in entry.items() if k != "rows"}
        if count is not None:
            kept["rows"] = count
        files.append(kept)
        texts.append(text)

    def write_document(entry: dict, document: dict) -> None:
        output.write_json(target / entry["path"], document)
        record(entry, output.dumps(document))

    def copy(entry: dict) -> None:
        data = (run_dir / entry["path"]).read_bytes()
        path = target / entry["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        record(entry, data.decode("utf-8", errors="replace"))

    blob_index = index["blobs"]["index"]
    for entry in sorted(index["files"], key=lambda e: (not e["path"].startswith(SWEEPS), e["path"])):  # sweeps first
        path, kind, source = entry["path"], entry["kind"], run_dir / entry["path"]
        if path == blob_index:
            continue  # rebuilt below, from the blobs the fixture keeps
        if path.startswith(SWEEPS):
            if kept_sweeps >= sweeps:
                continue
            document = _load(source)
            for block in document["adapters"].values():
                block["frames"] = sample(block["frames"], frames)
            kept_timelines.update(document["timelines"])
            kept_sweeps += 1
            write_document(entry, document)
        elif kind == "timeline":
            if path in kept_timelines:
                write_document(entry, _load(source))
        elif kind == "perf-summary":  # one shape's cells and fits; startup, throughput and time to refusal whole
            document = _load(source)
            shapes = sorted({c["cell"]["shape"] for c in document["cells"]})[:1]
            document["cells"] = [c for c in document["cells"] if c["cell"]["shape"] in shapes]
            document["fits"] = [f for f in document["fits"] if f["shape"] in shapes]
            write_document(entry, document)
        elif kind == "corpus-index":
            document = _load(source)
            document["snapshots"] = document["snapshots"][:rows]
            write_document(entry, document)
        elif kind == "goldens":
            document = _load(source)
            document["entries"] = document["entries"][:rows]
            write_document(entry, document)
        elif path.endswith(".jsonl"):
            lines = source.read_text(encoding="utf-8").splitlines()
            kept = [json.loads(line) for line in (lines if kind == "finding" else lines[:rows])]
            if kind == "finding" and upstream is not None:
                kept = [with_upstream(row, upstream) for row in kept]
            output.write_jsonl(target / path, kept)
            record(entry, "\n".join(output.dumps(row, compact=True) for row in kept), count=len(kept))
        elif kind in ("config", "conformance-draft", "conformance-report") or not path.endswith(".json"):
            copy(entry)  # another format, or the spec's own: byte for byte
        else:
            write_document(entry, _load(source))

    # The blobs any kept file names, from the run's own blob index.
    named = {digest for text in texts for digest in DIGEST.findall(text)}
    blobs = []
    for line in (run_dir / blob_index).read_text(encoding="utf-8").splitlines():
        blob = json.loads(line)
        if blob["digest"] in named:
            path = target / blob["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(run_dir / blob["path"], path)
            blobs.append(blob)
    output.write_jsonl(target / blob_index, blobs)
    files.append({**next(e for e in index["files"] if e["path"] == blob_index), "rows": len(blobs)})

    output.write_json(target / "index.json", {
        "$schema": output.schema_name("run-index"),
        "run_id": index["run_id"],
        "status": index["status"],
        "suites": index["suites"],
        "files": sorted(files, key=lambda f: f["path"]),
        "blobs": {**index["blobs"], "count": len(blobs)},
        "fixture": {"of": index["run_id"], "created_at": now(), "rows": rows, "frames": frames, "sweeps": sweeps},
    })
    update_runs_index(out_dir, link=False)
    log(f"fixture {target}: {len(files)} file(s), {len(blobs)} blob(s)")
    return target, validate_run(target)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
