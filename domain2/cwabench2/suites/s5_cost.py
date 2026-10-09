"""S5 · Cost and latency (domain-2-plan.md, 8), from S2's records alone: no assembly, no model call.

Per arm and tier:
- prompt and completion tokens, as the server reported them, per answered probe and per correct answer;
- the latency of the call that answered, as recorded when the cache was filled, so a replay reports the same numbers:
  p50 and p95;
- the token estimator's under-count: (the server's prompt tokens − the payload's own count) ÷ the server's prompt
  tokens. The server's count includes its chat template, which the payload's does not, and estimate-utf8/v1 counts
  digit-heavy text low, so short prompts are under-counted by a larger share. The summary reports it by prompt size,
  with the margin each size band would need, which is how `[budgets].margin_percent` is calibrated;
- **budget overruns**, the gate: a call whose server prompt tokens exceed the budget its payload was charged against.
  That is what the margin exists to prevent (R-16), so the suite fails on any;
- the answer length over the conversation: completion tokens by turn count, the study's answer bloat.

The prefix-cache counts a server reports are recorded per call in model/calls.jsonl and summed here; that question
belongs to Domain 4.
"""
from __future__ import annotations

from collections import defaultdict

from cwabench.metrics import percentiles
from cwabench.rundir import now

from .. import metrics, output
from . import SuiteContext, SuiteResult, finding

ID = "S5"
TITLE = "Cost and latency"


def run(ctx: SuiteContext) -> SuiteResult:
    started = now()
    grades = [g for g in ctx.shared.get("s2_grades", []) if g["request_sha256"] is not None]
    calls = {(c["request_sha256"], c["sample"]): c for c in ctx.shared.get("s2_calls", [])}
    margin = ctx.config.application.margin_percent
    groups = defaultdict(list)
    for row in grades:
        groups[(row["arm"], row["tier"])].append(row)
    by_arm, suite_metrics, errors_all = [], [], []
    for (arm, tier), rows in groups.items():
        prompt = [r["prompt_tokens"] for r in rows if isinstance(r["prompt_tokens"], int)]
        completion = [r["completion_tokens"] for r in rows if isinstance(r["completion_tokens"], int)]
        correct = sum(r["verdict"] == "correct" for r in rows)
        latency = [calls[(r["request_sha256"], r["sample"])]["provenance"].get("latency_ms") for r in rows
                   if (r["request_sha256"], r["sample"]) in calls]
        latency = [x for x in latency if isinstance(x, (int, float))]
        errors = [(r["input_tokens"] - r["prompt_tokens"]) / r["prompt_tokens"] for r in rows
                  if isinstance(r["prompt_tokens"], int) and r["prompt_tokens"] > 0 and r["input_tokens"] is not None]
        errors_all += errors
        cached = [calls[(r["request_sha256"], r["sample"])]["provenance"].get("cached_tokens") for r in rows
                  if (r["request_sha256"], r["sample"]) in calls]
        by_turns = defaultdict(list)
        for r in rows:
            if isinstance(r["completion_tokens"], int):
                by_turns[str(r["turn_count"])].append(r["completion_tokens"])
        entry = {
            "arm": arm, "tier": tier, "answered": len(rows), "correct": correct,
            "prompt_tokens": percentiles(prompt), "completion_tokens": percentiles(completion),
            "prompt_tokens_per_correct": round(sum(prompt) / correct, 3) if correct else None,
            "latency_ms": percentiles(latency),
            "estimator_error": percentiles([round(e, 6) for e in errors]),
            "cached_tokens": sum(c for c in cached if isinstance(c, int)),
            "answer_length_by_turns": {k: percentiles(v)
                                       for k, v in sorted(by_turns.items(), key=lambda kv: int(kv[0]))},
        }
        by_arm.append(entry)
        suite_metrics.append(metrics.value("s5.prompt_tokens_per_correct", "Prompt tokens per correct answer",
                                           entry["prompt_tokens_per_correct"], "tokens", suite=ID, arm=arm, tier=tier))
        suite_metrics.append(metrics.value("s5.latency_p50", "Latency, p50", entry["latency_ms"]["p50"], "ms",
                                           suite=ID, arm=arm, tier=tier))
    worst = max((-e for e in errors_all), default=None)
    worst = None if worst is None else max(0.0, worst)
    overruns = [r for r in grades if r["budget_input"] is not None and isinstance(r["prompt_tokens"], int)
                and r["prompt_tokens"] > r["budget_input"]]
    budgeted = sum(1 for r in grades if r["budget_input"] is not None and isinstance(r["prompt_tokens"], int))
    covers = not overruns
    bands = []
    for low, high in ((0, 500), (500, 2000), (2000, 8000), (8000, None)):
        under = [(r["prompt_tokens"] - r["input_tokens"]) / r["prompt_tokens"] for r in grades
                 if isinstance(r["prompt_tokens"], int) and r["input_tokens"] is not None
                 and r["prompt_tokens"] >= low and (high is None or r["prompt_tokens"] < high)]
        top = max(under, default=None)
        bands.append({"server_tokens": [low, high], "calls": len(under),
                      "undercount_max": None if top is None else round(max(0.0, top), 6),
                      # charged = count × (100 + m) / 100 ≥ server needs m ≥ 100 × u / (1 − u)
                      "margin_needed": None if top is None else -(-round(10000 * max(0.0, top) / (1 - top)) // 100)})
    suite_metrics.insert(0, metrics.count("s5.budget_overruns", "Calls whose server prompt exceeds their budget",
                                          len(overruns), maximum=0, suite=ID,
                                          description=f"of {budgeted} calls with a budget (R-16)"))
    suite_metrics.insert(1, metrics.value("s5.undercount_max", "Largest token under-count", worst, "rate",
                                          suite=ID, description="(server prompt tokens − own count) ÷ server prompt "
                                                                "tokens, over every answered call; by size in the "
                                                                "summary"))
    summary_path = f"suites/{ID}/summary.json"
    ctx.run.write_json(summary_path, {
        "$schema": output.schema_name("suite-summary"),
        "run_id": ctx.run.run_id, "suite": ID, "title": TITLE, "status": "pass" if covers is not False else "fail",
        "started_at": started, "finished_at": now(), "requirements": ["R-16"],
        "corpora": [{"id": family, "count": len(scripts)} for family, scripts in ctx.conversations.items()],
        "adapters": [], "metrics": suite_metrics,
        "files": {"results": "suites/S2/grades.jsonl", "findings": "findings.jsonl"},
        "cost": {"margin_percent": margin, "undercount_max": worst, "budget_overruns": len(overruns),
                 "undercount_by_size": bands, "by_arm": by_arm},
    }, "S5's tokens, latency and estimator error per arm and tier, from S2's records")
    status = "pass" if covers is not False else "fail"
    findings = []
    if overruns:
        first = overruns[0]
        findings.append(finding(ctx, ID, {"suite": ID, "check": "budget_overrun"}, case_id=first["case_id"],
                                oracle="producer", checks=["budget_overrun"], requirements=["R-16"],
                                summary=f"{len(overruns)} call(s) read more prompt tokens than their budget; first "
                                        f"{first['case_id']}: {first['prompt_tokens']} > {first['budget_input']} "
                                        f"(margin {margin}%)"))
    return SuiteResult(ID, TITLE, status, summary_path, suite_metrics, findings)
