"""S1 · Conformance replay (domain-1-plan.md, 7.1).

Every published case and rejection runs through every adapter, and is judged exactly as conformance/README.md says:
a case passes when the payload bytes and the normalized trace match; a rejection passes when the adapter rejects the
snapshot before assembly. Each adapter also gets a conformance-report.json in the spec's own format, compared with
the report its repository commits.
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from .. import adapters as adapters_mod
from .. import gitinfo, metrics, output, traces
from ..adapters import FAULTS, Adapter, Outcome
from ..contract import OPTIONAL_COMPONENTS, Case
from ..oracles import differential
from ..oracles.auditor import audit
from ..rundir import now
from . import Coverage, SuiteContext, SuiteResult

ID = "S1"
_ORACLES = {"expected": ("expected", "schema"), "auditor": ("auditor",)}
TITLE = "Conformance replay"
CORPUS = {"case": "conformance.cases", "rejection": "conformance.rejections"}


def _check(id: str, status: str, detail: str | None = None) -> dict:
    out = {"oracle": "expected" if id != "trace_schema" else "schema", "id": id, "status": status}
    if detail:
        out["detail"] = detail
    return out


def _skippable(case: Case, outcome: Outcome) -> str | None:
    """The skip detail when an exit 3 names only optional components the case uses, else None (README, Reporting
    results: an exit 3 that names a required component, or none, fails the case)."""
    if not outcome.unsupported:
        return None
    used = case.components()
    for kind, component in outcome.unsupported:
        if component not in OPTIONAL_COMPONENTS.get(kind, ()) or used.get(kind) != component:
            return None
    return "; ".join(f"{kind} {component} is not provided" for kind, component in outcome.unsupported)


def _describe(outcome: Outcome) -> str:
    if outcome.kind == "refused":
        return f"refused with {outcome.refusal_reason}"
    if outcome.kind == "unsupported":
        named = ", ".join(f"{k} {c}" for k, c in outcome.unsupported) or "no component"
        return f"exit 3 naming {named}"
    if outcome.problem:
        return f"{outcome.kind}: {outcome.problem[:500]}"
    return outcome.kind


def judge_case(case: Case, outcome: Outcome, trace_validator) -> tuple[str, list[dict], str | None, list[dict]]:
    """(verdict, checks, detail, differences) for a case directory."""
    if outcome.kind == "unsupported":
        skip = _skippable(case, outcome)
        if skip:
            return "skipped", [_check("outcome", "skipped", skip)], skip, []
    expected = case.expected_outcome
    checks = [_check("outcome", "pass" if outcome.kind == expected else "fail",
                     None if outcome.kind == expected else f"expected {expected}, got {_describe(outcome)}")]
    differences: list[dict] = []
    if outcome.kind in ("assembled", "refused"):
        if case.expected_payload is None:
            payload_ok = outcome.payload is None
            payload_detail = None if payload_ok else "a payload was returned where none is expected"
        else:
            payload_ok = outcome.payload == case.expected_payload
            payload_detail = None if payload_ok else _first_byte_difference(case.expected_payload, outcome.payload)
        checks.append(_check("payload", "pass" if payload_ok else "fail", payload_detail))

        found = traces.diff(traces.normalize(case.expected_trace or {}), traces.normalize(outcome.trace))
        differences = [d.as_json() for d in found]
        checks.append(_check("trace", "pass" if not found else "fail",
                             None if not found else f"{len(found)} difference(s), first at {found[0].pointer or '/'}"))

        errors = list(trace_validator.iter_errors(outcome.trace))
        checks.append(_check("trace_schema", "pass" if not errors else "fail",
                             None if not errors else errors[0].message[:300]))
    else:
        for id in ("payload", "trace", "trace_schema"):
            checks.append(_check(id, "not_run", "no trace to compare"))

    # A case's expected outcome is always assembled or refused, so checks are only not_run after the outcome failed.
    failed = [c for c in checks if c["status"] == "fail"]
    if not failed:
        return "passed", checks, None, differences
    return "failed", checks, failed[0].get("detail") or failed[0]["id"], differences


def judge_rejection(case: Case, outcome: Outcome) -> tuple[str, list[dict], str | None, list[dict]]:
    if outcome.kind == "rejected":
        return "rejected", [_check("outcome", "pass")], None, []
    if outcome.kind == "unsupported":
        skip = _skippable(case, outcome)
        if skip:
            return "skipped", [_check("outcome", "skipped", skip)], skip, []
    detail = f"expected rejection, got {_describe(outcome)}"
    return "failed", [_check("outcome", "fail", detail)], detail, []


def _first_byte_difference(expected: bytes, actual: bytes | None) -> str:
    if actual is None:
        return "no payload was returned"
    for i, (a, b) in enumerate(zip(expected, actual)):
        if a != b:
            return f"payload differs from byte {i} (expected {expected[i:i + 24]!r}, got {actual[i:i + 24]!r})"
    return f"payload length {len(actual)} differs from expected {len(expected)}"


def _row(ctx: SuiteContext, case: Case, adapter: Adapter, invocation, outcome: Outcome, trace_validator) -> dict:
    blobs = ctx.run.blobs
    judge = judge_case(case, outcome, trace_validator) if case.kind == "case" else judge_rejection(case, outcome)
    verdict, checks, detail, differences = judge
    trace = outcome.trace
    audited = None
    if outcome.kind in ("assembled", "refused") and isinstance(trace, dict) and case.snapshot is not None:
        result = audit(ctx.contract, case.snapshot_bytes, outcome.payload, trace)
        checks = checks + result.as_checks()
        audited = {"status": result.status, "failed": result.failed}
    result = trace.get("result") if isinstance(trace, dict) else None
    budget = trace.get("budget") if isinstance(trace, dict) else None
    input_tokens = result.get("input_tokens") if isinstance(result, dict) else None
    budget_input = budget.get("input") if isinstance(budget, dict) else None
    return {
        "$schema": output.schema_name("result-row"),
        "run_id": ctx.run.run_id,
        "suite": ID,
        "corpus": CORPUS[case.kind],
        "case_id": case.id,
        "case_kind": case.kind,
        "snapshot": blobs.put(case.snapshot_bytes, "application/json"),
        "adapter": adapter.name,
        "env_cell": "baseline",
        "repetition": 0,
        "expected_outcome": case.expected_outcome,
        "expected_trace": blobs.put_json(case.expected_trace) if case.expected_trace is not None else None,
        "expected_payload": blobs.put_text(case.expected_payload) if case.expected_payload is not None else None,
        "outcome": outcome.kind,
        "exit_code": invocation.exit_code,
        "refusal_reason": outcome.refusal_reason,
        "unsupported": [f"{k} {c}" for k, c in outcome.unsupported],
        "payload": blobs.put_text(outcome.payload) if outcome.payload is not None else None,
        "payload_hash": hashlib.sha256(outcome.payload).hexdigest() if outcome.payload is not None else None,
        "trace": blobs.put_json(trace) if trace is not None else None,
        "trace_normalized": blobs.put_json(traces.normalize(trace)) if trace is not None else None,
        "stderr": blobs.put_text(invocation.stderr) if invocation.stderr else None,
        "input_tokens": input_tokens if isinstance(input_tokens, int) and not isinstance(input_tokens, bool) else None,
        "charged_tokens": traces.charged_tokens(trace) if isinstance(trace, dict) else None,
        "budget_input": budget_input if isinstance(budget_input, int) and not isinstance(budget_input, bool) else None,
        "wall_ms": round(invocation.wall_ms, 3),
        "net_ms": None,
        "verdict": verdict,
        "detail": detail,
        "audit": audited,
        "checks": checks,
        "differences": differences,
        "requirements": case.rules,
        "coverage_tags": traces.coverage_tags(case.expected_trace, case.snapshot),
        "finding": None,
    }


def _finding(ctx: SuiteContext, row: dict, case: Case, oracle: str = "expected") -> dict:
    failing = [c["id"] for c in row["checks"] if c["status"] == "fail" and c["oracle"] in _ORACLES[oracle]]
    signature = {"suite": ID, "adapter": row["adapter"], "case_id": row["case_id"], "oracle": oracle, "checks": failing}
    finding_id = hashlib.sha256(output.dumps(signature, compact=True).encode()).hexdigest()[:12]
    if oracle == "auditor":
        detail = next(c.get("detail", "") for c in row["checks"] if c["oracle"] == "auditor" and c["status"] == "fail")
        summary = f"{row['adapter']} output for {case.kind} {case.id} breaks {', '.join(failing)}: {detail}"
    else:
        summary = f"{row['adapter']} fails {case.kind} {case.id}: {row['detail']}"
    return {
        "$schema": output.schema_name("finding"),
        "finding_id": finding_id,
        "run_id": ctx.run.run_id,
        "suite": ID,
        "adapter": row["adapter"],
        "case_id": row["case_id"],
        "oracle": oracle,
        "checks": failing,
        "severity": "error",
        "summary": summary[:1000],
        "first_pointer": row["differences"][0]["pointer"] if row["differences"] and oracle == "expected" else None,
        "requirements": case.rules,
        "occurrences": 1,
        "reproducer": {
            "snapshot": row["snapshot"],
            "spec_path": case.directory.relative_to(ctx.contract.path).as_posix(),
        },
    }


def _report(ctx: SuiteContext, adapter: Adapter, rows: list[dict], cases: dict[str, Case]) -> dict:
    """The adapter's run in the spec's conformance-report format (README, Reporting results)."""
    implementation = adapter.implementation or {
        "name": adapter.name, "version": "unknown", "language": adapter.config.language,
    }

    def entry(row: dict) -> dict:
        out = {"id": row["case_id"], "rules": cases[row["case_id"]].rules, "outcome": row["verdict"]}
        if row["detail"]:
            out["detail"] = row["detail"]
        return out

    mine = [r for r in rows if r["adapter"] == adapter.name]
    return {
        "implementation": implementation,
        "contract": {
            "repository": ctx.contract.checkout.repository or "unknown/unknown",
            "commit": ctx.contract.checkout.commit,
            "dirty": bool(ctx.contract.checkout.dirty),
        },
        "cases": [entry(r) for r in mine if r["case_kind"] == "case"],
        "rejections": [entry(r) for r in mine if r["case_kind"] == "rejection"],
    }


def _compare_committed(ctx: SuiteContext, adapter: Adapter, report: dict) -> dict | None:
    """How this run's outcomes compare with the report the implementation's repository commits."""
    committed = adapter.committed_report
    if not committed:
        return None
    their_commit = (committed.get("contract") or {}).get("commit")
    comparable = (
        gitinfo.unchanged_between(ctx.contract.path, their_commit, ctx.contract.checkout.commit,
                                  "conformance/cases", "conformance/rejections")
        if their_commit else None
    )
    differs, matches, total = [], 0, 0
    for section in ("cases", "rejections"):
        theirs = {e["id"]: e.get("outcome") for e in committed.get(section) or [] if isinstance(e, dict)}
        ours = {e["id"]: e["outcome"] for e in report[section]}
        for id in sorted(set(theirs) | set(ours)):
            total += 1
            if theirs.get(id) == ours.get(id):
                matches += 1
            else:
                differs.append({"id": id, "section": section, "committed": theirs.get(id), "observed": ours.get(id)})
    return {
        "contract_commit": their_commit,
        "cases_unchanged_since": comparable,
        "matches": matches,
        "total": total,
        "differs": differs,
    }


def run(ctx: SuiteContext) -> SuiteResult:
    started = now()
    corpus = ctx.contract.cases + ctx.contract.rejections
    cases = {c.id: c for c in corpus}
    trace_validator = ctx.contract.validator("trace.schema.json")
    jobs = [(case, adapter) for adapter in ctx.adapters.values() for case in corpus]
    ctx.log(f"S1: {len(ctx.contract.cases)} cases + {len(ctx.contract.rejections)} rejections "
            f"× {len(ctx.adapters)} adapter(s) = {len(jobs)} invocations")

    rows: list[dict] = []

    def work(case: Case, adapter: Adapter) -> dict:
        invocation = adapters_mod.invoke(adapter, case.snapshot_bytes, ctx.config.timeout_s, ctx.config.root)
        return _row(ctx, case, adapter, invocation, adapters_mod.classify(invocation), trace_validator)

    with ThreadPoolExecutor(max_workers=ctx.config.concurrency) as pool:
        futures = [pool.submit(work, case, adapter) for case, adapter in jobs]
        for done, future in enumerate(as_completed(futures), 1):
            rows.append(future.result())
            if done % 50 == 0 or done == len(futures):
                ctx.log(f"S1: {done}/{len(futures)}")

    order = {name: i for i, name in enumerate(ctx.adapters)}
    position = {c.id: i for i, c in enumerate(corpus)}
    rows.sort(key=lambda r: (order[r["adapter"]], position[r["case_id"]]))

    findings = []
    for row in rows:
        if row["verdict"] == "failed":
            finding = _finding(ctx, row, cases[row["case_id"]])
            row["finding"] = finding["finding_id"]
            findings.append(finding)
        if row["audit"] and row["audit"]["status"] == "fail":
            finding = _finding(ctx, row, cases[row["case_id"]], "auditor")
            row["finding"] = row["finding"] or finding["finding_id"]
            findings.append(finding)
    agreement = _differential(ctx, rows, cases, findings)

    base = f"suites/{ID}"
    report_validator = ctx.contract.validator("conformance_report.schema.json")
    per_adapter, suite_metrics = [], []
    for adapter in ctx.adapters.values():
        mine = [r for r in rows if r["adapter"] == adapter.name]
        report = _report(ctx, adapter, rows, cases)
        report_path = f"{base}/reports/{adapter.name}.conformance-report.json"
        ctx.run.write_external(report_path, report, report_validator, "conformance-report",
                               "spec:conformance_report.schema.json",
                               f"{adapter.name}: this run in the spec's conformance-report format")
        committed = _compare_committed(ctx, adapter, report)
        case_rows = [r for r in mine if r["case_kind"] == "case"]
        rejection_rows = [r for r in mine if r["case_kind"] == "rejection"]
        tally_cases = Counter(r["verdict"] for r in case_rows)
        tally_rejections = Counter(r["verdict"] for r in rejection_rows)
        audited = [r for r in mine if r["audit"]]
        audit_ok = sum(r["audit"]["status"] == "pass" for r in audited)
        ok = not tally_cases["failed"] and not tally_rejections["failed"] and audit_ok == len(audited)
        per_adapter.append({
            "adapter": adapter.name,
            "status": "pass" if ok else "fail",
            "error": None,
            "cases": {k: tally_cases[k] for k in ("passed", "failed", "skipped")} | {"total": len(case_rows)},
            "rejections": {k: tally_rejections[k] for k in ("rejected", "failed", "skipped")}
                          | {"total": len(rejection_rows)},
            "outcomes": {k: v for k, v in sorted(Counter(r["outcome"] for r in mine).items())},
            "report": report_path,
            "committed_report": committed,
            "wall_ms": metrics.percentiles([r["wall_ms"] for r in mine]),
        })
        suite_metrics.append(metrics.rate(
            "s1.cases.pass_rate", "Conformance cases passed", tally_cases["passed"],
            len(case_rows) - tally_cases["skipped"], suite=ID, adapter=adapter.name,
            description="Cases whose payload bytes and normalized trace match, out of cases not skipped."))
        suite_metrics.append(metrics.rate(
            "s1.rejections.rate", "Rejections rejected", tally_rejections["rejected"],
            len(rejection_rows) - tally_rejections["skipped"], suite=ID, adapter=adapter.name,
            description="Invalid snapshots rejected before assembly, out of rejections not skipped."))
        suite_metrics.append(metrics.rate(
            "s1.audit.pass_rate", "Outputs passing the trace audit", audit_ok, len(audited), suite=ID,
            adapter=adapter.name, description="Assembled and refused outputs that pass every auditor check A1–A16."))
        if committed is not None:
            suite_metrics.append(metrics.rate(
                "s1.committed_report.agreement", "Agreement with the committed report", committed["matches"],
                committed["total"], target=1.0 if committed["cases_unchanged_since"] else None,
                suite=ID, adapter=adapter.name,
                description="Outcomes that equal those in the implementation's own conformance-report.json. Informational "
                            "when the cases changed between that report's contract commit and this run's."))

    for name, error in ctx.unavailable.items():
        per_adapter.append({"adapter": name, "status": "unavailable", "error": error, "cases": None,
                            "rejections": None, "outcomes": {}, "report": None, "committed_report": None,
                            "wall_ms": metrics.percentiles([])})

    suite_metrics.append(metrics.rate(
        "s1.differential.agreement", "Cases on which every adapter agrees", agreement["agreeing"], agreement["cases"],
        suite=ID, description="Cases where every adapter returns the same outcome, payload bytes and normalized trace."))
    traced = [r for r in rows if r["trace"] is not None]
    schema_ok = [r for r in traced if any(c["id"] == "trace_schema" and c["status"] == "pass" for c in r["checks"])]
    suite_metrics.append(metrics.rate(
        "s1.fault_free_rate", "Invocations without a fault", sum(r["outcome"] not in FAULTS for r in rows), len(rows),
        suite=ID, description="Invocations that did not crash, time out or print invalid output."))
    suite_metrics.append(metrics.rate(
        "s1.trace_schema_rate", "Traces valid against trace.schema.json", len(schema_ok), len(traced), suite=ID,
        description="Traces that validate against the published schema with format assertion. Necessary, not sufficient "
                    "(R-21)."))

    ctx.run.write_jsonl(f"{base}/results.jsonl", rows, "result-row", "S1: one row per case × adapter")
    ctx.run.write_jsonl(f"{base}/findings.jsonl", findings, "finding", "S1: one row per failing case × adapter")

    statuses = [a["status"] for a in per_adapter]
    if not ctx.adapters:
        status = "error"
    elif "fail" in statuses or findings:
        status = "fail"
    elif "unavailable" in statuses:
        status = "partial"
    else:
        status = "pass"
    summary_path = f"{base}/summary.json"
    ctx.run.write_json(summary_path, {
        "$schema": output.schema_name("suite-summary"),
        "run_id": ctx.run.run_id,
        "suite": ID,
        "title": TITLE,
        "status": status,
        "started_at": started,
        "finished_at": now(),
        "requirements": sorted({r for c in corpus for r in c.rules}, key=lambda r: int(r[2:])),
        "corpora": [
            {"id": CORPUS["case"], "count": len(ctx.contract.cases)},
            {"id": CORPUS["rejection"], "count": len(ctx.contract.rejections)},
        ],
        "adapters": per_adapter,
        "metrics": suite_metrics,
        "files": {"results": f"{base}/results.jsonl", "findings": f"{base}/findings.jsonl"},
        "differential": agreement,
    }, "S1: status, per-adapter tallies, metrics")

    coverage = []
    for row in rows:
        case = cases[row["case_id"]]
        reasons = []
        if case.expected_trace:
            reasons = [(e["reason"], e.get("slot")) for e in case.expected_trace.get("excluded", [])
                       if isinstance(e, dict) and isinstance(e.get("reason"), str)]
            refused = case.expected_trace.get("refused") or {}
            if refused.get("bool") and refused.get("reason"):
                reasons.append((refused["reason"], None))
        coverage.append(Coverage(row["adapter"], case.rules, reasons, row["coverage_tags"],
                                 row["verdict"] in ("passed", "rejected")))

    return SuiteResult(ID, TITLE, status, summary_path, suite_metrics, findings, coverage)


def _differential(ctx: SuiteContext, rows: list[dict], cases: dict[str, Case], findings: list[dict]) -> dict:
    """Compare every adapter's answer to each case, add an agreement check to each row, and record each
    disagreement as one finding."""
    by_case: dict[str, dict[str, dict]] = defaultdict(dict)
    for row in rows:
        by_case[row["case_id"]][row["adapter"]] = row
    compared = agreeing = 0
    pair_total, pair_agree = Counter(), Counter()
    disagreements = []
    for case_id, per in by_case.items():
        if len(per) < 2:
            continue
        signatures = {a: {"outcome": r["outcome"], "payload": r["payload_hash"], "trace": r["trace_normalized"]}
                      for a, r in per.items()}
        result = differential.compare(signatures)
        compared += 1
        agreeing += result["agree"]
        for pair, stage in result["pairs"].items():
            pair_total[pair] += 1
            pair_agree[pair] += stage is None
        for adapter, row in per.items():
            differing = {o: differential.first_difference(signatures[adapter], signatures[o]) for o in per if o != adapter}
            differing = {o: stage for o, stage in differing.items() if stage}
            check = {"oracle": "differential", "id": "agreement", "status": "fail" if differing else "pass"}
            if differing:
                check["detail"] = "differs from " + ", ".join(f"{o} ({stage})" for o, stage in sorted(differing.items()))
            row["checks"].append(check)
        if not result["agree"]:
            stage = next(s for s in result["pairs"].values() if s)
            disagreements.append({"case_id": case_id, "groups": result["groups"], "first_stage": stage})
            signature = {"suite": ID, "case_id": case_id, "oracle": "differential"}
            finding_id = hashlib.sha256(output.dumps(signature, compact=True).encode()).hexdigest()[:12]
            for row in per.values():
                row["finding"] = row["finding"] or finding_id
            case = cases[case_id]
            findings.append({
                "$schema": output.schema_name("finding"),
                "finding_id": finding_id,
                "run_id": ctx.run.run_id,
                "suite": ID,
                "adapter": None,
                "adapters": sorted(per),
                "case_id": case_id,
                "oracle": "differential",
                "checks": ["agreement"],
                "severity": "error",
                "summary": f"adapters disagree on {case.kind} {case_id} from the {stage}: "
                           + " vs ".join("/".join(g) for g in result["groups"]),
                "first_pointer": None,
                "requirements": case.rules,
                "occurrences": 1,
                "reproducer": {"snapshot": next(iter(per.values()))["snapshot"],
                               "spec_path": case.directory.relative_to(ctx.contract.path).as_posix()},
            })
    return {
        "cases": compared,
        "agreeing": agreeing,
        "pairs": [{"a": a, "b": b, "agree": pair_agree[(a, b)], "total": pair_total[(a, b)]}
                  for (a, b) in sorted(pair_total)],
        "disagreements": disagreements,
    }
