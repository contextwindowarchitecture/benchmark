"""S12 · Regression and consensus goldens (domain-1-plan.md, 7.12).

A snapshot on which every adapter gives the same answer in S1, S6, S8 or S9, and every answer passes the trace audit, yields a
consensus golden: its outcome, refusal reason, payload hash and normalized-trace hash, keyed by the snapshot's bytes.
Each run writes its candidates; `cwabench goldens accept` adopts them deliberately, never automatically. Each run
then compares every adapter's answers with the adopted goldens:

- match: the answer equals the golden;
- spec_change: it differs, the contract commit changed since the golden, and so did the case's expected files,
  so the difference is expected and the goldens need accepting again;
- regression: it differs with no such change in the specification;
- new: the snapshot has no golden yet. A golden whose snapshot is not in this run is reported as removed.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from .. import gitinfo, metrics, output
from ..rundir import now
from . import SuiteContext, SuiteResult

ID = "S12"
TITLE = "Regression and consensus goldens"
SOURCES = ("S1", "S6", "S8", "S9")  # suites whose rows are result-rows: one answer per snapshot and adapter
TRACE_HASH = "sha256 of the normalized trace serialized with sorted keys, as S1's trace_normalized blob"


def goldens_path(ctx: SuiteContext) -> Path:
    return ctx.config.root / ctx.config.section("s12").get("goldens", "goldens/d1-goldens.json")


def _answer(row: dict) -> dict:
    return {"outcome": row["outcome"], "refusal_reason": row["refusal_reason"], "payload_hash": row["payload_hash"],
            "trace_hash": row["trace_normalized"].removeprefix("sha256:") if row["trace_normalized"] else None}


def load_goldens(path: Path) -> dict | None:
    if not path.is_file():
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    output.validate(document)
    return document


def run(ctx: SuiteContext) -> SuiteResult:
    started = now()
    by_snapshot: dict[str, dict[str, dict]] = defaultdict(dict)
    sources: dict[str, set] = {}
    for suite in SOURCES:
        path = ctx.run.path / "suites" / suite / "results.jsonl"
        if not path.is_file():
            continue
        sources[suite] = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            digest = row["snapshot"].removeprefix("sha256:")
            sources[suite].add(digest)
            by_snapshot[digest].setdefault(row["adapter"], row)

    adapters = list(ctx.adapters)
    candidates, no_consensus = [], []
    for digest, per in sorted(by_snapshot.items(), key=lambda kv: next(iter(kv[1].values()))["case_id"]):
        first = next(iter(per.values()))
        answers = {a: _answer(r) for a, r in per.items()}
        agree = len({json.dumps(v, sort_keys=True) for v in answers.values()}) == 1 and set(per) == set(adapters)
        audited = all(r["audit"] is None or r["audit"]["status"] == "pass" for r in per.values())
        faulted = any(r["outcome"] in ("crashed", "timeout", "invalid_output", "unsupported") for r in per.values())
        if agree and audited and not faulted:
            candidates.append({"snapshot": digest, "case_id": first["case_id"], "case_kind": first["case_kind"],
                               "corpus": first["corpus"], **answers[adapters[0]]})
        else:
            no_consensus.append({"case_id": first["case_id"], "snapshot": digest,
                                 "reason": "adapters disagree" if not agree else "audit failed" if not audited
                                 else "an adapter faulted"})

    contract_commit = ctx.contract.checkout.commit
    adapter_commits = {a: ctx.adapters[a].checkout.commit for a in adapters}
    candidate_doc = {
        "$schema": output.schema_name("goldens"),
        "from_run": ctx.run.run_id,
        "created_at": now(),
        "contract_commit": contract_commit,
        "adapters": adapter_commits,
        "trace_hash": TRACE_HASH,
        "entries": candidates,
    }
    base = f"suites/{ID}"
    ctx.run.write_json(f"{base}/candidate-goldens.json", candidate_doc,
                       "S12: consensus goldens from this run; adopt with `cwabench goldens accept`")

    path = goldens_path(ctx)
    goldens = load_goldens(path)
    rows, findings = [], []
    counts = Counter()
    if goldens is not None:
        known = {e["snapshot"]: e for e in goldens["entries"]}
        changed_spec = goldens["contract_commit"] != contract_commit
        for digest, per in by_snapshot.items():
            golden = known.get(digest)
            for adapter, row in per.items():
                answer = _answer(row)
                if golden is None:
                    kind = "new"
                else:
                    differs = [k for k in ("outcome", "refusal_reason", "payload_hash", "trace_hash")
                               if answer[k] != golden[k]]
                    if not differs:
                        kind = "match"
                    elif changed_spec and _case_changed(ctx, row, goldens["contract_commit"], contract_commit):
                        kind = "spec_change"
                    else:
                        kind = "regression"
                counts[(adapter, kind)] += 1
                rows.append({
                    "$schema": output.schema_name("drift-row"),
                    "run_id": ctx.run.run_id,
                    "suite": ID,
                    "case_id": row["case_id"],
                    "snapshot": row["snapshot"],
                    "adapter": adapter,
                    "drift": kind,
                    "fields": [] if golden is None or kind == "match" else differs,
                    "golden": {k: golden[k] for k in ("outcome", "refusal_reason", "payload_hash", "trace_hash")}
                    if golden else None,
                    "actual": answer,
                    "adapter_commit_changed": goldens["adapters"].get(adapter) != adapter_commits.get(adapter),
                    "finding": None,
                })
        regressions = defaultdict(list)
        for r in rows:
            if r["drift"] == "regression":
                regressions[r["adapter"]].append(r)
        for adapter, members in sorted(regressions.items()):
            signature = {"suite": ID, "adapter": adapter, "goldens_from": goldens["from_run"]}
            finding_id = hashlib.sha256(output.dumps(signature, compact=True).encode()).hexdigest()[:12]
            for r in members:
                r["finding"] = finding_id
            cases = sorted({r["case_id"] for r in members})
            findings.append({
                "$schema": output.schema_name("finding"),
                "finding_id": finding_id,
                "run_id": ctx.run.run_id,
                "suite": ID,
                "adapter": adapter,
                "case_id": cases[0],
                "oracle": "golden",
                "checks": sorted({f for r in members for f in r["fields"]}),
                "severity": "error",
                "summary": (f"{adapter} no longer matches the goldens from run {goldens['from_run']} on "
                            f"{len(cases)} snapshot(s) with no change in the specification, e.g. "
                            f"{', '.join(cases[:3])}")[:1000],
                "first_pointer": None,
                "requirements": ["R-23"],
                "occurrences": len(members),
                "reproducer": {"snapshot": members[0]["snapshot"], "spec_path": None},
            })
        present = set(by_snapshot)
        removed = [{"case_id": e["case_id"], "snapshot": e["snapshot"]} for e in goldens["entries"]
                   if e["snapshot"] not in present]
    else:
        removed = []

    suite_metrics = [metrics.rate(
        "s12.consensus_rate", "Snapshots with a consensus golden", len(candidates), len(by_snapshot), suite=ID,
        description="Snapshots on which every adapter agrees and every answer passes the trace audit.")]
    for adapter in adapters:
        compared = sum(counts[(adapter, k)] for k in ("match", "spec_change", "regression"))
        suite_metrics.append(metrics.rate(
            "s12.golden_match", "Answers matching the goldens", counts[(adapter, "match")], compared, suite=ID,
            adapter=adapter, target=1.0 if goldens else None,
            description="Answers equal to the adopted golden for their snapshot. Null until goldens are accepted."))
        suite_metrics.append(metrics.count(
            "s12.regressions", "Drift with no specification change", counts[(adapter, "regression")], maximum=0,
            suite=ID, adapter=adapter, description="Answers that no longer match their golden although the case "
                                                   "did not change in the specification."))

    status = "fail" if findings else "pass"
    if goldens is None:
        status = "partial"
    ctx.run.write_jsonl(f"{base}/results.jsonl", rows, "drift-row", "S12: one row per snapshot × adapter against the goldens")
    ctx.run.write_jsonl(f"{base}/findings.jsonl", findings, "finding", "S12: one row per adapter with regressions")
    summary_path = f"{base}/summary.json"
    ctx.run.write_json(summary_path, {
        "$schema": output.schema_name("suite-summary"),
        "run_id": ctx.run.run_id,
        "suite": ID,
        "title": TITLE,
        "status": status,
        "started_at": started,
        "finished_at": now(),
        "requirements": ["R-23"],
        "corpora": [{"id": suite, "count": len(digests)} for suite, digests in sources.items()],
        "adapters": [],
        "metrics": suite_metrics,
        "files": {"results": f"{base}/results.jsonl", "findings": f"{base}/findings.jsonl"},
        "goldens": {
            "path": str(path),
            "adopted": None if goldens is None else {
                "from_run": goldens["from_run"], "created_at": goldens["created_at"],
                "contract_commit": goldens["contract_commit"], "entries": len(goldens["entries"])},
            "candidates": f"{base}/candidate-goldens.json",
            "candidate_count": len(candidates),
            "no_consensus": no_consensus,
            "drift": [{"adapter": a, "drift": k, "count": n} for (a, k), n in sorted(counts.items())],
            "removed": removed,
            "note": None if goldens else "No goldens adopted yet: run `cwabench goldens accept` to adopt this run's.",
        },
    }, "S12: consensus, drift against adopted goldens")
    ctx.log(f"S12: {len(candidates)} candidate golden(s); "
            + ("no goldens adopted yet" if goldens is None else f"{sum(n for (a, k), n in counts.items() if k == 'regression')} regression(s)"))
    return SuiteResult(ID, TITLE, status, summary_path, suite_metrics, findings, [])


def _case_changed(ctx: SuiteContext, row: dict, old: str, new: str) -> bool:
    """Whether the case's expected files changed in the specification between two commits."""
    kind = "cases" if row["case_kind"] == "case" else "rejections"
    if not row["corpus"].startswith("conformance."):
        return False
    return gitinfo.unchanged_between(ctx.contract.path, old, new, f"conformance/{kind}/{row['case_id']}") is False


def accept(config, run_dir: Path) -> tuple[Path, int]:
    """Adopt a run's candidate goldens."""
    source = run_dir / "suites" / ID / "candidate-goldens.json"
    if not source.is_file():
        raise FileNotFoundError(f"{source} does not exist; the run did not include S12")
    document = json.loads(source.read_text(encoding="utf-8"))
    output.validate(document)
    target = config.root / config.section("s12").get("goldens", "goldens/d1-goldens.json")
    output.write_json(target, document)
    return target, len(document["entries"])
