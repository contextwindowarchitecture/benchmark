"""S1 · Assembly gate (domain-2-plan.md, 8): every CWA snapshot S2 to S4 will send, assembled by every assembler and
judged before a model sees it.

Every probe of every script, in every CWA arm, at every budget (the absolute ones and each ratio of the snapshot's
full size), and with `frames` every turn too, is assembled by each adapter. Each answer is judged three ways:

- **prediction**: the outcome, refusal reason, included items, `input_tokens` and payload bytes the rules give
  (application/snapshots.py, `Frozen.expect`);
- **audit**: Domain 1's trace auditor (A1–A16), on every answer;
- **agreement**: every adapter's outcome, payload bytes and normalized trace (Domain 1's differential oracle).

On every probe, the fact-in-payload oracle decides by the trace and by the payload's text whether the answer's facts
were included, and the two must agree (application/fact.py).

S1 also builds every conventional baseline (baselines/) at the same points, at the absolute budgets and each ratio of
the conversation's full size in native chat, and records each payload, its count, what it kept and dropped and, on
probes, the fact-in-payload oracle by its record and by its text (`baselines.jsonl`). A baseline's fact counts as
present only when its payload fits: an overflowing request reaches no model. A conversation and arm passes the gate when every one of
its rows passes; S2 to S4 use only those that do, and the rest are counted.

Each row also records what the payload source's trace shows assembly did: per slot, its candidates, how many were
included and their tokens, how many were omitted for budget, and every exclusion by reason. A conversation's turn rows
at one arm and budget play as frames, as a budget sweep does (domain-2-plan.md, 11). Timelines are written for each
conversation's last probe. Rows keep hashes: a probe's snapshot is stored once per arm, with its budget in the row,
and any other snapshot only as a finding's reproducer.

Each worker process runs one snapshot on every adapter at once and audits every answer, so the auditor, which is
pure Python, runs in parallel too.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass

from cwabench import adapters as adapters_mod
from cwabench import timelines
from cwabench.contract import Contract
from cwabench.oracles import differential
from cwabench.oracles.auditor import audit
from cwabench.rundir import now

from .. import baselines, metrics, output
from ..application import fact as fact_mod
from ..application import profile as profile_mod
from ..application.snapshots import ARMS, Frozen, Point, freeze, points
from . import SuiteContext, SuiteResult, finding

ID = "S1"
TITLE = "Assembly gate"
REQUIREMENTS = ["R-1", "R-3", "R-4", "R-7", "R-8", "R-9", "R-14", "R-16", "R-17", "R-21", "R-22", "R-23", "R-24",
                "R-25"]
FAULTS = ("crashed", "timeout", "invalid_output", "rejected", "unsupported")

_WORKER: dict = {}


def _init(adapters: dict, contract_args: tuple) -> None:
    _WORKER["adapters"] = adapters
    _WORKER["contract"] = Contract(*contract_args)


def _shedding(trace: dict | None, candidates: dict[str, int]) -> dict | None:
    if not isinstance(trace, dict):
        return None
    slots = {slot: {"candidates": n, "included": 0, "tokens": 0, "omitted": 0} for slot, n in candidates.items()}
    for row in trace.get("included") or []:
        slot = slots.setdefault(row.get("slot"), {"candidates": 0, "included": 0, "tokens": 0, "omitted": 0})
        slot["included"] += 1
        slot["tokens"] += row.get("tokens") or 0
    excluded = Counter()
    for row in trace.get("excluded") or []:
        excluded[row.get("reason")] += 1
        if row.get("reason") == "over_budget" and row.get("slot") in slots:
            slots[row["slot"]]["omitted"] += 1
    result = trace.get("result") if isinstance(trace.get("result"), dict) else {}
    return {"slots": dict(sorted(slots.items())), "excluded": dict(sorted(excluded.items())),
            "compressed": len(trace.get("compressed") or []), "input_tokens": result.get("input_tokens")}


def assemble(job: dict) -> dict:
    """One snapshot on every adapter: each answer judged against the prediction and by the auditor, then compared."""
    adapters, contract = _WORKER["adapters"], _WORKER["contract"]
    data, expected = job["data"], job["expected"]
    names = [n for n in job["adapters"] if n in adapters]
    with ThreadPoolExecutor(max_workers=max(1, len(names))) as pool:
        invocations = list(pool.map(lambda n: adapters_mod.invoke(adapters[n], data, job["timeout"], job["cwd"]),
                                    names))
    answers, outcomes, signatures = [], {}, {}
    for name, invocation in zip(names, invocations):
        outcome = adapters_mod.classify(invocation)
        outcomes[name] = outcome
        trace = outcome.trace if isinstance(outcome.trace, dict) else None
        failed, detail = [], {}
        if outcome.kind in ("assembled", "refused") and trace is not None:
            result = audit(contract, data, outcome.payload, trace)
            failed = result.failed
            detail = {k: result.checks[k].violations[:2] for k in failed}
        signature = differential.signature(outcome.kind, outcome.payload, trace)
        signatures[name] = signature
        tokens = (trace or {}).get("result", {}) or {}
        tokens = tokens.get("input_tokens") if isinstance(tokens, dict) else None
        included = sorted({r.get("item_id") for r in (trace or {}).get("included") or []})
        matches = (outcome.kind == expected["outcome"] and outcome.refusal_reason == expected["refusal_reason"]
                   and signature["payload"] == expected["payload_hash"] and tokens == expected["input_tokens"]
                   and included == expected["included"])
        answers.append({"adapter": name, "outcome": outcome.kind, "exit_code": invocation.exit_code,
                        "refusal_reason": outcome.refusal_reason, "payload_hash": signature["payload"],
                        "trace_hash": signature["trace"], "input_tokens": tokens if isinstance(tokens, int) else None,
                        "audit_failed": failed, "matches_prediction": matches, "wall_ms": round(invocation.wall_ms, 3),
                        "problem": (outcome.problem or "")[:300] or None, "_detail": detail,
                        "_included": included})
    compared = differential.compare(signatures)
    source = job["payload_source"] if job["payload_source"] in outcomes else (names[0] if names else None)
    reference = outcomes.get(source)
    trace = reference.trace if reference is not None and isinstance(reference.trace, dict) else None
    fact = None
    if job["fact"] is not None:
        needs = job["fact"]["needs"]
        included = {r.get("item_id") for r in (trace or {}).get("included") or []}
        trace_says = fact_mod.by_trace(included, job["fact"]["carriers"], needs)
        text_says = fact_mod.by_text(reference.payload if reference else None, job["fact"]["evidence"], needs)
        fact = {"needs": needs, "by_trace": trace_says, "by_text": text_says, "present": all(trace_says),
                "agree": trace_says == text_says}
    return {
        "answers": answers,
        "agree": compared["agree"],
        "groups": compared["groups"],
        "shedding": _shedding(trace, job["candidates"]),
        "fact": fact,
        "source": source,
        "trace": trace if job["keep"] else None,  # the payload source's trace, for a timeline
    }


@dataclass
class Report:
    """Findings, one per signature (suite, oracle, check, adapter, arm), with their occurrences."""

    ctx: SuiteContext

    def __post_init__(self):
        self.found: dict[str, dict] = {}

    def add(self, oracle: str, checks: list[str], adapter: str | None, arm: str, case_id: str, summary: str,
            data: bytes | None) -> str:
        signature = {"suite": ID, "oracle": oracle, "checks": checks, "adapter": adapter, "arm": arm}
        row = finding(self.ctx, ID, signature, case_id=case_id, oracle=oracle, checks=checks, summary=summary[:500],
                      requirements=REQUIREMENTS if oracle == "auditor" else [])
        known = self.found.get(row["finding_id"])
        if known is not None:
            known["occurrences"] += 1
            return known["finding_id"]
        row["adapter"] = adapter
        if data is not None:
            row["reproducer"] = {"snapshot": self.ctx.run.blobs.put(data, "application/json"), "spec_path": None}
        self.found[row["finding_id"]] = row
        return row["finding_id"]


def _candidates(frozen: Frozen) -> dict[str, int]:
    counts = Counter()
    for batch in frozen.snapshot["batches"]:
        for item in batch["items"]:
            counts[item["slot"]] += 1
    return dict(counts)


def run(ctx: SuiteContext) -> SuiteResult:
    started = now()
    config = ctx.config
    problems = profile_mod.check(ctx.contract)
    names = list(ctx.adapters)
    report = Report(ctx)
    for problem in problems:
        report.add("self-check", ["profile"], None, "all", "profile", f"the arms' profile or route: {problem}", None)
    source = config.adapters.payload_source
    rows: list[dict] = []
    rows_b: list[dict] = []
    gate: dict[tuple[str, str], bool] = {}
    tally = defaultdict(lambda: [0, 0])  # metric → [passed, total]
    present = defaultdict(lambda: [0, 0])  # (arm, tier) → [probes with every needed fact, probes]
    timelines_written = 0
    contract_args = (ctx.contract.path, config.contract_commit, config.allow_dirty)
    ctx.log(f"S1: {sum(len(s) for s in ctx.conversations.values())} scripts × {len(config.arms)} arms × "
            f"{len(names)} adapter(s), frames {'on' if config.frames else 'off'}")
    with ProcessPoolExecutor(max_workers=config.concurrency, initializer=_init,
                             initargs=(ctx.adapters, contract_args)) as pool:
        for family, scripts in ctx.conversations.items():
            for script in scripts:
                last = script["probes"][-1]["probe_id"]
                for point in points(script, config.frames):
                    if config.baselines:
                        rows_b.extend(_baseline_rows(ctx, script, family, point, tally, present))
                for arm_name in config.arms:
                    arm = ARMS[arm_name]
                    jobs, metas = [], []
                    for point in points(script, config.frames):
                        for tier, budget, frozen in _snapshots(ctx, script, arm, point):
                            frozen_ref = None
                            if point.kind == "probe":
                                frozen_ref = ctx.run.blobs.put(frozen.at(frozen.full), "application/json")
                            expected = frozen.expect(budget)
                            data = frozen.at(budget)
                            keep = config.timelines and point.kind == "probe" and point.id == last
                            fact = None
                            if point.kind == "probe":
                                fact = {"needs": point.probe["needs"], "carriers": frozen.carriers,
                                        "evidence": frozen.evidence}
                            jobs.append({
                                "data": data, "adapters": names, "timeout": config.timeout_s, "cwd": str(config.root),
                                "payload_source": source, "fact": fact, "keep": keep,
                                "candidates": _candidates(frozen),
                                "expected": {"outcome": expected.outcome, "refusal_reason": expected.refusal_reason,
                                             "payload_hash": expected.payload_hash,
                                             "input_tokens": expected.input_tokens,
                                             "included": sorted(expected.included)},
                            })
                            metas.append((point, tier, budget, frozen_ref, expected, data))
                    for (point, tier, budget, frozen_ref, expected, data), result in zip(metas, pool.map(assemble, jobs)):
                        row, ok = _row(ctx, report, script, family, arm_name, point, tier, budget, frozen_ref,
                                       expected, data, result, tally, present)
                        rows.append(row)
                        gate[(script["conversation_id"], arm_name)] = gate.get((script["conversation_id"], arm_name),
                                                                               True) and ok
                        if result["trace"] is not None and result["source"] is not None:
                            document = timelines.build(ctx.contract, _parsed(data), result["trace"], result["source"],
                                                       frozen_ref, ctx.run.run_id, domain=output.D2)
                            digest = hashlib.sha256(data).hexdigest()
                            ctx.run.write_json(f"timelines/{digest}/{result['source']}.json", document,
                                               f"Assembly timeline: {script['conversation_id']} {arm_name} "
                                               f"{point.id} at {tier}")
                            timelines_written += 1
                ctx.log(f"S1: {script['conversation_id']} done ({len(rows)} rows)")

    ctx.run.write_jsonl(f"suites/{ID}/turns.jsonl", rows, "turn-row",
                        "One row per point, arm and budget: every adapter's answer, the prediction, the shedding "
                        "record and, on probes, the fact-in-payload oracle")
    if config.baselines:
        ctx.run.write_jsonl(f"suites/{ID}/baselines.jsonl", rows_b, "baseline-row",
                            "One row per point, baseline and budget: the payload, its count, what it kept and "
                            "dropped and, on probes, the fact-in-payload oracle")
    ctx.shared["s1_baselines"] = rows_b
    passed = {key for key, ok in gate.items() if ok}
    ctx.shared["s1_rows"] = rows
    ctx.shared["gate"] = {f"{c}/{a}": ok for (c, a), ok in sorted(gate.items())}
    labels = {"agreement": "Snapshots every adapter answered alike", "audit": "Answers that pass the trace audit",
              "prediction": "Answers equal to the prediction", "fact_oracle": "Needed facts judged alike by trace "
              "and text"}
    suite_metrics = [metrics.rate(f"s1.{name}", label, *tally[name], suite=ID) for name, label in labels.items()]
    suite_metrics.append(metrics.count("s1.faults", "Crashes, timeouts and invalid answers", tally["faults"][1],
                                       maximum=0, suite=ID))
    suite_metrics.append(metrics.rate("s1.gate", "Conversations and arms that pass the gate", len(passed), len(gate),
                                      suite=ID))
    suite_metrics.append(metrics.rate("s1.baseline_fact_oracle", "Baseline facts judged alike by record and text",
                                      *tally["baseline_fact_oracle"], suite=ID))
    tiers = [t for t, _ in config.budgets.of(1)]  # the configured order: absolute budgets, then ratios
    order = [*config.baselines, *config.arms]
    for (arm_name, tier), (yes, total) in sorted(present.items(),
                                                 key=lambda kv: (order.index(kv[0][0]), tiers.index(kv[0][1]))):
        suite_metrics.append(metrics.rate("s1.fact_in_payload", "Probes with every needed fact in the payload", yes,
                                          total, target=None, suite=ID, arm=arm_name, tier=tier,
                                          description="Measured, not gated: what each arm keeps at each budget"))
    findings = list(report.found.values())
    unavailable = {n: e for n, e in ctx.unavailable.items()}
    gates = [m for m in suite_metrics if m["status"] in ("pass", "fail")]
    status = "fail" if findings or any(m["status"] == "fail" for m in gates) else (
        "partial" if unavailable or not names else "pass")
    summary_path = f"suites/{ID}/summary.json"
    ctx.run.write_json(summary_path, {
        "$schema": output.schema_name("suite-summary"),
        "run_id": ctx.run.run_id,
        "suite": ID,
        "title": TITLE,
        "status": status,
        "started_at": started,
        "finished_at": now(),
        "requirements": REQUIREMENTS,
        "corpora": [{"id": family, "count": len(scripts)} for family, scripts in ctx.conversations.items()],
        "adapters": [{"adapter": n, "status": "unavailable" if n in unavailable else (
            "fail" if any(f["adapter"] == n for f in findings) else "pass"), "error": unavailable.get(n)}
            for n in config.adapters.use],
        "metrics": suite_metrics,
        "files": {"results": f"suites/{ID}/turns.jsonl", "findings": "findings.jsonl"},
        "gate": {"passed": len(passed), "total": len(gate),
                 "failed": sorted(f"{c}/{a}" for (c, a), ok in gate.items() if not ok)},
        "rows": {"probes": sum(r["point"] == "probe" for r in rows), "turns": sum(r["point"] == "turn" for r in rows),
                 "timelines": timelines_written, "baselines": len(rows_b)},
    }, "S1's gate: agreement, audit, prediction and the fact-in-payload oracle")
    return SuiteResult(ID, TITLE, status, summary_path, suite_metrics, findings)


def _snapshots(ctx: SuiteContext, script: dict, arm, point: Point):
    """(tier, budget, frozen) for each budget an arm is assembled at, at one point. A ladder arm freezes once and is
    assembled at every budget; the format control freezes the window baseline's selection at each budget and is
    assembled at that snapshot's own full size, so its selection is the baseline's exactly."""
    config = ctx.config
    if arm.selection is None:
        frozen = freeze(ctx.contract, script, arm, point, config.application)
        for tier, budget in config.budgets.of(frozen.full):
            yield tier, budget, frozen
        return
    for tier, budget in config.budgets.of(baselines.full(script, point, config.baseline)):
        built = baselines.at(arm.selection, script, point, budget, config.baseline)
        frozen = freeze(ctx.contract, script, arm, point, config.application, only=set(built.kept))
        yield tier, frozen.full, frozen


def _baseline_rows(ctx: SuiteContext, script: dict, family: str, point: Point, tally, present) -> list[dict]:
    config = ctx.config
    rows = []
    for tier, budget in config.budgets.of(baselines.full(script, point, config.baseline)):
        for arm in config.baselines:
            built = baselines.at(arm, script, point, budget, config.baseline)
            fact = None
            if point.kind == "probe":
                needs = point.probe["needs"]
                by_record = fact_mod.by_trace(set(built.kept), built.carriers, needs)
                by_text = fact_mod.by_text_chat(built.payload, built.evidence, needs)
                for agreed in (a == b for a, b in zip(by_record, by_text)):
                    tally["baseline_fact_oracle"][0] += agreed
                    tally["baseline_fact_oracle"][1] += 1
                fact = {"needs": needs, "by_record": by_record, "by_text": by_text, "present": all(by_record)}
                present[(arm, tier)][0] += fact["present"] and built.outcome == "fits"
                present[(arm, tier)][1] += 1
            rows.append({
                "$schema": output.schema_name("baseline-row"),
                "run_id": ctx.run.run_id,
                "suite": ID,
                "case_id": f"{script['conversation_id']}/{arm}/{point.id}@{tier}",
                "conversation": script["conversation_id"],
                "family": family,
                "arm": arm,
                "point": point.kind,
                "turn": point.turn,
                "probe_id": point.probe["probe_id"] if point.probe else None,
                "tier": tier,
                "budget_input": budget,
                "outcome": built.outcome,
                "payload_hash": built.payload_hash,
                "input_tokens": built.input_tokens,
                "charged": built.charged,
                "history": {"total": built.history_total, "kept": built.history_kept, "first_kept": built.first_kept},
                "system_survived": built.system_survived,
                "summary": built.summary,
                "fact": fact,
            })
    return rows


def _parsed(data: bytes) -> dict:
    return json.loads(data.decode("utf-8"))


def _row(ctx, report, script, family, arm, point: Point, tier, budget, frozen_ref, expected, data, result, tally,
         present) -> tuple[dict, bool]:
    case_id = f"{script['conversation_id']}/{arm}/{point.id}@{tier}"
    found = []
    for answer in result["answers"]:
        adapter = answer["adapter"]
        if answer["outcome"] in FAULTS:
            tally["faults"][1] += 1
            found.append(report.add("fault", [answer["outcome"]], adapter, arm, case_id,
                                    f"{adapter} {answer['outcome']} on {case_id}: {answer['problem'] or ''}", data))
            continue
        tally["audit"][0] += not answer["audit_failed"]
        tally["audit"][1] += 1
        tally["prediction"][0] += answer["matches_prediction"]
        tally["prediction"][1] += 1
        if answer["audit_failed"]:
            detail = "; ".join(f"{k}: {v[0]}" for k, v in answer["_detail"].items() if v)[:300]
            found.append(report.add("auditor", sorted(answer["audit_failed"]), adapter, arm, case_id,
                                    f"{adapter} output for {case_id} breaks {', '.join(answer['audit_failed'])}: "
                                    f"{detail}", data))
        if not answer["matches_prediction"]:
            found.append(report.add("expected", ["prediction"], adapter, arm, case_id,
                                    f"{adapter} on {case_id}: {answer['outcome']} with {len(answer['_included'])} "
                                    f"items, {answer['input_tokens']} tokens; expected {expected.outcome} with "
                                    f"{len(expected.included)} items, {expected.input_tokens} tokens", data))
    tally["agreement"][0] += result["agree"]
    tally["agreement"][1] += 1
    if not result["agree"]:
        found.append(report.add("differential", ["agreement"], None, arm, case_id,
                                f"adapters disagree on {case_id}: " + " vs ".join("/".join(g) for g in result["groups"]),
                                data))
    fact = result["fact"]
    if fact is not None:
        for agreed in (a == b for a, b in zip(fact["by_trace"], fact["by_text"])):
            tally["fact_oracle"][0] += agreed
            tally["fact_oracle"][1] += 1
        if not fact["agree"]:
            found.append(report.add("self-check", ["fact_in_payload"], None, arm, case_id,
                                    f"on {case_id} the trace says needed turns {fact['by_trace']} are included, the "
                                    f"payload's text says {fact['by_text']}", data))
        present[(arm, tier)][0] += fact["present"]
        present[(arm, tier)][1] += 1
    answers = [{k: v for k, v in a.items() if not k.startswith("_")} for a in result["answers"]]
    row = {
        "$schema": output.schema_name("turn-row"),
        "run_id": ctx.run.run_id,
        "suite": ID,
        "case_id": case_id,
        "conversation": script["conversation_id"],
        "family": family,
        "arm": arm,
        "point": point.kind,
        "turn": point.turn,
        "probe_id": point.probe["probe_id"] if point.probe else None,
        "tier": tier,
        "budget_input": budget,
        "snapshot": frozen_ref,
        "snapshot_sha256": hashlib.sha256(data).hexdigest(),
        "expected": {"outcome": expected.outcome, "refusal_reason": expected.refusal_reason,
                     "payload_hash": expected.payload_hash, "input_tokens": expected.input_tokens,
                     "included": len(expected.included)},
        "answers": answers,
        "agree": result["agree"],
        "groups": result["groups"],
        "shedding": result["shedding"],
        "fact": None if fact is None else {"needs": fact["needs"], "by_trace": fact["by_trace"],
                                           "by_text": fact["by_text"], "present": fact["present"]},
        "verdict": "failed" if found else "passed",
        "findings": sorted(set(found)),
    }
    return row, not found
