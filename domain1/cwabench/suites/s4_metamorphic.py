"""S4 · Metamorphic relations (domain-1-plan.md, 7.4).

Seeds are drawn from the labeled corpora and the fixed fuzz seeds. Each relation turns a seed into instances: a base
snapshot (usually the seed) and a variant whose answer must relate to the base's in a known way. Every adapter is
judged against its own answer on the base. The variants' answers are also audited and compared across adapters,
since a variant is a snapshot no other suite has run.

A failure is grouped by signature (relation, adapter, where the answers differed) into one finding. Its seed is
minimized while the same relation still fails the same way, and the failing variant is written out as a draft case,
with the base beside it.
"""
from __future__ import annotations

import hashlib
import random
from collections import Counter, defaultdict

from .. import corpora, metrics, output
from ..adapters import FAULTS
from ..canon import validity
from ..canon.spelling import load_keeping
from ..oracles import metamorphic as mr
from ..rundir import now
from . import Coverage, SuiteContext, SuiteResult
from .generated import (AUDIT_RULES, Answers, Occurrence, compared, Plan, finding, first_difference, group, last_line, majority, minimize,
                        normalized, partition, pointer_shape, snapshot_plan, with_rules)

ID = "S4"
TITLE = "Metamorphic relations"
SEED = 20261006
CORPORA = [f"labeled.{n}" for n in corpora.LABELED] + ["fuzz.seeds"]


def _seeds(ctx, names: list[str], per: int, seed) -> list:
    out, seen = [], set()
    for name in names:
        found = [s for s in corpora.load(ctx.contract, [name])
                 if s.kind == "case" and s.digest not in seen and validity.is_valid(ctx.contract, s.data)]
        rng = random.Random(f"cwa-s4:{seed}:{name}")
        for snapshot in rng.sample(found, min(per, len(found))) if per > 0 else found:
            seen.add(snapshot.digest)
            out.append(snapshot)
    return out


def run(ctx: SuiteContext) -> SuiteResult:
    started = now()
    settings = ctx.config.section("s4")
    names = list(ctx.adapters)
    seed = settings.get("seed", SEED)
    corpus_names = settings.get("corpus", CORPORA)
    relations = settings.get("relations", list(mr.MAKERS))
    unknown = [r for r in relations if r not in mr.MAKERS]
    if unknown:
        raise ValueError(f"[s4].relations names unknown relations: {unknown}")
    reference = settings.get("reference", "python")
    reference = reference if reference in ctx.adapters else names[0]
    max_tests = int(settings.get("max_tests", 150))
    answers = Answers(ctx)

    seeds = _seeds(ctx, corpus_names, int(settings.get("seeds_per_corpus", 40)), seed)
    answers.fill([(reference, s.data) for s in seeds])
    made = []
    for s in seeds:
        for instance in mr.instances(ctx.contract, s.data, answers.get(reference, s.data).outcome, relations, str(seed)):
            made.append((s, instance))
    ctx.log(f"S4: {len(seeds)} seeds → {len(made)} instances of {len(relations)} relations × {len(names)} adapter(s); "
            f"reference {reference}")
    answers.fill([(a, d) for _, i in made for a in names for d in (i.base, i.data)],
                 lambda n, total: ctx.log(f"S4: {n}/{total} answers"))

    occurrences: list[Occurrence] = []
    judged = []
    for s, instance in made:
        title, rules = mr.RELATIONS[instance.relation]
        case_id = f"{s.case_id}/{instance.kind}"
        judgments, mine = [], []
        variant_answers = []
        for a in names:
            base, variant = answers.get(a, instance.base), answers.get(a, instance.data)
            variant_answers.append(variant)
            status, detail = mr.judge(ctx.contract, instance, base.outcome, variant.outcome)
            judgments.append((a, status, detail, base, variant))
            if status in ("fail", "triage"):
                mine.append(Occurrence(
                    ID, "metamorphic", [instance.kind],
                    {"suite": ID, "oracle": "metamorphic", "kind": instance.kind, "adapter": a,
                     "detail": normalized(detail)}, a, [a], f"{a} breaks {instance.relation} ({title}, {instance.kind}): "
                    f"{detail}", None, list(rules), case_id, instance.data,
                    "warning" if status == "triage" else "error",
                    {"kind": instance.kind, "relation": instance.relation, "seed": s.data, "base": instance.base,
                     "expect": instance.expect, "status": status, "detail": normalized(detail)}))
            for check in variant.audit_failed:
                violation = (variant.audit_detail.get(check) or [""])[0]
                mine.append(Occurrence(ID, "auditor", [check], {"suite": ID, "oracle": "auditor", "adapter": a,
                                                                "check": check, "violation": normalized(violation)},
                                       a, [a], f"{a} breaks {check} on a {instance.kind} variant: {violation[:300]}",
                                       None, with_rules(rules, AUDIT_RULES.get(check, [])), case_id, instance.data,
                                       context={"check": check, "violation": normalized(violation)}))
            if variant.kind in FAULTS:
                problem = last_line(variant.outcome.problem)
                mine.append(Occurrence(ID, "fault", [variant.kind], {"suite": ID, "oracle": "fault", "adapter": a,
                                                                     "outcome": variant.kind,
                                                                     "problem": normalized(problem, 80)},
                                       a, [a], f"{a} {variant.kind} on a {instance.kind} variant: {problem[:200]}",
                                       None, list(rules),
                                       case_id, instance.data, context={"kind": variant.kind}))
        agreeing = compared(variant_answers, instance.data)
        groups = partition(agreeing)
        if len(groups) > 1:
            lead = next(v for v in agreeing if v.adapter == groups[0][0])
            other = next(v for v in agreeing if v.adapter == groups[1][0])
            diff = first_difference(lead, other)
            odd = sorted(set(names) - set(groups[0])) if len(groups[0]) > 1 else []
            mine.append(Occurrence(ID, "differential", ["agreement"],
                                   {"suite": ID, "oracle": "differential", "groups": groups, "stage": diff["stage"],
                                    "pointer": pointer_shape(diff["pointer"])}, None, names,
                                   f"adapters disagree on a {instance.kind} variant "
                                   f"({' vs '.join('/'.join(g) for g in groups)}) first at {diff['stage']}"
                                   + (f" {diff['pointer']}" if diff["pointer"] else ""), diff["pointer"],
                                   list(rules), case_id, instance.data,
                                   context={"groups": groups, "stage": diff["stage"], "odd": odd}))
        occurrences += mine
        judged.append((s, instance, case_id, judgments, groups, mine))

    grouped = group(occurrences)
    findings = {key: finding(ctx, found) for key, found in grouped.items()}
    ctx.log(f"S4: {len(occurrences)} failure(s) in {len(findings)} finding(s); minimizing")
    plans = []
    for key, found in grouped.items():
        first = found[0]
        if first.oracle == "metamorphic":
            plans.append((findings[key], _relation_plan(ctx, first, answers, names, reference, str(seed))))
        else:
            plans.append((findings[key], snapshot_plan(ctx, first, answers, names)))
    minimizations = minimize(ctx, plans, max_tests)

    # Rows ---------------------------------------------------------------------------------------------------------------
    blobs = ctx.run.blobs
    rows, coverage = [], []
    tally = defaultdict(Counter)  # (kind, adapter) → statuses
    for s, instance, case_id, judgments, groups, mine in judged:
        keys = sorted({o.key for o in mine})
        store = bool(keys)
        verdict = "failed" if any(o.severity == "error" for o in mine) else ("triage" if mine else "passed")
        title, rules = mr.RELATIONS[instance.relation]
        rows.append({
            "$schema": output.schema_name("relation-row"),
            "run_id": ctx.run.run_id,
            "suite": ID,
            "relation": instance.relation,
            "kind": instance.kind,
            "title": title,
            "seed": {"corpus": s.corpus, "case_id": s.case_id, "snapshot_sha256": s.digest},
            "base_sha256": hashlib.sha256(instance.base).hexdigest(),
            "variant_sha256": hashlib.sha256(instance.data).hexdigest(),
            "base": blobs.put(instance.base, "application/json") if store else None,
            "variant": blobs.put(instance.data, "application/json") if store else None,
            "base_is_seed": instance.base_is_seed,
            "expect": {k: v for k, v in instance.expect.items() if k != "mapping"},
            "judgments": [{"adapter": a, "status": status, "detail": detail,
                           "base": base.summary(blobs if store and status != "pass" else None),
                           "variant": variant.summary(blobs if store else None)}
                          for a, status, detail, base, variant in judgments],
            "variant_agree": len(groups) <= 1,
            "verdict": verdict,
            "requirements": list(rules),
            "findings": keys,
        })
        for a, status, detail, base, variant in judgments:
            tally[(instance.kind, a)][status] += 1
            coverage.append(Coverage(a, list(rules), [], [f"relation:{instance.relation}", f"relation:{instance.kind}"],
                                     status in ("pass", "skipped")))

    # Metrics ------------------------------------------------------------------------------------------------------------
    suite_metrics = []
    for a in names:
        mine = [j for r in rows for j in r["judgments"] if j["adapter"] == a]
        suite_metrics.append(metrics.rate("s4.pass_rate", "Relation instances holding", sum(j["status"] == "pass"
                                                                                            for j in mine),
                                          sum(j["status"] in ("pass", "fail") for j in mine), suite=ID, adapter=a,
                                          description="Triaged and skipped instances are not counted."))
        audited = [j["variant"] for j in mine if j["variant"]["outcome"] in ("assembled", "refused")]
        suite_metrics.append(metrics.rate("s4.variant_audit_rate", "Variant outputs passing the trace audit",
                                          sum(not v["audit_failed"] for v in audited), len(audited), suite=ID, adapter=a))
    for relation in relations:
        mine = [j for r in rows if r["relation"] == relation for j in r["judgments"]]
        suite_metrics.append(metrics.rate(f"s4.{relation.lower()}", f"{relation} {mr.RELATIONS[relation][0]}",
                                          sum(j["status"] == "pass" for j in mine),
                                          sum(j["status"] in ("pass", "fail") for j in mine), suite=ID))
    suite_metrics.append(metrics.rate("s4.variant_agreement", "Variants on which every adapter agrees",
                                      sum(r["variant_agree"] for r in rows), len(rows), suite=ID))
    suite_metrics.append(metrics.count("s4.triage", "Instances triaged for a person (MR13)",
                                       sum(j["status"] == "triage" for r in rows for j in r["judgments"]), suite=ID))
    suite_metrics.append(metrics.rate("s4.findings_minimized", "Findings whose minimized draft still reproduces",
                                      sum(m["reproduces"] for m in minimizations), len(minimizations), suite=ID))

    finding_rows = list(findings.values())
    errors = [f for f in finding_rows if f["severity"] == "error"]
    status = "fail" if errors else ("partial" if ctx.unavailable else "pass")
    base = f"suites/{ID}"
    ctx.run.write_jsonl(f"{base}/results.jsonl", rows, "relation-row", f"{ID}: one row per relation instance")
    ctx.run.write_jsonl(f"{base}/findings.jsonl", finding_rows, "finding", f"{ID}: one row per failure signature")
    summary_path = f"{base}/summary.json"
    kinds = sorted({k for k, _ in tally})
    ctx.run.write_json(summary_path, {
        "$schema": output.schema_name("suite-summary"),
        "run_id": ctx.run.run_id,
        "suite": ID,
        "title": TITLE,
        "status": status,
        "started_at": started,
        "finished_at": now(),
        "requirements": sorted({r for row in rows for r in row["requirements"]}, key=lambda r: int(r[2:])),
        "corpora": [{"id": n, "count": sum(s.corpus == n for s in seeds)} for n in corpus_names],
        "adapters": [],
        "metrics": suite_metrics,
        "files": {"results": f"{base}/results.jsonl", "findings": f"{base}/findings.jsonl"},
        "metamorphic": {
            "reference": reference,
            "seed": seed,
            "seeds": len(seeds),
            "relations": [{"id": r, "title": mr.RELATIONS[r][0], "requirements": list(mr.RELATIONS[r][1]),
                           "triaged": r in mr.TRIAGED} for r in relations],
            "instances": dict(sorted(Counter(i.kind for _, i in made).items())),
            "by_kind": [{"kind": k, "adapter": a, **{s: tally[(k, a)][s] for s in ("pass", "fail", "triage", "skipped")}}
                        for k in kinds for a in names],
        },
        "generated": {
            "findings": [{"finding_id": f["finding_id"], "oracle": f["oracle"], "checks": f["checks"],
                          "adapter": f["adapter"], "adapters": f["adapters"], "occurrences": f["occurrences"],
                          "severity": f["severity"], "summary": f["summary"], "minimized": f["minimized"]}
                         for f in finding_rows],
            "minimization": {"findings": len(minimizations), "reproduced": sum(m["reproduces"] for m in minimizations),
                             "tests": sum(m["tests"] for m in minimizations)},
        },
    }, f"{ID}: pass rates per relation and adapter, and findings")
    ctx.log(f"S4: {sum(r['verdict'] == 'passed' for r in rows)}/{len(rows)} instances clean, {len(finding_rows)} "
            f"finding(s)")
    return SuiteResult(ID, TITLE, status, summary_path, suite_metrics, finding_rows, coverage)


def _relation_plan(ctx, occurrence: Occurrence, answers: Answers, names: list[str], reference: str, key: str) -> Plan:
    """Reduce the seed while some instance of the same kind, made from the reduced seed, still fails the same way on
    the same adapter. The draft is that instance's variant; its base goes beside it."""
    context, adapter = occurrence.context, occurrence.adapter
    bases: dict[str, bytes] = {}

    def materialize(document):
        data = mr.dump(document)
        if not validity.is_valid(ctx.contract, data):
            return None
        reference_answer = answers.get(reference, data).outcome
        for instance in mr.instances(ctx.contract, data, reference_answer, [context["relation"]], key):
            if instance.kind != context["kind"]:
                continue
            status, detail = mr.judge(ctx.contract, instance, answers.get(adapter, instance.base).outcome,
                                      answers.get(adapter, instance.data).outcome)
            if status == context["status"] and normalized(detail) == context["detail"]:
                bases[hashlib.sha256(instance.data).hexdigest()] = instance.base
                return instance.data
        return None

    def reproduces(data):
        if data == occurrence.data:  # the variant that failed when it was found
            status, detail = mr.judge(ctx.contract, _instance(occurrence), answers.get(adapter, context["base"]).outcome,
                                      answers.get(adapter, data).outcome)
            return status == context["status"] and normalized(detail) == context["detail"]
        return hashlib.sha256(data).hexdigest() in bases

    return Plan(load_keeping(context["seed"]), materialize, reproduces, "case",
                lambda data: majority([answers.get(a, data) for a in names]),
                f"{context['relation']} ({context['kind']}): judged against base.snapshot.json, the base this "
                f"variant was made from",
                lambda data: {"base.snapshot.json": bases.get(hashlib.sha256(data).hexdigest(), context["base"])},
                occurrence.data)


def _instance(occurrence: Occurrence) -> mr.Instance:
    context = occurrence.context
    return mr.Instance(context["relation"], context["kind"], context["base"], occurrence.data, context["expect"])
