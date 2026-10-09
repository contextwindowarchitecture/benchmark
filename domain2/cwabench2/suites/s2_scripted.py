"""S2 · Scripted conversations (domain-2-plan.md, 8): every probe of every script, in every arm, sent to the model.

Arms, at each of `[s2].tiers` (absolute budgets of S1's):

- the CWA arms that passed S1's gate for the conversation. Each sends the payload the prediction gives, whose hash
  must equal the payload source's answer in S1: the bytes all four assemblers agreed on and the auditor passed. A
  conversation and arm that failed the gate is left out and counted;
- the baselines, as S1 built them. An `overflow` reaches no model and grades as `overflow`;
- the controls, once per probe with no budget: `control-full`, the fully specified task (the probe's `full` text), and
  `control-concat`, the fact sentences so far as one message (`concat`). Both carry the system prompt.

Each payload is asked `[s2].repeats` times (samples 0 … K−1, each its own cached call) and every reply is graded
(grading/). A grade row holds what grading needs and nothing that depends on the run's mode: no latency, no cache hit.
A `replay` run therefore writes the same grades as the `llm` run that filled the cache, byte for byte, but for the
run id. Calls are made once per distinct request and sample, however many arms send the same payload.

Measured, never gated: per arm and tier, aptitude (the rate of correct answers) with a cluster-bootstrap interval
over conversations, the stale and unparsed rates, accuracy with the needed facts in the payload and without, and each
arm's difference from `[s2].reference` on the same probes and samples. What gates the suite is the harness: every
CWA payload equal to its gated bytes, and every call answered (a replay miss or an endpoint error fails it).
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

from cwabench.canon import jcs
from cwabench.canon.tokenizers import TOKENIZERS
from cwabench.rundir import now

from .. import baselines, metrics, output, stats
from ..application.snapshots import ARMS, Point
from ..grading import grade
from ..model import CacheMiss, EndpointError, Model
from . import SuiteContext, SuiteResult, finding
from .s1_gate import _snapshots

ID = "S2"
TITLE = "Scripted conversations"
CONTROLS = ("control-full", "control-concat")
VERDICTS = ("correct", "stale", "distractor", "wrong", "unparsed", "overflow")


def control_payload(script: dict, text: str) -> bytes:
    return jcs.serialize_bytes({"system": [{"id": "system", "text": baselines.system_prompt(script)}], "tools": [],
                                "messages": [{"role": "user", "content": text}]})


def _payloads(ctx: SuiteContext, script: dict, probe: dict, s1: dict, b1: dict):
    """(arm, tier, budget, payload or None, input_tokens, fact_present, problem) for one probe in every S2 arm."""
    config, settings = ctx.config, ctx.config.s2
    point = Point("probe", probe["after_turn"], probe)
    cid = script["conversation_id"]
    for arm in settings["arms"]:
        if arm in CONTROLS:
            text = probe["full"] if arm == "control-full" else probe["concat"]
            count = TOKENIZERS[config.application.tokenizer]
            data = control_payload(script, text)
            tokens = count(baselines.system_prompt(script)) + count(text)
            yield arm, "control", None, data, tokens, True, None
            continue
        for tier in settings["tiers"]:
            if arm in baselines.ARMS:
                row = b1.get((cid, arm, probe["probe_id"], tier))
                if row is None:
                    yield arm, tier, None, None, None, None, f"S1 built no {arm} row at {tier}"
                    continue
                built = baselines.at(arm, script, point, row["budget_input"], config.baseline)
                if built.payload_hash != row["payload_hash"]:
                    yield arm, tier, row["budget_input"], None, None, None, "the baseline differs from S1's"
                    continue
                payload = built.payload if built.outcome == "fits" else None
                yield arm, tier, row["budget_input"], payload, built.input_tokens, row["fact"]["present"], None
                continue
            if not ctx.shared.get("gate", {}).get(f"{cid}/{arm}", False):
                yield arm, tier, None, None, None, None, "gate"
                continue
            row = s1.get((cid, arm, probe["probe_id"], tier))
            found = [(t, b, f) for t, b, f in _snapshots(ctx, script, ARMS[arm], point) if t == tier]
            if row is None or not found:
                yield arm, tier, None, None, None, None, f"S1 assembled no {arm} snapshot at {tier}"
                continue
            _, budget, frozen = found[0]
            expected = frozen.expect(budget)
            source = next((a for a in row["answers"] if a["adapter"] == config.adapters.payload_source), None)
            if source is None or expected.payload_hash != source["payload_hash"]:
                yield arm, tier, budget, None, None, None, "the payload differs from the gated bytes"
                continue
            yield arm, tier, budget, expected.payload, expected.input_tokens, row["fact"]["present"], None


def run(ctx: SuiteContext) -> SuiteResult:
    started = now()
    config, settings = ctx.config, ctx.config.s2
    model = Model(config.model, config.root, config.model["mode"])
    ctx.log(f"S2: model {model.client.model} at {model.client.host}, mode {model.mode}, cache {model.cache.path} "
            f"({model.cache.entries()} entries), {settings['repeats']} sample(s) per payload")
    s1 = {(r["conversation"], r["arm"], r["probe_id"], r["tier"]): r for r in ctx.shared.get("s1_rows", [])
          if r["point"] == "probe"}
    b1 = {(r["conversation"], r["arm"], r["probe_id"], r["tier"]): r for r in ctx.shared.get("s1_baselines", [])
          if r["point"] == "probe"}

    planned, excluded, problems = [], Counter(), []
    for family, scripts in ctx.conversations.items():
        for script in scripts:
            for probe in script["probes"]:
                for arm, tier, budget, payload, tokens, present, problem in _payloads(ctx, script, probe, s1, b1):
                    if problem == "gate":
                        excluded[arm] += 1
                        continue
                    if problem is not None:
                        problems.append((script["conversation_id"], arm, probe["probe_id"], tier, problem))
                        continue
                    planned.append((family, script, probe, arm, tier, budget, payload, tokens, present))

    # One call per distinct request and sample, in a fixed order, however many arms share it.
    calls: dict[tuple[str, int], object] = {}
    payload_of = {}
    for *_, payload, _, _ in planned:
        if payload is not None:
            sha = hashlib.sha256(payload).hexdigest()
            payload_of[sha] = payload
    jobs = [(sha, sample) for sha in sorted(payload_of) for sample in range(settings["repeats"])]
    ctx.log(f"S2: {len(planned)} payloads, {len(payload_of)} distinct, {len(jobs)} calls")
    errors = []

    def ask(job):
        sha, sample = job
        try:
            return job, model.ask(payload_of[sha], sample)
        except (CacheMiss, EndpointError) as error:
            return job, error

    done = 0
    with ThreadPoolExecutor(max_workers=max(1, int(config.model.get("concurrency", 2)))) as pool:
        for job, answer in pool.map(ask, jobs):
            calls[job] = answer
            done += 1
            if isinstance(answer, Exception):
                errors.append(f"{type(answer).__name__}: {answer}")
            if done % 200 == 0:
                ctx.log(f"S2: {done}/{len(jobs)} calls ({model.calls} to the endpoint)")

    call_rows = []
    for (sha, sample), answer in sorted(calls.items()):
        if isinstance(answer, Exception):
            continue
        call_rows.append({"$schema": output.schema_name("call-row"), "run_id": ctx.run.run_id, "suite": ID,
                          "key": answer.key, "request_sha256": answer.request_sha256, "payload_sha256": sha,
                          "sample": sample, "cache_hit": answer.cache_hit, "lookup_ms": answer.lookup_ms,
                          "provenance": answer.provenance})
    ctx.run.write_jsonl("model/calls.jsonl", call_rows, "call-row", "Provenance of every model call S2 made or "
                                                                   "replayed: one row per distinct request and sample")

    grades = []
    for family, script, probe, arm, tier, budget, payload, tokens, present in planned:
        sha = hashlib.sha256(payload).hexdigest() if payload is not None else None
        for sample in range(settings["repeats"]):
            answer = calls.get((sha, sample)) if sha else None
            base = {"$schema": output.schema_name("grade-row"), "run_id": ctx.run.run_id, "suite": ID,
                    "case_id": f"{script['conversation_id']}/{arm}/{probe['probe_id']}@{tier}#{sample}",
                    "conversation": script["conversation_id"], "family": family, "task": script["ground_truth"]["task"],
                    "turn_count": script["turn_count"], "arm": arm, "probe_id": probe["probe_id"],
                    "after_turn": probe["after_turn"], "tier": tier, "budget_input": budget, "sample": sample,
                    "payload_sha256": sha, "input_tokens": tokens, "fact_present": present,
                    "attributes": probe["attributes"], "expected": _expected(probe["answer"])}
            if payload is None:
                grades.append({**base, "request_sha256": None, "reply": None, "verdict": "overflow", "score": 0.0,
                               "answer": None, "detail": "the payload overflows the budget", "fields": None,
                               "finish_reason": None, "completion_tokens": None, "prompt_tokens": None})
                continue
            if isinstance(answer, Exception) or answer is None:
                continue
            graded = grade(probe["answer"], answer.text)
            usage = answer.provenance.get("usage") or {}
            grades.append({**base, "request_sha256": answer.request_sha256, "reply": answer.text[:2000],
                           **graded.as_json(), "finish_reason": answer.provenance.get("finish_reason"),
                           "completion_tokens": usage.get("completion_tokens"),
                           "prompt_tokens": usage.get("prompt_tokens")})
    ctx.run.write_jsonl(f"suites/{ID}/grades.jsonl", grades, "grade-row",
                        "One row per probe, arm, tier and sample: the reply, its grade, and whether the needed facts "
                        "were in the payload")
    ctx.shared["s2_grades"] = grades
    ctx.shared["s2_calls"] = call_rows

    findings = []
    for cid, arm, probe_id, tier, problem in problems:
        findings.append(finding(ctx, ID, {"suite": ID, "check": "payload", "arm": arm, "problem": problem},
                                case_id=f"{cid}/{arm}/{probe_id}@{tier}", oracle="expected", checks=["payload"],
                                summary=f"S2 {cid} {arm} {probe_id} at {tier}: {problem}"))
    if errors:
        findings.append(finding(ctx, ID, {"suite": ID, "check": "calls"}, case_id="model", oracle="producer",
                                checks=["replay_miss" if "CacheMiss" in errors[0] else "endpoint"],
                                summary=f"{len(errors)} model call(s) failed; first: {errors[0][:300]}"))
    suite_metrics, by_arm = _measure(ctx, grades)
    suite_metrics.insert(0, metrics.count("s2.call_errors", "Model calls that failed or missed the cache", len(errors),
                                          maximum=0, suite=ID))
    suite_metrics.insert(1, metrics.count("s2.payload_problems", "CWA and baseline payloads unequal to S1's",
                                          len(problems), maximum=0, suite=ID))
    status = "fail" if findings else "pass"
    summary_path = f"suites/{ID}/summary.json"
    ctx.run.write_json(summary_path, {
        "$schema": output.schema_name("suite-summary"),
        "run_id": ctx.run.run_id, "suite": ID, "title": TITLE, "status": status, "started_at": started,
        "finished_at": now(), "requirements": [],
        "corpora": [{"id": family, "count": len(scripts)} for family, scripts in ctx.conversations.items()],
        "adapters": [], "metrics": suite_metrics,
        "files": {"results": f"suites/{ID}/grades.jsonl", "findings": "findings.jsonl"},
        "model": {"model": model.client.model, "endpoint_host": model.client.host, "mode": model.mode,
                  "params": model.params, "cache": str(model.cache.path), "cache_entries": model.cache.entries(),
                  "context_limit": config.model.get("context_limit"), "calls": len(jobs),
                  "endpoint_calls": model.calls, "cache_hits": sum(r["cache_hit"] for r in call_rows),
                  "excluded_by_gate": dict(sorted(excluded.items()))},
        "by_arm": by_arm,
    }, "S2's grades by arm and tier, with intervals and paired differences")
    return SuiteResult(ID, TITLE, status, summary_path, suite_metrics, findings)


def _expected(answer: dict):
    if answer["kind"] == "record":
        return {name: spec["expected"] for name, spec in answer["fields"].items()}
    return answer["expected"]


def _measure(ctx: SuiteContext, grades: list[dict]) -> tuple[list[dict], list[dict]]:
    settings = ctx.config.s2
    resamples, seed = settings["bootstrap_resamples"], settings["bootstrap_seed"]
    groups = defaultdict(list)
    for row in grades:
        groups[(row["arm"], row["tier"])].append(row)
    order = {arm: i for i, arm in enumerate(settings["arms"])}
    tiers = ["control", *settings["tiers"]]
    paired_by = {(r["conversation"], r["probe_id"], r["tier"], r["sample"]): r["verdict"] == "correct"
                 for r in grades if r["arm"] == settings["reference"]}
    out, by_arm = [], []
    for (arm, tier), rows in sorted(groups.items(), key=lambda kv: (order[kv[0][0]], tiers.index(kv[0][1]))):
        verdicts = Counter(r["verdict"] for r in rows)
        clusters = defaultdict(list)
        for r in rows:
            clusters[r["conversation"]].append(1.0 if r["verdict"] == "correct" else 0.0)
        interval = stats.bootstrap(clusters, resamples, seed)
        aptitude = metrics.rate("s2.aptitude", "Correct answers", verdicts["correct"], len(rows), target=None,
                                suite=ID, arm=arm, tier=tier, description="All probes and samples; the interval is a "
                                                                          "cluster bootstrap over conversations")
        aptitude["interval"] = interval
        out.append(aptitude)
        out.append(metrics.rate("s2.stale_rate", "Answers giving an earlier value", verdicts["stale"], len(rows),
                                target=None, suite=ID, arm=arm, tier=tier))
        present = [r for r in rows if r["fact_present"]]
        absent = [r for r in rows if r["fact_present"] is False]
        given = {"present": {"n": len(present), "correct": sum(r["verdict"] == "correct" for r in present)},
                 "absent": {"n": len(absent), "correct": sum(r["verdict"] == "correct" for r in absent)}}
        by_turns = {}
        for turns in sorted({r["turn_count"] for r in rows}):
            subset = [r for r in rows if r["turn_count"] == turns]
            by_turns[str(turns)] = {"n": len(subset), "correct": sum(r["verdict"] == "correct" for r in subset)}
        paired = None
        if arm != settings["reference"] and tier != "control":
            differences = defaultdict(list)
            for r in rows:
                reference = paired_by.get((r["conversation"], r["probe_id"], r["tier"], r["sample"]))
                if reference is not None:
                    differences[r["conversation"]].append((r["verdict"] == "correct") - reference)
            n = sum(len(v) for v in differences.values())
            if n:
                paired = {"reference": settings["reference"], "n": n,
                          "difference": round(sum(sum(v) for v in differences.values()) / n, 6),
                          "interval": stats.bootstrap(differences, resamples, seed)}
        by_arm.append({"arm": arm, "tier": tier, "n": len(rows), "verdicts": {v: verdicts[v] for v in VERDICTS},
                       "aptitude": aptitude["value"], "interval": interval, "given": given, "by_turns": by_turns,
                       "paired": paired})
    return out, by_arm
