"""The runner S6, S8 and S9 share: labeled corpora through every adapter, each answer judged three ways, by its
label, by the trace auditor and by agreement among the adapters."""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from .. import adapters as adapters_mod
from .. import corpora, metrics, output, traces
from ..adapters import FAULTS
from ..oracles import label as label_oracle
from ..oracles.auditor import audit
from ..rundir import now
from . import Coverage, SuiteContext, SuiteResult


def _finding(ctx, suite: str, oracle: str, checks: list[str], snapshot, adapter, summary: str, extra=None) -> dict:
    signature = {"suite": suite, "oracle": oracle, "case": snapshot.case_id, "corpus": snapshot.corpus,
                 "adapter": adapter, "checks": checks}
    return {
        "$schema": output.schema_name("finding"),
        "finding_id": hashlib.sha256(output.dumps(signature, compact=True).encode()).hexdigest()[:12],
        "run_id": ctx.run.run_id,
        "suite": suite,
        "adapter": adapter,
        **(extra or {}),
        "case_id": snapshot.case_id,
        "oracle": oracle,
        "checks": checks,
        "severity": "error",
        "summary": summary[:1000],
        "first_pointer": None,
        "requirements": list(snapshot.rules),
        "occurrences": 1,
        "reproducer": {"snapshot": ctx.run.blobs.put(snapshot.data, "application/json"), "spec_path": None},
    }


def run_labeled(ctx: SuiteContext, suite: str, title: str, corpus_names: list[str], requirements: list[str],
                extra_summary: dict | None = None) -> SuiteResult:
    started = now()
    corpus = corpora.load(ctx.contract, corpus_names)
    jobs = [(s, a) for a in ctx.adapters.values() for s in corpus]
    ctx.log(f"{suite}: {len(corpus)} labeled snapshots × {len(ctx.adapters)} adapter(s) = {len(jobs)} invocations")
    trace_validator = ctx.contract.validator("trace.schema.json")
    blobs = ctx.run.blobs

    def work(job):
        snapshot, adapter = job
        invocation = adapters_mod.invoke(adapter, snapshot.data, ctx.config.timeout_s, ctx.config.root)
        outcome = adapters_mod.classify(invocation)
        checks = label_oracle.judge(snapshot.label, outcome)
        audited = None
        if outcome.kind in ("assembled", "refused") and isinstance(outcome.trace, dict):
            result = audit(ctx.contract, snapshot.data, outcome.payload, outcome.trace)
            checks += result.as_checks()
            audited = {"status": result.status, "failed": result.failed}
            errors = list(trace_validator.iter_errors(outcome.trace))
            checks.append({"oracle": "schema", "id": "trace_schema", "status": "fail" if errors else "pass",
                           **({"detail": errors[0].message[:300]} if errors else {})})
        failed_label = [c for c in checks if c["oracle"] == "label" and c["status"] == "fail"]
        trace = outcome.trace
        result_ = trace.get("result") if isinstance(trace, dict) else None
        budget = trace.get("budget") if isinstance(trace, dict) else None
        tokens = result_.get("input_tokens") if isinstance(result_, dict) else None
        return {
            "$schema": output.schema_name("result-row"),
            "run_id": ctx.run.run_id,
            "suite": suite,
            "corpus": snapshot.corpus,
            "case_id": snapshot.case_id,
            "case_kind": snapshot.kind,
            "snapshot": blobs.put(snapshot.data, "application/json"),
            "label": blobs.put_json(snapshot.label),
            "adapter": adapter.name,
            "env_cell": "baseline",
            "repetition": 0,
            "expected_outcome": snapshot.label["outcome"],
            "expected_trace": None,
            "expected_payload": None,
            "outcome": outcome.kind,
            "exit_code": invocation.exit_code,
            "refusal_reason": outcome.refusal_reason,
            "unsupported": [f"{k} {c}" for k, c in outcome.unsupported],
            "payload": blobs.put_text(outcome.payload) if outcome.payload is not None else None,
            "payload_hash": hashlib.sha256(outcome.payload).hexdigest() if outcome.payload is not None else None,
            "trace": blobs.put_json(trace) if trace is not None else None,
            "trace_normalized": blobs.put_json(traces.normalize(trace)) if trace is not None else None,
            "stderr": blobs.put_text(invocation.stderr) if invocation.stderr and outcome.kind in FAULTS else None,
            "input_tokens": tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else None,
            "charged_tokens": traces.charged_tokens(trace) if isinstance(trace, dict) else None,
            "budget_input": budget.get("input") if isinstance(budget, dict) else None,
            "wall_ms": round(invocation.wall_ms, 3),
            "net_ms": None,
            "verdict": "failed" if failed_label else "passed",
            "detail": failed_label[0].get("detail") if failed_label else None,
            "audit": audited,
            "checks": checks,
            "differences": [],
            "requirements": list(snapshot.rules),
            "coverage_tags": traces.coverage_tags(trace if isinstance(trace, dict) else None, None),
            "finding": None,
        }

    with ThreadPoolExecutor(max_workers=ctx.config.concurrency) as pool:
        rows = list(pool.map(work, jobs))
    by_digest = {s.digest: s for s in corpus}

    # Differential: the adapters must agree on each snapshot.
    by_snapshot = defaultdict(dict)
    for row in rows:
        by_snapshot[row["snapshot"]][row["adapter"]] = row
    agreeing, disagreements = 0, []
    for digest, per in by_snapshot.items():
        signatures = {a: (r["outcome"], r["refusal_reason"], r["payload_hash"], r["trace_normalized"])
                      for a, r in per.items()}
        groups = defaultdict(list)
        for a, sig in signatures.items():
            groups[sig].append(a)
        agree = len(groups) <= 1
        agreeing += agree
        for a, row in per.items():
            others = [o for o in per if signatures[o] != signatures[a]]
            row["checks"].append({"oracle": "differential", "id": "agreement", "status": "fail" if others else "pass",
                                  **({"detail": "differs from " + ", ".join(sorted(others))} if others else {})})
        if not agree:
            disagreements.append({"case_id": next(iter(per.values()))["case_id"],
                                  "groups": sorted(groups.values(), key=len, reverse=True)})

    findings = []
    for row in rows:
        snapshot = by_digest[row["snapshot"].removeprefix("sha256:")]
        if row["verdict"] == "failed":
            finding = _finding(ctx, suite, "label", [c["id"] for c in row["checks"] if c["oracle"] == "label"
                                                     and c["status"] == "fail"], snapshot, row["adapter"],
                               f"{row['adapter']} on {snapshot.corpus}/{snapshot.case_id}: {row['detail']}")
            row["finding"] = finding["finding_id"]
            findings.append(finding)
        if row["audit"] and row["audit"]["status"] == "fail":
            detail = next(c.get("detail", "") for c in row["checks"] if c["oracle"] == "auditor" and c["status"] == "fail")
            finding = _finding(ctx, suite, "auditor", row["audit"]["failed"], snapshot, row["adapter"],
                               f"{row['adapter']} output for {snapshot.case_id} breaks {', '.join(row['audit']['failed'])}: {detail}")
            row["finding"] = row["finding"] or finding["finding_id"]
            findings.append(finding)
        if row["outcome"] in FAULTS:
            findings.append(_finding(ctx, suite, "label", ["fault"], snapshot, row["adapter"],
                                     f"{row['adapter']} {row['outcome']} on {snapshot.case_id}"))
    for d in disagreements:
        snapshot = next(s for s in corpus if s.case_id == d["case_id"])
        findings.append(_finding(ctx, suite, "differential", ["agreement"], snapshot, None,
                                 f"adapters disagree on {snapshot.corpus}/{snapshot.case_id}: "
                                 + " vs ".join("/".join(g) for g in d["groups"]), {"adapters": sorted(ctx.adapters)}))

    suite_metrics, per_corpus = [], []
    for adapter in ctx.adapters:
        mine = [r for r in rows if r["adapter"] == adapter]
        suite_metrics.append(metrics.rate(f"{suite.lower()}.label_rate", "Answers matching their labels",
                                          sum(r["verdict"] == "passed" for r in mine), len(mine), suite=suite,
                                          adapter=adapter, description="Every decision the label states is recorded, "
                                                                       "and no other exclusion."))
        audited = [r for r in mine if r["audit"]]
        suite_metrics.append(metrics.rate(f"{suite.lower()}.audit_rate", "Outputs passing the trace audit",
                                          sum(r["audit"]["status"] == "pass" for r in audited), len(audited),
                                          suite=suite, adapter=adapter))
    suite_metrics.append(metrics.rate(f"{suite.lower()}.agreement", "Snapshots on which every adapter agrees",
                                      agreeing, len(by_snapshot), suite=suite))
    for name in corpus_names:
        for adapter in ctx.adapters:
            mine = [r for r in rows if r["corpus"] == name and r["adapter"] == adapter]
            per_corpus.append({"corpus": name, "adapter": adapter, "snapshots": len(mine),
                               "passed": sum(r["verdict"] == "passed" for r in mine),
                               "outcomes": dict(sorted(Counter(r["outcome"] for r in mine).items()))})

    status = "fail" if findings else ("partial" if ctx.unavailable else "pass")
    base = f"suites/{suite}"
    ctx.run.write_jsonl(f"{base}/results.jsonl", rows, "result-row", f"{suite}: one row per labeled snapshot × adapter")
    ctx.run.write_jsonl(f"{base}/findings.jsonl", findings, "finding", f"{suite}: label, audit and agreement failures")
    summary_path = f"{base}/summary.json"
    ctx.run.write_json(summary_path, {
        "$schema": output.schema_name("suite-summary"),
        "run_id": ctx.run.run_id,
        "suite": suite,
        "title": title,
        "status": status,
        "started_at": started,
        "finished_at": now(),
        "requirements": requirements,
        "corpora": [{"id": n, "count": sum(s.corpus == n for s in corpus)} for n in corpus_names],
        "adapters": [],
        "metrics": suite_metrics,
        "files": {"results": f"{base}/results.jsonl", "findings": f"{base}/findings.jsonl"},
        "labeled": {"by_corpus": per_corpus, "disagreements": disagreements, **(extra_summary or {})},
    }, f"{suite}: label, audit and agreement results per corpus and adapter")
    coverage = [Coverage(r["adapter"], r["requirements"], by_digest[r["snapshot"].removeprefix("sha256:")]
                         .label_reasons(), r["coverage_tags"], r["verdict"] == "passed") for r in rows]
    ctx.log(f"{suite}: {sum(r['verdict'] == 'passed' for r in rows)}/{len(rows)} labels matched, "
            f"{len(findings)} finding(s)")
    return SuiteResult(suite, title, status, summary_path, suite_metrics, findings, coverage)
