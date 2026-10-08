"""S5 · Generative fuzzing (domain-1-plan.md, 7.5).

Valid snapshots are generated in rounds; between rounds the intents whose coverage tags no trace has shown yet gain
weight (corpora/fuzz/steer.py). Each answer is judged by the auditor, by agreement among the adapters, and for faults:
a valid snapshot must assemble or refuse, never be rejected, crash, hang or answer garbage. Mutants, each breaking
exactly one snapshot check, must be rejected by every adapter.

Every failure is grouped by signature into one finding, whose smallest reproducer is minimized and written out as a
conformance-case draft (minimized/<finding-id>/).
"""
from __future__ import annotations

import hashlib
import json
import random
from collections import Counter, defaultdict
from dataclasses import dataclass

from .. import metrics, output
from ..adapters import FAULTS
from ..corpora.fuzz import generate as gen_mod
from ..corpora.fuzz import mutate as mut_mod
from ..corpora.fuzz.steer import Steering, observe
from ..rundir import now
from . import Coverage, SuiteContext, SuiteResult
from .generated import (AUDIT_RULES, CHECK_RULES, OPTIONAL_RENDERERS, Answer, Answers, Occurrence, Plan, compared,
                        finding,
                        first_difference, group, last_line, minimize, normalized, partition, pointer_shape, reproducer,
                        snapshot_plan, with_rules)

ID = "S5"
TITLE = "Generative fuzzing"
SEED = 20261006
BASE_RULES = ["R-21", "R-22", "R-23"]


@dataclass(frozen=True)
class _MutantInfo:
    """What minimizing a mutant's finding needs: the operator, and the valid parent it reduces and mutates again."""

    case_id: str
    operator: str
    check: str
    parent: str
    parent_data: bytes


def _rules(contract, trace: dict | None) -> list[str]:
    rules = set(BASE_RULES)
    reasons = {r["code"]: r["rule"] for r in contract.reasons}
    if isinstance(trace, dict):
        for row in trace.get("excluded") or []:
            code = contract.reason_template(row.get("reason")) if isinstance(row.get("reason"), str) else None
            if code in reasons:
                rules.add(reasons[code])
        refusal = (trace.get("refused") or {}).get("reason")
        if refusal in reasons:
            rules.update((reasons[refusal], "R-17"))
    return sorted(rules, key=lambda r: int(r[2:]))


def _reasons(trace: dict | None) -> list[tuple[str, str | None]]:
    if not isinstance(trace, dict):
        return []
    out = [(r["reason"], r.get("slot")) for r in trace.get("excluded") or []
           if isinstance(r, dict) and isinstance(r.get("reason"), str)]
    refusal = (trace.get("refused") or {}).get("reason") if isinstance(trace.get("refused"), dict) else None
    if refusal:
        out.append((refusal, None))
    return out


def _judge_valid(ctx, case_id: str, data: bytes, answers: list[Answer], rules: list[str]) -> list[Occurrence]:
    out = []
    renderer = json.loads(data).get("renderer")
    names = [a.adapter for a in answers]

    def occur(oracle, checks, signature, adapter, summary, pointer=None, context=None):
        cited = with_rules(rules, [r for c in checks for r in AUDIT_RULES.get(c, [])])
        out.append(Occurrence(ID, oracle, checks, {"suite": ID, "oracle": oracle, **signature}, adapter,
                              [adapter] if adapter else names, summary, pointer, cited, case_id, data,
                              context=context or {}))

    for a in answers:
        if a.kind in FAULTS:
            problem = last_line(a.outcome.problem)
            occur("fault", [a.kind], {"adapter": a.adapter, "outcome": a.kind, "problem": normalized(problem, 80)},
                  a.adapter, f"{a.adapter} {a.kind} on a valid snapshot: {problem[:200]}", context={"kind": a.kind})
        elif a.kind == "rejected":
            occur("expected", ["valid-rejected"], {"adapter": a.adapter, "check": "valid-rejected"}, a.adapter,
                  f"{a.adapter} rejected a snapshot that passes every snapshot check", context={"kind": "rejected"})
        elif a.kind == "unsupported" and renderer not in OPTIONAL_RENDERERS:
            occur("expected", ["unsupported-required"], {"adapter": a.adapter, "check": "unsupported-required"},
                  a.adapter, f"{a.adapter} does not provide a required component", context={"kind": "unsupported"})
        for check in a.audit_failed:
            violation = (a.audit_detail.get(check) or [""])[0]
            occur("auditor", [check], {"adapter": a.adapter, "check": check, "violation": normalized(violation)},
                  a.adapter, f"{a.adapter} breaks {check}: {violation[:300]}",
                  context={"check": check, "violation": normalized(violation)})
    agreeing = compared(answers, data)
    groups = partition(agreeing)
    if len(groups) > 1:
        lead = next(a for a in agreeing if a.adapter == groups[0][0])
        other = next(a for a in agreeing if a.adapter == groups[1][0])
        diff = first_difference(lead, other)
        odd = sorted(set(names) - set(groups[0])) if len(groups[0]) > 1 else []
        occur("differential", ["agreement"], {"groups": groups, "stage": diff["stage"],
                                              "pointer": pointer_shape(diff["pointer"])}, None,
              f"adapters disagree ({' vs '.join('/'.join(g) for g in groups)}) first at {diff['stage']}"
              + (f" {diff['pointer']}" if diff["pointer"] else ""), diff["pointer"],
              context={"groups": groups, "stage": diff["stage"], "odd": odd})
    return out


def _judge_mutant(ctx, mutant, answers: list[Answer]) -> list[Occurrence]:
    out = []
    rules = CHECK_RULES.get(mutant.check, ["R-17"])
    for a in answers:
        if a.kind in FAULTS:
            out.append(Occurrence(ID, "fault", [a.kind], {"suite": ID, "oracle": "fault", "adapter": a.adapter,
                                                          "outcome": a.kind, "operator": mutant.operator},
                                  a.adapter, [a.adapter], f"{a.adapter} {a.kind} on a mutant ({mutant.operator}): "
                                  f"{last_line(a.outcome.problem)[:200]}", None, rules, mutant.case_id, mutant.data,
                                  context={"kind": a.kind}))
        elif a.kind != "rejected":
            out.append(Occurrence(ID, "expected", ["not-rejected"], {"suite": ID, "oracle": "expected",
                                                                     "adapter": a.adapter, "operator": mutant.operator},
                                  a.adapter, [a.adapter], f"{a.adapter} {a.kind} a snapshot that breaks the "
                                  f"{mutant.check} check ({mutant.operator}) instead of rejecting it", None, rules,
                                  mutant.case_id, mutant.data, context={"kind": a.kind}))
    return out


def _plan(ctx, occurrence: Occurrence, answers: Answers, names: list[str], mutants: dict) -> Plan:
    mutant = mutants.get(occurrence.case_id)
    if mutant is None:
        return snapshot_plan(ctx, occurrence, answers, names)
    operator = mut_mod.BY_NAME[mutant.operator]

    def materialize(document):  # reduce the valid parent, then break the same check again
        made = mut_mod.mutate(ctx.contract, document, mutant.parent, random.Random(f"minimize:{mutant.case_id}"),
                              operator, mutant.case_id)
        return made.data if made else None

    return Plan(json.loads(mutant.parent_data), materialize, reproducer(occurrence, answers, names), "rejection",
                original=occurrence.data)


def run(ctx: SuiteContext) -> SuiteResult:
    started = now()
    settings = ctx.config.section("s5")
    seed = settings.get("seed", SEED)
    n_valid, n_mutated = int(settings.get("valid", 10000)), int(settings.get("mutated", 2000))
    round_size = max(1, int(settings.get("round", 1000)))
    max_tests = int(settings.get("max_tests", 200))
    contract, names = ctx.contract, list(ctx.adapters)
    answers = Answers(ctx)
    steering = Steering()
    ctx.log(f"S5: {n_valid} valid + {n_mutated} mutated snapshots × {len(names)} adapter(s), rounds of {round_size}")

    valid, generator_errors = [], []  # valid: (Generated, round, tags)
    start, number = 0, 0
    while start < n_valid:
        size = min(round_size, n_valid - start)
        weights, families = steering.start_round(number, size)
        batch = []
        for k in range(start, start + size):
            try:
                batch.append(gen_mod.generate(contract, random.Random(f"cwa-fuzz:{seed}:valid:{k}"), f"fuzz-{k:05d}",
                                              weights, families))
            except gen_mod.GeneratorError as error:
                generator_errors.append(str(error)[:500])
        answers.fill([(a, g.data) for g in batch for a in names])
        for g in batch:
            document = json.loads(g.data)
            tags = set()
            for a in names:
                answer = answers.get(a, g.data)
                tags |= set(observe(answer.outcome.trace if answer.kind in ("assembled", "refused") else None, document))
            steering.observe(tags)
            valid.append((g, number, sorted(tags)))
        ctx.log(f"S5: round {number}: {start + size}/{n_valid} valid; coverage {steering.covered()}/"
                f"{len(steering.universe)} tags")
        start, number = start + size, number + 1

    mutants = []
    operators = mut_mod.OPERATORS
    for k in range(n_mutated if valid else 0):
        operator = operators[k % len(operators)]
        rng = random.Random(f"cwa-fuzz:{seed}:mutated:{k}")
        for attempt in range(10):
            parent = valid[(k * 7 + attempt) % len(valid)][0]
            made = mut_mod.mutate(contract, json.loads(parent.data), parent.case_id, rng, operator, f"mutant-{k:05d}")
            if made is not None:
                mutants.append((made, parent.data))
                break
    answers.fill([(a, m.data) for m, _ in mutants for a in names],
                 lambda n, total: ctx.log(f"S5: mutants {n}/{total}"))
    ctx.log(f"S5: {len(mutants)} mutants answered")

    # Judge -----------------------------------------------------------------------------------------------------------
    occurrences: list[Occurrence] = []
    rows_input = []
    for g, number, tags in valid:
        found = [answers.get(a, g.data) for a in names]
        rules = sorted(set().union(*[_rules(contract, f.outcome.trace) for f in found]), key=lambda r: int(r[2:]))
        mine = _judge_valid(ctx, g.case_id, g.data, found, rules)
        occurrences += mine
        rows_input.append(("fuzz.valid", g.case_id, number, g.data, g.intents, None, "valid", found, mine, tags, rules))
    mutant_by_case = {}
    for m, parent_data in mutants:
        found = [answers.get(a, m.data) for a in names]
        mine = _judge_mutant(ctx, m, found)
        occurrences += mine
        mutant_by_case[m.case_id] = _MutantInfo(m.case_id, m.operator, m.check, m.parent, parent_data)
        rows_input.append(("fuzz.mutated", m.case_id, 0, m.data, (), {"operator": m.operator, "check": m.check,
                                                                     "parent": m.parent}, "rejected", found, mine, [],
                           CHECK_RULES.get(m.check, ["R-17"])))

    grouped = group(occurrences)
    findings = {key: finding(ctx, found) for key, found in grouped.items()}
    ctx.log(f"S5: {len(occurrences)} failure(s) in {len(findings)} finding(s); minimizing")
    plans = [(findings[key], _plan(ctx, found[0], answers, names, mutant_by_case)) for key, found in grouped.items()]
    minimizations = minimize(ctx, plans, max_tests)

    # Rows --------------------------------------------------------------------------------------------------------------
    blobs = ctx.run.blobs
    rows, coverage = [], []
    in_finding = defaultdict(set)
    for o in occurrences:
        in_finding[o.case_id].add(o.key)
    for corpus, case_id, number, data, intents, mutation, expected, found, mine, tags, rules in rows_input:
        keys = sorted(in_finding.get(case_id, ()))
        # a mutant may not even parse, so only a valid snapshot's renderer can excuse an unsupported answer
        groups = partition(compared(found, data) if corpus == "fuzz.valid" else [a for a in found if a.kind not in FAULTS])
        involved = {o.adapter for o in mine if o.adapter} | {a for o in mine for a in o.context.get("odd", [])}
        diff = next((o for o in mine if o.oracle == "differential"), None)
        rows.append({
            "$schema": output.schema_name("fuzz-row"),
            "run_id": ctx.run.run_id,
            "suite": ID,
            "corpus": corpus,
            "case_id": case_id,
            "round": number,
            "snapshot_sha256": hashlib.sha256(data).hexdigest(),
            "snapshot": blobs.put(data, "application/json") if keys else None,
            "bytes": len(data),
            "intents": list(intents),
            "mutation": mutation,
            "expected": expected,
            # blobs only for answers a finding involves: every answer, when the finding is a disagreement
            "answers": [a.summary(blobs if keys and (diff is not None or a.adapter in involved) else None)
                        for a in found],
            "agree": len(groups) <= 1,
            "groups": groups,
            "first_difference": ({"stage": diff.context["stage"], "pointer": diff.first_pointer,
                                  "between": [diff.context["groups"][0][0], diff.context["groups"][1][0]]}
                                 if diff else None),
            "verdict": "failed" if mine else "passed",
            "problems": [o.summary[:300] for o in mine][:10],
            "coverage_tags": tags,
            "requirements": rules,
            "findings": keys,
        })
        for a in found:
            ok = not any(o.adapter == a.adapter or a.adapter in o.context.get("odd", []) for o in mine)
            coverage.append(Coverage(a.adapter, rules, _reasons(a.outcome.trace), [f"fuzz:{corpus}"] + tags, ok))

    # Metrics -----------------------------------------------------------------------------------------------------------
    valid_rows = [r for r in rows if r["corpus"] == "fuzz.valid"]
    mutant_rows = [r for r in rows if r["corpus"] == "fuzz.mutated"]
    suite_metrics = []
    for adapter in names:
        mine = [a for r in rows for a in r["answers"] if a["adapter"] == adapter]
        suite_metrics.append(metrics.rate("s5.crash_free", "Answers without a crash, hang or garbage", sum(
            a["outcome"] not in FAULTS for a in mine), len(mine), suite=ID, adapter=adapter))
        audited = [a for r in valid_rows for a in r["answers"] if a["adapter"] == adapter
                   and a["outcome"] in ("assembled", "refused")]
        suite_metrics.append(metrics.rate("s5.audit_rate", "Generated outputs passing the trace audit", sum(
            not a["audit_failed"] for a in audited), len(audited), suite=ID, adapter=adapter))
        answered = [a for r in valid_rows for a in r["answers"] if a["adapter"] == adapter]
        suite_metrics.append(metrics.rate("s5.valid_answered", "Valid snapshots assembled or refused", sum(
            a["outcome"] in ("assembled", "refused") or a["outcome"] == "unsupported" for a in answered), len(answered),
            suite=ID, adapter=adapter, description="Rejecting a valid snapshot, or failing on it, counts against."))
        rejected = [a for r in mutant_rows for a in r["answers"] if a["adapter"] == adapter]
        suite_metrics.append(metrics.rate("s5.mutants_rejected", "Mutants rejected before assembly", sum(
            a["outcome"] == "rejected" for a in rejected), len(rejected), suite=ID, adapter=adapter))
    suite_metrics.append(metrics.rate("s5.agreement", "Generated snapshots on which every adapter agrees",
                                      sum(r["agree"] for r in valid_rows), len(valid_rows), suite=ID))
    suite_metrics.append(metrics.rate("s5.rejection_agreement", "Mutants every adapter rejects",
                                      sum(all(a["outcome"] == "rejected" for a in r["answers"]) for r in mutant_rows),
                                      len(mutant_rows), suite=ID))
    suite_metrics.append(metrics.rate("s5.tag_coverage", "Steering targets some trace exercised", steering.covered(),
                                      len(steering.universe), target=None, suite=ID,
                                      description="Coverage tags the intents aim at, as traces recorded them."))
    suite_metrics.append(metrics.rate("s5.findings_minimized", "Findings whose minimized draft still reproduces",
                                      sum(m["reproduces"] for m in minimizations), len(minimizations), suite=ID))
    suite_metrics.append(metrics.count("s5.generator_errors", "Generated snapshots the validity check refused",
                                       len(generator_errors), maximum=0, suite=ID,
                                       description="A generator bug: every generated snapshot must be valid."))

    # Corpus indexes ------------------------------------------------------------------------------------------------------
    ctx.run.write_json("corpora/fuzz.valid/index.json", {
        "$schema": output.schema_name("corpus-index"), "run_id": ctx.run.run_id, "corpus": "fuzz.valid",
        "generator": {"module": "cwabench.corpora.fuzz.generate", "seed": seed,
                      "settings": {"valid": n_valid, "round": round_size,
                                   "rng": "random.Random(f'cwa-fuzz:{seed}:valid:{index}')"}},
        "count": len(valid), "rounds": steering.history,
        "snapshots": [{"case_id": g.case_id, "sha256": r["snapshot_sha256"], "round": n, "intents": list(g.intents)}
                      for (g, n, _), r in zip(valid, valid_rows)],
    }, "S5: the steered valid corpus, with each round's weights")
    ctx.run.write_json("corpora/fuzz.mutated/index.json", {
        "$schema": output.schema_name("corpus-index"), "run_id": ctx.run.run_id, "corpus": "fuzz.mutated",
        "generator": {"module": "cwabench.corpora.fuzz.mutate", "seed": seed,
                      "settings": {"mutated": n_mutated, "operators": [o.name for o in operators]}},
        "count": len(mutants), "rounds": [],
        "snapshots": [{"case_id": m.case_id, "sha256": r["snapshot_sha256"], "intents": [m.operator]}
                      for (m, _), r in zip(mutants, mutant_rows)],
    }, "S5: the mutant corpus, one snapshot check broken in each")

    finding_rows = list(findings.values())
    errors = [f for f in finding_rows if f["severity"] == "error"]
    status = "fail" if errors or generator_errors else ("partial" if ctx.unavailable else "pass")
    base = f"suites/{ID}"
    ctx.run.write_jsonl(f"{base}/results.jsonl", rows, "fuzz-row", f"{ID}: one row per generated snapshot")
    ctx.run.write_jsonl(f"{base}/findings.jsonl", finding_rows, "finding", f"{ID}: one row per failure signature")
    outcomes = {corpus: {a: dict(Counter(x["outcome"] for r in rows if r["corpus"] == corpus for x in r["answers"]
                                         if x["adapter"] == a)) for a in names}
                for corpus in ("fuzz.valid", "fuzz.mutated")}
    by_round = []  # the common views need per-round outcomes without the rows
    for number in sorted({r["round"] for r in valid_rows}):
        mine = [r for r in valid_rows if r["round"] == number]
        by_round.append({"round": number, "snapshots": len(mine), "passed": sum(r["verdict"] == "passed" for r in mine),
                         "agree": sum(r["agree"] for r in mine),
                         "outcomes": {a: dict(Counter(x["outcome"] for r in mine for x in r["answers"]
                                                      if x["adapter"] == a)) for a in names}})
    by_operator = []
    for operator in operators:
        mine = [r for r in mutant_rows if r["mutation"]["operator"] == operator.name]
        by_operator.append({"operator": operator.name, "check": operator.check, "mutants": len(mine),
                            "rejected": {a: sum(x["outcome"] == "rejected" for r in mine for x in r["answers"]
                                                if x["adapter"] == a) for a in names}})
    summary_path = f"{base}/summary.json"
    ctx.run.write_json(summary_path, {
        "$schema": output.schema_name("suite-summary"),
        "run_id": ctx.run.run_id,
        "suite": ID,
        "title": TITLE,
        "status": status,
        "started_at": started,
        "finished_at": now(),
        "requirements": sorted({r for row in rows for r in row["requirements"]}, key=lambda r: int(r[2:])),
        "corpora": [{"id": "fuzz.valid", "count": len(valid_rows)}, {"id": "fuzz.mutated", "count": len(mutant_rows)}],
        "adapters": [],
        "metrics": suite_metrics,
        "files": {"results": f"{base}/results.jsonl", "findings": f"{base}/findings.jsonl"},
        "generated": {
            "seed": seed,
            "generator_errors": generator_errors[:20],
            "steering": {"universe": len(steering.universe), "covered": steering.covered(),
                         "rounds": steering.history, "uncovered": steering.uncovered()},
            "intents": dict(sorted(Counter(i for g, _, _ in valid for i in g.intents).items())),
            "outcomes": outcomes,
            "by_round": by_round,
            "operators": by_operator,
            "findings": [{"finding_id": f["finding_id"], "oracle": f["oracle"], "checks": f["checks"],
                          "adapter": f["adapter"], "adapters": f["adapters"], "occurrences": f["occurrences"],
                          "summary": f["summary"], "minimized": f["minimized"]} for f in finding_rows],
            "minimization": {"findings": len(minimizations), "reproduced": sum(m["reproduces"] for m in minimizations),
                             "tests": sum(m["tests"] for m in minimizations)},
        },
    }, f"{ID}: coverage steering, outcomes, mutant rejection and findings")
    ctx.log(f"S5: {sum(r['verdict'] == 'passed' for r in rows)}/{len(rows)} snapshots clean, {len(finding_rows)} "
            f"finding(s), {sum(m['reproduces'] for m in minimizations)} minimized")
    return SuiteResult(ID, TITLE, status, summary_path, suite_metrics, finding_rows, coverage)

