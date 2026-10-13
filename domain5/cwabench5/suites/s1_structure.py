"""S1 · Structural isolation (domain-5-plan.md, 8): every payload S2 will send, built and read before a model sees it.

For every cell of the grid (grid.py) and every arm:

- **CWA arms**: the probe's snapshot (application/snapshot.py) is assembled by every adapter and judged as Domain 2's
  gate judges (its `assemble`): the prediction (outcome, payload bytes, `input_tokens`, included items), Domain 1's
  trace audit, agreement between adapters; then the **structure oracle** reads the payload (oracles/structure.py), and
  the **attack-in-payload oracle** reads it by trace and by text, which must agree (oracles/presence.py). A cell and
  arm passes the gate when all of that holds; S2 sends only what passed.
- **Baselines**: each payload is built (baselines.py) and read with the arm's own delimiters: whether the attack forged
  its structure, and whether raw chat-template markers reach the model. Both are results, not gates: a conventional
  application sends these payloads. The attack-in-payload oracle reads them too.

Then, on each family's first plain instance:

- **claims** (application/claims.py): each field-level claim added to the clean `cwa` snapshot, assembled by every
  adapter; every adapter must return the clean payload and exclude the claimed item with the expected reason;
- **floods**: the instruction repeated past the budget, ranked last; every adapter must keep every protected item, omit
  the flood `over_budget` and return the clean payload; a front truncation at the same count is recorded.

With `[s1].tokenize` (oracles/tokens.py), the served model's tokenizer counts the special tokens each arm's payload
holds for the first marker cells, against the same instance's clean payload.

Rows go to `suites/S1/gate.jsonl` (`gate-row`), `baselines.jsonl` (`baseline-row`), `claims.jsonl` (`claim-row`)
and `tokens.jsonl` (`token-row`); `ctx.shared["gate"]` holds each cell and arm's verdict for S2 and S7.
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

from cwabench import adapters as adapters_mod
from cwabench.oracles import differential
from cwabench.oracles.auditor import audit
from cwabench.rundir import now
from cwabench2 import metrics
from cwabench2.suites import s1_gate as d2_gate

from .. import baselines, grid, output
from ..application import claims as claims_mod
from ..application import route as route_mod
from ..application.snapshot import freeze
from ..arms import ARMS
from ..oracles import presence, structure
from . import SuiteContext, SuiteResult, finding

ID = "S1"
TITLE = "Structural isolation"
REQUIREMENTS = ["R-1", "R-6", "R-7", "R-10", "R-13", "R-15", "R-16", "R-21", "R-22", "R-23"]
FAULTS = d2_gate.FAULTS


def _candidates(snapshot: dict) -> dict[str, int]:
    counts = Counter()
    for batch in snapshot["batches"]:
        for item in batch["items"]:
            counts[item["slot"]] += 1
    return dict(counts)


def assemble_claim(job: dict) -> dict:
    """One claim or flood snapshot on every adapter: each answer's outcome, payload hash, exclusions and audit."""
    adapters, contract = d2_gate._WORKER["adapters"], d2_gate._WORKER["contract"]
    answers = []
    for name in job["adapters"]:
        if name not in adapters:
            continue
        invocation = adapters_mod.invoke(adapters[name], job["data"], job["timeout"], job["cwd"])
        outcome = adapters_mod.classify(invocation)
        trace = outcome.trace if isinstance(outcome.trace, dict) else None
        failed = audit(contract, job["data"], outcome.payload, trace).failed if (
            outcome.kind in ("assembled", "refused") and trace is not None) else []
        signature = differential.signature(outcome.kind, outcome.payload, trace)
        answers.append({
            "adapter": name, "outcome": outcome.kind, "payload_hash": signature["payload"],
            "excluded": sorted([r.get("item_id"), r.get("reason")] for r in (trace or {}).get("excluded") or []),
            "included": sorted({r.get("item_id") for r in (trace or {}).get("included") or []}),
            "audit_failed": failed, "problem": (outcome.problem or "")[:300] or None,
        })
    return {"answers": answers}


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
                      requirements=REQUIREMENTS if oracle in ("auditor", "expected", "label") else [])
        known = self.found.get(row["finding_id"])
        if known is not None:
            known["occurrences"] += 1
            return known["finding_id"]
        row["adapter"] = adapter
        if data is not None:
            row["reproducer"] = {"snapshot": self.ctx.run.blobs.put(data, "application/json"), "spec_path": None}
        self.found[row["finding_id"]] = row
        return row["finding_id"]


def _judge(report: Report, arm: str, case_id: str, frozen, data: bytes, result: dict, tally) -> list[str]:
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
            found.append(report.add("auditor", sorted(answer["audit_failed"]), adapter, arm, case_id,
                                    f"{adapter} output for {case_id} breaks {', '.join(answer['audit_failed'])}",
                                    data))
        if not answer["matches_prediction"]:
            found.append(report.add("expected", ["prediction"], adapter, arm, case_id,
                                    f"{adapter} on {case_id}: {answer['outcome']}, {answer['input_tokens']} tokens; "
                                    f"expected all {len(frozen.items)} items, {frozen.input_tokens} tokens", data))
    tally["agreement"][0] += result["agree"]
    tally["agreement"][1] += 1
    if not result["agree"]:
        found.append(report.add("differential", ["agreement"], None, arm, case_id,
                                f"adapters disagree on {case_id}: " + " vs ".join("/".join(g) for g in
                                                                                   result["groups"]), data))
    return found


def run(ctx: SuiteContext) -> SuiteResult:
    started = now()
    config = ctx.config
    names = list(ctx.adapters)
    report = Report(ctx)
    for problem in route_mod.check(ctx.contract):
        report.add("self-check", ["profile"], None, "all", "profile", f"the arms' route or profiles: {problem}", None)
    cwa_arms = [a for a in config.arms if ARMS[a].kind == "cwa"]
    base_arms = [a for a in config.arms if ARMS[a].kind == "baseline"]
    tasks_by_id = {t["task_id"]: t for family in ctx.tasks.values() for t in family}
    cells = grid.cells(ctx.tasks, config.attacks.families, config.html_templates)
    seed, template = config.attacks.seed, config.attacks.chat_template
    settings, tokenizer = config.application, config.application.tokenizer
    tally = defaultdict(lambda: [0, 0])  # check → [passed, total]
    gate: dict[tuple[str, str], bool] = {}
    forged = defaultdict(lambda: [0, 0])  # (arm, carrier) → [forged, read]
    markers = defaultdict(lambda: [0, 0])  # (arm, carrier) → [raw markers, payloads]
    dropped = defaultdict(lambda: [0, 0])  # arm → [attacks dropped by the producer, html probes]
    rows, rows_b, rows_c = [], [], []
    contract_args = (ctx.contract.path, config.contract_commit, config.allow_dirty)
    ctx.log(f"S1: {len(cells)} cells × {len(cwa_arms)} CWA arms × {len(names)} adapter(s), {len(base_arms)} baselines")
    with ProcessPoolExecutor(max_workers=config.concurrency, initializer=d2_gate._init,
                             initargs=(ctx.adapters, contract_args)) as pool:
        for start in range(0, len(cells), 64):
            batch = cells[start:start + 64]
            jobs, metas = [], []
            for cell in batch:
                task = tasks_by_id[cell["task_id"]]
                for arm in cwa_arms:
                    probe = grid.probe(cell, task, arm, seed, template)
                    frozen = freeze(ctx.contract, task, probe["items"], arm, settings)
                    data = frozen.at()
                    jobs.append({"data": data, "adapters": names, "timeout": config.timeout_s,
                                 "cwd": str(config.root), "payload_source": config.adapters.payload_source,
                                 "fact": None, "keep": False, "candidates": _candidates(frozen.snapshot),
                                 "expected": {"outcome": "assembled", "refusal_reason": None,
                                              "payload_hash": frozen.payload_hash,
                                              "input_tokens": frozen.input_tokens, "included": frozen.included}})
                    metas.append((cell, arm, probe, frozen, data))
            for (cell, arm, probe, frozen, data), result in zip(metas, pool.map(d2_gate.assemble, jobs)):
                case_id = f"{cell['cell_id']}@{arm}"
                found = _judge(report, arm, case_id, frozen, data, result, tally)
                read = structure.cwa(frozen.payload, frozen.snapshot, frozen.items)
                tally["structure"][0] += read.ok
                tally["structure"][1] += 1
                if not read.ok:
                    found.append(report.add("expected", ["structure"], None, arm, case_id,
                                            f"{case_id}: {'; '.join(read.problems)}", data))
                attack = None
                if probe["attack_text"] is not None:
                    content_id = frozen.content.get(probe["item_id"])
                    by_record = presence.by_record_cwa(set(frozen.included), content_id, read.bodies,
                                                       probe["attack_text"])
                    by_text = presence.by_text(arm, frozen.payload, probe["attack_text"])
                    tally["presence"][0] += by_record == by_text
                    tally["presence"][1] += 1
                    if by_record != by_text:
                        found.append(report.add("self-check", ["presence"], None, arm, case_id,
                                                f"{case_id}: the attack is in the payload by record {by_record}, "
                                                f"by text {by_text}", data))
                    attack = {"item": content_id, "by_record": by_record, "by_text": by_text}
                    if cell["kind"] == "html":
                        dropped[arm][0] += not by_text
                        dropped[arm][1] += 1
                ok = not found
                gate[(cell["cell_id"], arm)] = ok
                rows.append({
                    "$schema": output.schema_name("gate-row"), "run_id": ctx.run.run_id, "suite": ID,
                    "case_id": case_id, "cell": cell["cell_id"], "kind": cell["kind"], "task": cell["task_id"],
                    "template": cell["template_id"], "position": probe["position"], "carrier": probe["carrier"],
                    "producer": cell["producer"], "arm": arm,
                    "snapshot": ctx.run.blobs.put(data, "application/json") if cell["kind"] == "clean" else None,
                    "snapshot_sha256": hashlib.sha256(data).hexdigest(), "payload_hash": frozen.payload_hash,
                    "input_tokens": frozen.input_tokens, "budget_input": frozen.full,
                    "answers": [{k: v for k, v in a.items() if not k.startswith("_")} for a in result["answers"]],
                    "agree": result["agree"], "structure": {"ok": read.ok, "problems": read.problems[:5]},
                    "attack": attack, "verdict": "failed" if found else "passed", "findings": sorted(set(found)),
                })
            for cell in batch:
                task = tasks_by_id[cell["task_id"]]
                for arm in base_arms:
                    probe = grid.probe(cell, task, arm, seed, template)
                    places, cwa_payload = None, None
                    if arm == "xml-unescaped":
                        twin = freeze(ctx.contract, task, probe["items"], "cwa", settings)
                        cwa_payload = twin.payload
                        places = Counter(t for t, _, _ in structure.expected(twin.snapshot, twin.items)[1])
                    built = baselines.build(arm, task, probe["items"], tokenizer, cwa_payload)
                    read = structure.baseline(arm, built.payload, places)
                    carrier = probe["carrier"] or "clean"
                    if read.forged is not None:
                        forged[(arm, carrier)][0] += read.forged
                        forged[(arm, carrier)][1] += 1
                    markers[(arm, carrier)][0] += read.raw_markers
                    markers[(arm, carrier)][1] += 1
                    attack = None
                    if probe["attack_text"] is not None:
                        by_text = presence.by_text(arm, built.payload, probe["attack_text"])
                        attack = {"by_record": True if cell["kind"] != "html" else by_text, "by_text": by_text}
                        if cell["kind"] == "html":
                            dropped[arm][0] += not by_text
                            dropped[arm][1] += 1
                    rows_b.append({
                        "$schema": output.schema_name("baseline-row"), "run_id": ctx.run.run_id, "suite": ID,
                        "case_id": f"{cell['cell_id']}@{arm}", "cell": cell["cell_id"], "kind": cell["kind"],
                        "task": cell["task_id"], "template": cell["template_id"], "position": probe["position"],
                        "carrier": probe["carrier"], "producer": cell["producer"], "arm": arm,
                        "payload_hash": built.payload_hash, "input_tokens": built.input_tokens,
                        "forged": read.forged, "raw_markers": read.raw_markers, "detail": read.detail,
                        "attack": attack,
                    })
            ctx.log(f"S1: {min(start + 64, len(cells))}/{len(cells)} cells")

        firsts = [family[0] for family in ctx.tasks.values() if family]
        jobs, metas = [], []
        for task in firsts:
            frozen = freeze(ctx.contract, task, task["items"], "cwa", settings)
            made = [claims_mod.claim(ctx.contract, frozen, name) for name in claims_mod.CLAIMS]
            made.append(claims_mod.flood(ctx.contract, frozen, claims_mod.INSTRUCTION, tokenizer))
            for item in made:
                jobs.append({"data": item.data, "adapters": names, "timeout": config.timeout_s,
                             "cwd": str(config.root)})
                metas.append((task, frozen, item))
        for (task, frozen, item), result in zip(metas, pool.map(assemble_claim, jobs)):
            case_id = f"{task['task_id']}/{item.name}"
            found, checked = [], []
            for answer in result["answers"]:
                excluded = [reason for id, reason in answer["excluded"] if id == item.item_id]
                others = [(id, reason) for id, reason in answer["excluded"] if id != item.item_id]
                ok = (answer["outcome"] == "assembled" and answer["payload_hash"] == frozen.payload_hash
                      and excluded == [item.reason] and not others and not answer["audit_failed"]
                      and set(frozen.included) <= set(answer["included"]))
                checked.append(ok)
                key = "instructions_kept" if item.name == "flood" else "exclusion"
                tally[key][0] += ok
                tally[key][1] += 1
                if not ok:
                    found.append(report.add("label", [item.name], answer["adapter"], "cwa", case_id,
                                            f"{answer['adapter']} on {case_id}: {answer['outcome']}, excluded "
                                            f"{excluded or 'nothing'} for the claim (expected {item.reason}), other "
                                            f"exclusions {others[:3]}, audit {answer['audit_failed']}", item.data))
            truncation = None
            if item.name == "flood":
                system = f"{task['instructions']}\n\n{task['output_contract']}"
                user = "\n\n".join([*(i["text"] for i in task["items"]), item.body, task["question"]])
                truncation = {"system_kept": claims_mod.truncated(system, user, frozen.full, tokenizer)}
            rows_c.append({
                "$schema": output.schema_name("claim-row"), "run_id": ctx.run.run_id, "suite": ID,
                "case_id": case_id, "task": task["task_id"], "claim": item.name, "item_id": item.item_id,
                "expected_reason": item.reason, "snapshot": ctx.run.blobs.put(item.data, "application/json"),
                "answers": result["answers"], "truncation": truncation,
                "verdict": "passed" if all(checked) and checked else "failed", "findings": sorted(set(found)),
            })

    rows_t = []
    if config.tokenize != "off":
        from ..oracles import tokens

        rows_t = tokens.run(ctx, cells, tasks_by_id, tally)
    ctx.run.write_jsonl(f"suites/{ID}/gate.jsonl", rows, "gate-row",
                        "One row per cell and CWA arm: every adapter's answer, the prediction, the structure oracle "
                        "and the attack-in-payload oracle")
    ctx.run.write_jsonl(f"suites/{ID}/baselines.jsonl", rows_b, "baseline-row",
                        "One row per cell and baseline: the payload, its count, whether the attack forged its "
                        "structure, raw markers, and the attack-in-payload oracle")
    ctx.run.write_jsonl(f"suites/{ID}/claims.jsonl", rows_c, "claim-row",
                        "One row per field-level claim and flood: every adapter's outcome and exclusions")
    if rows_t:
        ctx.run.write_jsonl(f"suites/{ID}/tokens.jsonl", rows_t, "token-row",
                            "One row per marker probe and arm: the special tokens the served tokenizer reads")
    ctx.shared["gate"] = {f"{c}@{a}": ok for (c, a), ok in sorted(gate.items())}
    ctx.shared["s1_rows"] = rows
    passed = sum(gate.values())
    labels = {"agreement": "Snapshots every adapter answered alike", "audit": "Answers that pass the trace audit",
              "prediction": "Answers equal to the prediction", "structure": "CWA payloads the structure oracle reads "
              "back item for item", "presence": "Attacks judged alike in the payload by record and by text",
              "exclusion": "Field-level claims excluded with the expected reason, payload unchanged",
              "instructions_kept": "Floods omitted with every protected item kept"}
    suite_metrics = [metrics.rate(f"s1.{name}", label, *tally[name], suite=ID) for name, label in labels.items()]
    suite_metrics.append(metrics.count("s1.faults", "Crashes, timeouts and invalid answers", tally["faults"][1],
                                       maximum=0, suite=ID))
    suite_metrics.append(metrics.rate("s1.gate", "Cells and CWA arms that pass the gate", passed, len(gate), suite=ID))
    for (arm, carrier), (yes, total) in sorted(forged.items()):
        suite_metrics.append(metrics.rate("s1.forged", "Baseline payloads whose structure the attack forged", yes,
                                          total, target=None, suite=ID, arm=arm, tier=carrier,
                                          description="Measured, not gated: by carrier (tier)"))
    for (arm, carrier), (yes, total) in sorted(markers.items()):
        if yes:
            suite_metrics.append(metrics.rate("s1.raw_markers", "Payloads handing the model raw chat-template "
                                              "markers", yes, total, target=None, suite=ID, arm=arm, tier=carrier,
                                              description="Measured, not gated: by carrier (tier)"))
    for arm, (yes, total) in sorted(dropped.items()):
        suite_metrics.append(metrics.rate("s1.dropped_by_producer", "HTML attacks the producer kept out of the "
                                          "payload", yes, total, target=None, suite=ID, arm=arm,
                                          description="Measured, not gated: half the HTML probes use the text "
                                                      "producer"))
    if "special_tokens" in tally:
        suite_metrics.append(metrics.rate("s1.special_tokens", "Marker probes whose payload the served tokenizer "
                                          "reads as more special tokens than the clean payload",
                                          *tally["special_tokens"], target=None, suite=ID))
    findings = list(report.found.values())
    gates = [m for m in suite_metrics if m["status"] in ("pass", "fail")]
    status = "fail" if findings or any(m["status"] == "fail" for m in gates) else (
        "partial" if ctx.unavailable or not names else "pass")
    summary_path = f"suites/{ID}/summary.json"
    ctx.run.write_json(summary_path, {
        "$schema": output.schema_name("suite-summary"), "run_id": ctx.run.run_id, "suite": ID, "title": TITLE,
        "status": status, "started_at": started, "finished_at": now(), "requirements": REQUIREMENTS,
        "corpora": [{"id": family, "count": len(t)} for family, t in ctx.tasks.items()],
        "adapters": [{"adapter": n, "status": "unavailable" if n in ctx.unavailable else (
            "fail" if any(f["adapter"] == n for f in findings) else "pass"), "error": ctx.unavailable.get(n)}
            for n in config.adapters.use],
        "metrics": suite_metrics,
        "files": {"results": f"suites/{ID}/gate.jsonl", "findings": "findings.jsonl"},
        "gate": {"passed": passed, "total": len(gate), "failed": sorted(f"{c}@{a}" for (c, a), ok in gate.items()
                                                                        if not ok)[:200]},
    }, "S1's structural isolation: the gate, the baselines' structure, the field claims and the floods")
    return SuiteResult(ID, TITLE, status, summary_path, suite_metrics, findings)
