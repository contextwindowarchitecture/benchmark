"""S1's part for the LQ family (domain-2-plan.md, 6.5 and 8): every LQ snapshot S4 will send, gated before a model
sees it, and every LQ baseline S4 will send, built and recorded.

For each corpus and question, the retriever's candidates are computed once (application/retrieval.py) and shared by
every retrieval arm. Each CWA arm of `[s4].arms` (application/evidence.py) freezes the question's snapshot, assembled
at `[s4].budget` by every adapter and judged as a chat snapshot is (s1_gate.py): the prediction, the audit,
agreement, and the fact-in-payload oracle by trace and by text, for a question that needs a fact. A corpus and arm
passes the gate when every one of its questions does. Each baseline of `[s4].arms` (baselines/longcontext.py) is built
and recorded with the fact-in-payload oracle by its record and its text, which must agree.

Rows go to `suites/S1/lq.jsonl` (`lq-gate-row`) and `suites/S1/lq-baselines.jsonl` (`lq-baseline-row`); the gate's
verdicts join the chat ones in `ctx.shared["gate"]`, keyed `<corpus>/<arm>`.
"""
from __future__ import annotations

import hashlib
from collections import defaultdict

from .. import metrics, output
from ..application import evidence
from ..application.retrieval import Index, retrieve
from ..baselines import longcontext as lq_baselines
from . import SuiteContext


def candidates(ctx: SuiteContext) -> dict[tuple[str, str], list[tuple[str, float]]]:
    """(corpus, question) → the retriever's candidates, computed once a run."""
    found = ctx.shared.get("lq_candidates")
    if found is None:
        found = {}
        for corpus in ctx.corpora:
            index = Index(corpus["chunks"])
            for q in corpus["questions"]:
                found[(corpus["corpus_id"], q["question_id"])] = retrieve(index, q["query"],
                                                                         ctx.config.s4["candidates"])
        ctx.shared["lq_candidates"] = found
    return found


def baseline(ctx: SuiteContext, arm: str, corpus: dict, question: dict) -> lq_baselines.Built:
    config = ctx.config
    limit = config.model.get("context_limit")
    if limit is not None:
        limit -= config.application.reserved_output
    return lq_baselines.build(arm, corpus, question, candidates(ctx)[(corpus["corpus_id"], question["question_id"])],
                              None if arm == "control-full" else config.s4["budget"],
                              config.application.margin_percent, config.application.tokenizer, limit)


def frozen(ctx: SuiteContext, arm: str, corpus: dict, question: dict) -> evidence.Frozen:
    return evidence.freeze(ctx.contract, corpus, question,
                           candidates(ctx)[(corpus["corpus_id"], question["question_id"])], evidence.ARMS[arm],
                           ctx.config.application)


def run(ctx: SuiteContext, pool, report, tally, gate, assemble, judge, expected_json, candidate_counts) -> dict:
    """Gate and build every LQ payload; returns the summary's LQ counts. `pool`, `report`, `tally`, `gate` and the
    functions are S1's own (s1_gate.py)."""
    config = ctx.config
    budget = config.s4["budget"]
    names = list(ctx.adapters)
    arms = [a for a in config.s4["arms"] if a in evidence.ARMS]
    bases = [a for a in config.s4["arms"] if a in lq_baselines.ARMS]
    rows, rows_b = [], []
    present = defaultdict(lambda: [0, 0])  # (arm, tier) → [questions with every needed chunk kept, questions]
    problems = evidence.check(ctx.contract)
    for problem in problems:
        report.add("self-check", ["profile"], None, "lq", "lq-profile", f"the LQ arms' profile or route: {problem}",
                   None)
    for corpus in ctx.corpora:
        cid, tier = corpus["corpus_id"], corpus["tier"]
        for question in corpus["questions"]:
            needs = question["needs"]
            for arm in bases:
                built = baseline(ctx, arm, corpus, question)
                fact = None
                if needs:
                    by_record, by_text = lq_baselines.present(built, question)
                    for agreed in (a == b for a, b in zip(by_record, by_text)):
                        tally["baseline_fact_oracle"][0] += agreed
                        tally["baseline_fact_oracle"][1] += 1
                    fact = {"needs": needs, "by_record": by_record, "by_text": by_text, "present": all(by_record)}
                    present[(arm, tier)][0] += fact["present"] and built.outcome == "fits"
                    present[(arm, tier)][1] += 1
                rows_b.append({
                    "$schema": output.schema_name("lq-baseline-row"), "run_id": ctx.run.run_id, "suite": "S1",
                    "case_id": f"{cid}/{arm}/{question['question_id']}", "corpus": cid, "family": "lq", "arm": arm,
                    "question_id": question["question_id"], "tier": tier,
                    "budget_input": None if arm == "control-full" else budget, "outcome": built.outcome,
                    "payload_hash": built.payload_hash, "input_tokens": built.input_tokens, "charged": built.charged,
                    "offered": built.offered, "kept": len(built.kept), "fact": fact,
                })
        for arm in arms:
            jobs, metas = [], []
            for question in corpus["questions"]:
                snapshot = frozen(ctx, arm, corpus, question)
                expected = snapshot.expect(budget)
                data = snapshot.at(budget)
                fact = None
                if question["needs"]:
                    fact = {"needs": question["needs"], "carriers": snapshot.carriers, "evidence": snapshot.evidence}
                jobs.append({
                    "data": data, "adapters": names, "timeout": config.timeout_s, "cwd": str(config.root),
                    "payload_source": config.adapters.payload_source, "fact": fact, "keep": False,
                    "candidates": candidate_counts(snapshot),
                    "expected": {"outcome": expected.outcome, "refusal_reason": expected.refusal_reason,
                                 "payload_hash": expected.payload_hash, "input_tokens": expected.input_tokens,
                                 "included": sorted(expected.included)},
                })
                metas.append((question, snapshot, expected, data))
            for (question, snapshot, expected, data), result in zip(metas, pool.map(assemble, jobs)):
                case_id = f"{cid}/{arm}/{question['question_id']}"
                found = judge(report, arm, case_id, expected, data, result, tally)
                fact = result["fact"]
                if fact is not None:
                    present[(arm, tier)][0] += fact["present"]
                    present[(arm, tier)][1] += 1
                gate[(cid, arm)] = gate.get((cid, arm), True) and not found
                rows.append({
                    "$schema": output.schema_name("lq-gate-row"), "run_id": ctx.run.run_id, "suite": "S1",
                    "case_id": case_id, "corpus": cid, "family": "lq", "arm": arm,
                    "question_id": question["question_id"], "tier": tier, "budget_input": budget,
                    "candidates": len(candidates(ctx)[(cid, question["question_id"])]),
                    "excluded": dict(sorted(snapshot.excluded.items())),
                    "snapshot": ctx.run.blobs.put(snapshot.at(snapshot.full), "application/json"),
                    "snapshot_sha256": hashlib.sha256(data).hexdigest(), "expected": expected_json(expected),
                    "answers": [{k: v for k, v in a.items() if not k.startswith("_")} for a in result["answers"]],
                    "agree": result["agree"], "groups": result["groups"], "shedding": result["shedding"],
                    "fact": None if fact is None else {"needs": fact["needs"], "by_trace": fact["by_trace"],
                                                       "by_text": fact["by_text"], "present": fact["present"]},
                    "verdict": "failed" if found else "passed", "findings": sorted(set(found)),
                })
        ctx.log(f"S1: {cid} done ({len(rows)} LQ rows)")
    ctx.run.write_jsonl("suites/S1/lq.jsonl", rows, "lq-gate-row",
                        "One row per LQ question and CWA arm at the S4 budget: every adapter's answer, the prediction, "
                        "the shedding record and the fact-in-payload oracle")
    ctx.run.write_jsonl("suites/S1/lq-baselines.jsonl", rows_b, "lq-baseline-row",
                        "One row per LQ question and conventional arm: the payload, its count, what it kept and the "
                        "fact-in-payload oracle")
    ctx.shared["s1_lq_rows"], ctx.shared["s1_lq_baselines"] = rows, rows_b
    order = list(config.s4["arms"])
    tiers = [c["tier"] for c in ctx.corpora]
    found_metrics = [metrics.rate("s1.fact_in_payload", "LQ questions with every needed chunk in the payload", yes,
                                  total, target=None, suite="S1", arm=arm, family="lq", tier=tier,
                                  description="Measured, not gated: what each LQ arm keeps at each corpus tier")
                     for (arm, tier), (yes, total) in sorted(present.items(),
                                                             key=lambda kv: (order.index(kv[0][0]),
                                                                             tiers.index(kv[0][1])))]
    return {"rows": len(rows), "baselines": len(rows_b), "metrics": found_metrics}
