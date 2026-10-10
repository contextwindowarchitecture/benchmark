"""S4 · Long context (domain-2-plan.md, 8): every LQ question, in every arm of `[s4].arms`, sent to the model.

Arms (application/evidence.py, baselines/longcontext.py):

- the CWA arms that passed S1's gate for the corpus: each sends the payload the prediction gives at `[s4].budget`,
  whose hash must equal the payload source's answer in S1. An assembly that refuses reaches no model, grades
  `refused`, and a corpus and arm that failed the gate is left out and counted;
- the baselines, as S1 built them, each rebuilt and checked against S1's hash: `control-full` has no budget, and
  overflows (grades `overflow`, reaching no model) when the corpus exceeds the model's context.

Each payload is asked `[s4].repeats` times at the model's temperature, as S2's are (suites/calls.py), one stream per
corpus so a server's prefix cache answers the shared documents. Every reply is graded (grading/): a value, a site's
name, NOT FOUND, or an option's letter. A grade row holds nothing that depends on the run's mode, so a `replay` run
writes the same grades as the `llm` run that filled the cache.

Measured, never gated: per arm and corpus tier, accuracy with a cluster-bootstrap interval over corpora, its split by
question kind (single, multi, none) and format, accuracy with the needed chunks in the payload and without, the rate
of distractor answers (the twin's value, the other site), the rate of abstaining on a question the documents answer,
and each arm's difference from `[s4].reference` on the same questions and samples. What gates the suite is the
harness: every payload equal to S1's, and every call answered.
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict

from cwabench.rundir import now

from .. import metrics, output, stats
from ..application import evidence
from ..baselines import longcontext as lq_baselines
from ..conversations.longcontext import NOT_FOUND
from ..grading import grade
from ..grading.normalize import strip_reasoning, text_key
from ..model import Model
from . import SuiteContext, SuiteResult, finding
from . import calls as calls_mod
from .s1_longcontext import baseline, frozen

ID = "S4"
TITLE = "Long context"
VERDICTS = ("correct", "distractor", "wrong", "unparsed", "overflow", "refused")


def _payloads(ctx: SuiteContext, corpus: dict, question: dict, s1: dict, b1: dict):
    """(arm, budget, payload or None, input_tokens, fact_present, verdict if no call, problem) for one question."""
    config = ctx.config
    cid, qid = corpus["corpus_id"], question["question_id"]
    for arm in config.s4["arms"]:
        if arm in lq_baselines.ARMS:
            row = b1.get((cid, arm, qid))
            built = baseline(ctx, arm, corpus, question)
            if row is None or built.payload_hash != row["payload_hash"]:
                yield arm, None, None, None, None, None, "the baseline differs from S1's"
                continue
            present = row["fact"]["present"] if row["fact"] else None
            if built.outcome != "fits":
                yield arm, row["budget_input"], None, built.input_tokens, present, "overflow", None
                continue
            yield arm, row["budget_input"], built.payload, built.input_tokens, present, None, None
            continue
        if not ctx.shared.get("gate", {}).get(f"{cid}/{arm}", False):
            yield arm, None, None, None, None, None, "gate"
            continue
        row = s1.get((cid, arm, qid))
        budget = config.s4["budget"]
        expected = frozen(ctx, arm, corpus, question).expect(budget)
        source = None if row is None else next(
            (a for a in row["answers"] if a["adapter"] == config.adapters.payload_source), None)
        if source is None or expected.payload_hash != source["payload_hash"]:
            yield arm, budget, None, None, None, None, "the payload differs from the gated bytes"
            continue
        present = row["fact"]["present"] if row["fact"] else None
        if expected.outcome != "assembled":
            yield arm, budget, None, None, present, "refused", None
            continue
        yield arm, budget, expected.payload, expected.input_tokens, present, None, None


def _abstained(question: dict, graded, reply: str) -> bool | None:
    """On a question the documents answer, whether the reply said they do not: NOT FOUND, whatever the grader read
    (a figure's grader reads no number in it), or the letter of the option saying so. None for a `none` question."""
    if question["kind"] == "none":
        return None
    if question["format"] == "mcq":
        return graded.answer == question["answer"]["options"][-1]
    return text_key(strip_reasoning(reply)) == text_key(NOT_FOUND)


def run(ctx: SuiteContext) -> SuiteResult:
    started = now()
    config = ctx.config
    settings = config.s4
    model = Model(config.model, config.root, config.model["mode"])
    ctx.log(f"{ID}: model {model.client.model} at {model.client.host}, mode {model.mode}, cache {model.cache.path} "
            f"({model.cache.entries()} entries), {settings['repeats']} sample(s) per payload at temperature "
            f"{config.model['temperature']}")
    s1 = {(r["corpus"], r["arm"], r["question_id"]): r for r in ctx.shared.get("s1_lq_rows", [])}
    b1 = {(r["corpus"], r["arm"], r["question_id"]): r for r in ctx.shared.get("s1_lq_baselines", [])}

    planned, excluded, problems = [], Counter(), []
    for corpus in ctx.corpora:
        for question in corpus["questions"]:
            for arm, budget, payload, tokens, present, verdict, problem in _payloads(ctx, corpus, question, s1, b1):
                if problem == "gate":
                    excluded[arm] += 1
                elif problem is not None:
                    problems.append((corpus["corpus_id"], arm, question["question_id"], problem))
                else:
                    planned.append((corpus, question, arm, budget, payload, tokens, present, verdict))

    arms = settings["arms"]
    ordered = [(c["corpus_id"], payload) for c, q, arm, budget, payload, tokens, present, verdict
               in sorted(planned, key=lambda p: (p[0]["corpus_id"], arms.index(p[2]), p[1]["question_id"]))]
    calls, call_rows, errors, asked = calls_mod.ask(ctx, ID, model, ordered, settings["repeats"],
                                                    int(config.model.get("concurrency", 2)), "model/s4-calls.jsonl")

    grades = []
    for corpus, question, arm, budget, payload, tokens, present, verdict in planned:
        sha = hashlib.sha256(payload).hexdigest() if payload is not None else None
        for sample in range(settings["repeats"]):
            base = {"$schema": output.schema_name("lq-grade-row"), "run_id": ctx.run.run_id, "suite": ID,
                    "case_id": f"{corpus['corpus_id']}/{arm}/{question['question_id']}#{sample}",
                    "corpus": corpus["corpus_id"], "family": "lq", "tier": corpus["tier"], "ratio": corpus["ratio"],
                    "arm": arm, "question_id": question["question_id"], "kind": question["kind"],
                    "format": question["format"], "attribute": question["attribute"],
                    "depth": question["attributes"]["depth"], "budget_input": budget, "sample": sample,
                    "payload_sha256": sha, "input_tokens": tokens, "fact_present": present,
                    "expected": question["answer"]["expected"]}
            if payload is None:
                grades.append({**base, "request_sha256": None, "reply": None, "verdict": verdict, "score": 0.0,
                               "answer": None, "detail": "the payload overflows the model's context"
                               if verdict == "overflow" else "assembly refused", "finish_reason": None,
                               "completion_tokens": None, "prompt_tokens": None, "abstained": None})
                continue
            answer = calls.get((sha, sample))
            if isinstance(answer, Exception) or answer is None:
                continue
            graded = grade(question["answer"], answer.text)
            usage = answer.provenance.get("usage") or {}
            grades.append({**base, "request_sha256": answer.request_sha256, "reply": answer.text[:2000],
                           "verdict": graded.verdict, "score": graded.score, "answer": graded.answer,
                           "detail": graded.detail, "finish_reason": answer.provenance.get("finish_reason"),
                           "completion_tokens": usage.get("completion_tokens"),
                           "prompt_tokens": usage.get("prompt_tokens"),
                           "abstained": _abstained(question, graded, answer.text)})
    ctx.run.write_jsonl(f"suites/{ID}/grades.jsonl", grades, "lq-grade-row",
                        "One row per LQ question, arm, corpus and sample: the reply, its grade, and whether the "
                        "needed chunks were in the payload")

    findings = []
    for cid, arm, qid, problem in problems:
        findings.append(finding(ctx, ID, {"suite": ID, "check": "payload", "arm": arm, "problem": problem},
                                case_id=f"{cid}/{arm}/{qid}", oracle="expected", checks=["payload"],
                                summary=f"S4 {cid} {arm} {qid}: {problem}"))
    if errors:
        findings.append(finding(ctx, ID, {"suite": ID, "check": "calls"}, case_id="model", oracle="producer",
                                checks=["replay_miss" if "CacheMiss" in errors[0] else "endpoint"],
                                summary=f"{len(errors)} model call(s) failed; first: {errors[0][:300]}"))
    suite_metrics, by_arm = _measure(settings, [c["tier"] for c in ctx.corpora], grades)
    suite_metrics.insert(0, metrics.count("s4.call_errors", "Model calls that failed or missed the cache", len(errors),
                                          maximum=0, suite=ID))
    suite_metrics.insert(1, metrics.count("s4.payload_problems", "LQ payloads unequal to S1's", len(problems),
                                          maximum=0, suite=ID))
    status = "fail" if findings else "pass"
    summary_path = f"suites/{ID}/summary.json"
    ctx.run.write_json(summary_path, {
        "$schema": output.schema_name("suite-summary"),
        "run_id": ctx.run.run_id, "suite": ID, "title": TITLE, "status": status, "started_at": started,
        "finished_at": now(), "requirements": [],
        "corpora": [{"id": "lq", "count": len(ctx.corpora)}], "adapters": [], "metrics": suite_metrics,
        "files": {"results": f"suites/{ID}/grades.jsonl", "findings": "findings.jsonl"},
        "model": {"model": model.client.model, "endpoint_host": model.client.host, "mode": model.mode,
                  "params": model.params, "cache": str(model.cache.path), "cache_entries": model.cache.entries(),
                  "context_limit": config.model.get("context_limit"), "server": model.server(),
                  "concurrency": max(1, int(config.model.get("concurrency", 2))), "calls": asked,
                  "endpoint_calls": model.calls, "cache_hits": sum(r["cache_hit"] for r in call_rows),
                  "excluded_by_gate": dict(sorted(excluded.items()))},
        "by_arm": by_arm,
    }, f"{ID}'s grades by arm and corpus tier, with intervals and paired differences")
    ctx.shared["s4_grades"], ctx.shared["s4_calls"] = grades, call_rows
    return SuiteResult(ID, TITLE, status, summary_path, suite_metrics, findings)


def _count(rows: list[dict], key: str) -> dict:
    return {value: {"n": sum(r[key] == value for r in rows),
                    "correct": sum(r["verdict"] == "correct" for r in rows if r[key] == value)}
            for value in sorted({r[key] for r in rows})}


def _measure(settings: dict, tiers: list[str], grades: list[dict]) -> tuple[list[dict], list[dict]]:
    resamples, seed = settings["bootstrap_resamples"], settings["bootstrap_seed"]
    groups = defaultdict(list)
    for row in grades:
        groups[(row["arm"], row["tier"])].append(row)
    order = {arm: i for i, arm in enumerate(settings["arms"])}
    tier_order = list(dict.fromkeys(tiers))
    reference = {(r["corpus"], r["question_id"], r["sample"]): r["verdict"] == "correct"
                 for r in grades if r["arm"] == settings["reference"]}
    out, by_arm = [], []
    for (arm, tier), rows in sorted(groups.items(), key=lambda kv: (order[kv[0][0]], tier_order.index(kv[0][1]))):
        verdicts = Counter(r["verdict"] for r in rows)
        clusters = defaultdict(list)
        for r in rows:
            clusters[r["corpus"]].append(1.0 if r["verdict"] == "correct" else 0.0)
        interval = stats.bootstrap(clusters, resamples, seed)
        accuracy = metrics.rate("s4.accuracy", "Correct answers", verdicts["correct"], len(rows), target=None,
                                suite=ID, arm=arm, family="lq", tier=tier,
                                description="All questions and samples; the interval is a cluster bootstrap over "
                                            "corpora")
        accuracy["interval"] = interval
        out.append(accuracy)
        answerable = [r for r in rows if r["abstained"] is not None]
        out.append(metrics.rate("s4.abstained", "Answerable questions answered NOT FOUND",
                                sum(r["abstained"] for r in answerable), len(answerable), target=None, suite=ID,
                                arm=arm, family="lq", tier=tier))
        out.append(metrics.rate("s4.distractor_rate", "Answers giving the wrong site's value or name",
                                verdicts["distractor"], len(rows), target=None, suite=ID, arm=arm, family="lq",
                                tier=tier))
        present = [r for r in rows if r["fact_present"]]
        absent = [r for r in rows if r["fact_present"] is False]
        paired = None
        if arm != settings["reference"]:
            differences = defaultdict(list)
            for r in rows:
                other = reference.get((r["corpus"], r["question_id"], r["sample"]))
                if other is not None:
                    differences[r["corpus"]].append((r["verdict"] == "correct") - other)
            n = sum(len(v) for v in differences.values())
            if n:
                paired = {"reference": settings["reference"], "n": n,
                          "difference": round(sum(sum(v) for v in differences.values()) / n, 6),
                          "interval": stats.bootstrap(differences, resamples, seed)}
        tokens = [r["input_tokens"] for r in rows if r["input_tokens"] is not None]
        by_arm.append({"arm": arm, "tier": tier, "n": len(rows), "verdicts": {v: verdicts[v] for v in VERDICTS},
                       "accuracy": accuracy["value"], "interval": interval,
                       "given": {"present": {"n": len(present), "correct": sum(r["verdict"] == "correct"
                                                                                 for r in present)},
                                 "absent": {"n": len(absent), "correct": sum(r["verdict"] == "correct"
                                                                               for r in absent)}},
                       "by_kind": _count(rows, "kind"), "by_format": _count(rows, "format"),
                       "abstained": {"n": len(answerable), "count": sum(r["abstained"] for r in answerable)},
                       "input_tokens_mean": round(sum(tokens) / len(tokens), 1) if tokens else None,
                       "paired": paired})
    return out, by_arm
