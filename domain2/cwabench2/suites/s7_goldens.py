"""S7 · Goldens (domain-2-plan.md, 8), with Domain 1's S12 semantics.

A probe snapshot on which every adapter in S1 gave the same answer, equal to the prediction and passing the audit,
yields a candidate golden: its outcome, refusal reason, payload hash and normalized-trace hash, keyed by the
snapshot's bytes. Only probes: they are the payloads a model is sent, and the turn frames would multiply the file
without guarding anything S2 reads. Each run writes its candidates; `cwabench --domain 2 goldens accept` adopts them
deliberately, never automatically. Each run then compares every adapter's answer on every probe with the goldens:

- match: the answer equals the golden;
- spec_change: it differs, and the contract commit changed since the golden was adopted, so the goldens need
  accepting again;
- regression: it differs with no such change;
- new: the snapshot has no golden yet. A golden whose snapshot is not in this run is counted as removed.

Rows are written for every answer that is not a match.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from cwabench.rundir import now

from .. import metrics, output
from . import SuiteContext, SuiteResult, finding

ID = "S7"
TITLE = "Goldens"
TRACE_HASH = "sha256 of the normalized trace as JCS (Domain 1's differential signature)"
FIELDS = ("outcome", "refusal_reason", "payload_hash", "trace_hash")


def load(path: Path | None) -> dict | None:
    if path is None or not path.is_file():
        return None
    document = json.loads(path.read_text(encoding="utf-8"))
    output.validate(document)
    return document


def _answer(answer: dict) -> dict:
    return {k: answer[k] for k in FIELDS}


def run(ctx: SuiteContext) -> SuiteResult:
    started = now()
    rows_in = [r for r in ctx.shared.get("s1_rows", []) if r["point"] == "probe"]
    commits = {name: adapter.checkout.commit for name, adapter in ctx.adapters.items()}
    candidates = []
    for row in rows_in:
        answers = row["answers"]
        if (row["agree"] and answers and len(answers) == len(ctx.adapters)
                and all(a["matches_prediction"] and not a["audit_failed"] for a in answers)):
            candidates.append({"snapshot": row["snapshot_sha256"], "case_id": row["case_id"],
                               "conversation": row["conversation"], "arm": row["arm"], "probe_id": row["probe_id"],
                               "tier": row["tier"], **_answer(answers[0])})
    candidates_path = f"suites/{ID}/candidate-goldens.json"
    ctx.run.write_json(candidates_path, {
        "$schema": output.schema_name("goldens"),
        "from_run": ctx.run.run_id,
        "created_at": now(),
        "contract_commit": ctx.contract.checkout.commit,
        "adapters": commits,
        "trace_hash": TRACE_HASH,
        "entries": candidates,
    }, "Candidate goldens: probe answers every adapter agreed on, as predicted and audited clean")

    goldens = load(ctx.config.goldens)
    tally = Counter()
    drift_rows, findings = [], []
    if goldens is not None:
        by_snapshot = {e["snapshot"]: e for e in goldens["entries"]}
        seen = set()
        spec_moved = goldens["contract_commit"] != ctx.contract.checkout.commit
        for row in rows_in:
            golden = by_snapshot.get(row["snapshot_sha256"])
            if golden is not None:
                seen.add(row["snapshot_sha256"])
            for answer in row["answers"]:
                actual = _answer(answer)
                if golden is None:
                    drift, fields = "new", []
                else:
                    fields = [f for f in FIELDS if golden[f] != actual[f]]
                    drift = "match" if not fields else ("spec_change" if spec_moved else "regression")
                tally[drift] += 1
                if drift == "match":
                    continue
                finding_id = None
                if drift == "regression":
                    signature = {"suite": ID, "oracle": "golden", "adapter": answer["adapter"], "arm": row["arm"],
                                 "fields": fields}
                    found = finding(ctx, ID, signature, case_id=row["case_id"], oracle="golden", checks=fields,
                                    summary=f"{answer['adapter']} on {row['case_id']} differs from its golden at "
                                            f"{', '.join(fields)}")
                    found["adapter"] = answer["adapter"]
                    known = next((f for f in findings if f["finding_id"] == found["finding_id"]), None)
                    if known is None:
                        findings.append(found)
                    else:
                        known["occurrences"] += 1
                    finding_id = found["finding_id"]
                drift_rows.append({
                    "$schema": output.schema_name("drift-row"),
                    "run_id": ctx.run.run_id,
                    "suite": ID,
                    "case_id": row["case_id"],
                    "snapshot": f"sha256:{row['snapshot_sha256']}",
                    "adapter": answer["adapter"],
                    "drift": drift,
                    "fields": fields,
                    "golden": None if golden is None else {k: golden[k] for k in FIELDS},
                    "actual": actual,
                    "adapter_commit_changed": golden is not None
                    and goldens["adapters"].get(answer["adapter"]) != commits.get(answer["adapter"]),
                    "finding": finding_id,
                })
        tally["removed"] = len(set(by_snapshot) - seen)
    ctx.run.write_jsonl(f"suites/{ID}/drift.jsonl", drift_rows, "drift-row",
                        "Every probe answer that does not match its golden: new, spec_change or regression")

    compared = tally["match"] + tally["spec_change"] + tally["regression"]
    suite_metrics = [
        metrics.rate("s7.golden_match", "Probe answers equal to their golden", tally["match"], compared, suite=ID,
                     description="na until goldens are adopted"),
        metrics.count("s7.regressions", "Answers that drifted from their golden", tally["regression"], maximum=0,
                      suite=ID),
        metrics.count("s7.candidates", "Candidate goldens this run", len(candidates), suite=ID),
        metrics.count("s7.new", "Probe answers with no golden yet", tally["new"], suite=ID),
        metrics.count("s7.removed", "Goldens whose snapshot this run did not make", tally["removed"], suite=ID),
    ]
    status = "fail" if findings else "pass"
    summary_path = f"suites/{ID}/summary.json"
    ctx.run.write_json(summary_path, {
        "$schema": output.schema_name("suite-summary"),
        "run_id": ctx.run.run_id,
        "suite": ID,
        "title": TITLE,
        "status": status,
        "started_at": started,
        "finished_at": now(),
        "requirements": ["R-23"],
        "corpora": [{"id": family, "count": len(scripts)} for family, scripts in ctx.conversations.items()],
        "adapters": [{"adapter": n, "status": "pass", "error": None} for n in ctx.adapters],
        "metrics": suite_metrics,
        "files": {"results": f"suites/{ID}/drift.jsonl", "findings": "findings.jsonl"},
        "goldens": {"path": str(ctx.config.goldens) if goldens is not None else None,
                    "contract_commit": goldens["contract_commit"] if goldens is not None else None,
                    "candidates": candidates_path, "drift": {k: tally[k] for k in
                                                             ("match", "spec_change", "regression", "new", "removed")}},
    }, "S7's comparison of the probe answers with the adopted goldens")
    return SuiteResult(ID, TITLE, status, summary_path, suite_metrics, findings)


def accept(config, run_dir: Path) -> tuple[Path, int]:
    """Adopt a run's candidate goldens as the baseline."""
    source = run_dir / "suites" / ID / "candidate-goldens.json"
    if not source.is_file():
        raise FileNotFoundError(f"{source} does not exist; the run did not include S7")
    document = json.loads(source.read_text(encoding="utf-8"))
    output.validate(document)
    output.write_json(config.goldens, document)
    return config.goldens, len(document["entries"])
