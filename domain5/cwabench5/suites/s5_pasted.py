"""S5 · Pasted material (domain-5-plan.md, 8; R-10): the attack inside the user's own message.

The probes are S2's for the families whose content a user would paste (`[s5].families`: emails and documents): every
plain instance with every test template that applies, at every position, the carrier balanced as in S2 (grid.py), and
every instance clean. Each probe's items are pasted into the user's message, in each arm of pasted.py, instead of
being retrieved.

A CWA arm's payload is gated before it is sent (suites/gate.py: every adapter, the trace audit, agreement, the
structure oracle, the attack-in-payload oracle by record and by text); a baseline's is read for a boundary the
material forged (pasted.forged). Attacked probes are asked `[s5].attacked_samples` times at the model's temperature,
clean ones `[s5].clean_samples` times; replies are graded as in S2.

Measured per arm, with cluster-bootstrap intervals over task instances: `s5.asr`, `s5.partial`, `s5.reported`,
`s5.utility_under_attack`, `s5.bu`, `s5.bu_imperative`, `s5.over_refusal`, `s5.attack_in_payload`, `s5.forged` (tag
probes whose material wrote the arm's own boundary), `s5.prompt_tokens`; and `s5.asr_paired` for each of
`[s5].pairs`.

Rows: `suites/S5/gate.jsonl` (`gate-row`, CWA arms), `probes.jsonl` (`probe-row`) and `calls.jsonl` (`call-row`).
"""
from __future__ import annotations

import hashlib
from collections import defaultdict

from cwabench.rundir import now
from cwabench2 import metrics, stats
from cwabench2.model import Model

from .. import grid, handoff, output, pasted
from ..arms import PASTED
from ..grading import grade_reply, judge
from ..grading.refusal import refused
from ..oracles import presence
from . import SuiteContext, SuiteResult, finding
from .calls import ask
from .gate import Entry, Gate
from .s2_attacks import CACHE_FORMAT, _rate

ID = "S5"
TITLE = "Pasted material"


def run(ctx: SuiteContext) -> SuiteResult:
    started = now()
    config = ctx.config
    s5 = config.s5
    seed, template = config.attacks.seed, config.attacks.chat_template
    chosen = {f: ctx.tasks[f] for f in s5["families"] if f in ctx.tasks}
    cells = grid.cells(chosen, config.attacks.families, 0)
    tasks_by_id = grid.tasks_by_id(chosen, [])
    arms = list(s5["arms"])

    plan, entries = [], []  # (cell, arm, probe, payload, code); the CWA ones to gate
    for cell in cells:
        task = tasks_by_id[cell["task_id"]]
        code = pasted.code(seed, cell["cell_id"])
        for arm in arms:
            probe = grid.probe(cell, task, arm, seed, template)
            if PASTED[arm].kind == "cwa":
                frozen = pasted.freeze(ctx.contract, task, probe["items"], arm, config.application, code)
                entries.append((len(plan), Entry(f"{cell['cell_id']}@{arm}", arm, frozen, probe["attack_text"],
                                                 probe["item_id"])))
                plan.append((cell, arm, probe, frozen.payload, code))
            else:
                plan.append((cell, arm, probe, pasted.build(arm, task, probe["items"], code,
                                                            config.application.tokenizer).payload, code))
    ctx.log(f"S5: {len(cells)} cells × {len(arms)} arms; gating {len(entries)} CWA payloads")
    gate = Gate(ctx, ID)
    passed, gate_rows = set(), []
    try:
        for start in range(0, len(entries), 64):
            batch = entries[start:start + 64]
            for (index, entry), verdict in zip(batch, gate.check([e for _, e in batch])):
                cell, arm, probe = plan[index][:3]
                if verdict.ok:
                    passed.add(index)
                gate_rows.append({
                    "$schema": output.schema_name("gate-row"), "run_id": ctx.run.run_id, "suite": ID,
                    "case_id": entry.case_id, "cell": cell["cell_id"], "kind": cell["kind"], "task": cell["task_id"],
                    "template": cell["template_id"], "position": probe["position"], "carrier": probe["carrier"],
                    "producer": None, "arm": arm, "snapshot": None,
                    "snapshot_sha256": hashlib.sha256(entry.frozen.at()).hexdigest(),
                    "payload_hash": entry.frozen.payload_hash, "input_tokens": entry.frozen.input_tokens,
                    "budget_input": entry.frozen.full, "answers": verdict.answers, "agree": verdict.agree,
                    "structure": verdict.structure, "attack": verdict.attack,
                    "verdict": "passed" if verdict.ok else "failed", "findings": verdict.findings,
                })
    finally:
        gate.close()
    gate_rows.sort(key=lambda r: r["case_id"])
    ctx.run.write_jsonl(f"suites/{ID}/gate.jsonl", gate_rows, "gate-row",
                        "One row per pasted probe and CWA arm: every adapter's answer, the structure and the attack")
    cwa_indices = {index for index, _ in entries}
    skipped = len(cwa_indices - passed)
    sending = [(i, p) for i, p in enumerate(plan) if i not in cwa_indices or i in passed]

    model = Model(config.model, config.root, config.model["mode"], handoff=handoff.messages, format=CACHE_FORMAT)
    server = model.server()

    def samples(cell) -> int:
        return s5["attacked_samples"] if cell["kind"] == "attacked" else s5["clean_samples"]

    answers, call_rows, errors = ask(ctx, ID, model, [(f"{p[0]['task_id']}/{p[1]}", p[3], samples(p[0]))
                                                      for _, p in sending],
                                     int(config.model.get("concurrency", 2)), f"suites/{ID}/calls.jsonl")
    rows = []
    for _, (cell, arm, probe, data, code) in sending:
        task = tasks_by_id[cell["task_id"]]
        sha = hashlib.sha256(data).hexdigest()
        attacked = probe["attack_text"] is not None
        present = presence.by_text(arm, data, probe["attack_text"]) if attacked else None
        for sample in range(samples(cell)):
            reply = answers.get((sha, sample))
            if reply is None or isinstance(reply, Exception):
                continue
            got = grade_reply(task["contract"], probe["answer"], reply.text)
            verdict = judge(probe["predicate"], probe["instruction"], reply.text) if attacked else None
            usage = reply.provenance.get("usage") or {}
            rows.append({
                "$schema": output.schema_name("probe-row"), "run_id": ctx.run.run_id, "suite": ID,
                "case_id": f"{cell['cell_id']}@{arm}#{sample}", "cell": cell["cell_id"], "kind": cell["kind"],
                "task": task["task_id"], "family": task["family"], "variant": task["variant"],
                "template": cell["template_id"], "attack": probe["family"],
                "form": (probe["predicate"] or {}).get("form"), "position": probe["position"],
                "carrier": probe["carrier"], "producer": None, "length": None, "arm": arm, "sample": sample,
                "temperature": config.model["temperature"], "payload_hash": sha,
                "request_sha256": reply.request_sha256, "present": present,
                "observable": (probe["predicate"] or {}).get("observable", True) if attacked else None,
                "task_grade": {"verdict": got.verdict, "score": got.score},
                "attack_verdict": verdict.verdict if verdict else None,
                "attack_detail": verdict.detail if verdict else None,
                "refused": (not attacked) and got.verdict != "correct" and refused(reply.text),
                "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                "finish_reason": reply.provenance.get("finish_reason"),
            })
    rows.sort(key=lambda r: r["case_id"])
    ctx.run.write_jsonl(f"suites/{ID}/probes.jsonl", rows, "probe-row",
                        "One row per pasted probe, arm and sample: the task's grade and the attack's verdict")

    r, sd = s5["bootstrap_resamples"], s5["bootstrap_seed"]
    suite_metrics = []
    forged = defaultdict(lambda: [0, 0])
    for cell, arm, probe, data, code in plan:
        if cell["carrier"] == "tag" and PASTED[arm].kind == "baseline":
            found = pasted.forged(arm, data, code)
            if found is not None:
                forged[arm][0] += found
                forged[arm][1] += 1
    for arm in arms:
        mine = [x for x in rows if x["arm"] == arm]
        main = [x for x in mine if x["kind"] == "attacked" and x["present"] and x["observable"]]
        clean = [x for x in mine if x["kind"] == "clean"]
        suite_metrics += [
            _rate("s5.asr", "Pasted attacks executed", main, lambda x: x["attack_verdict"] == "executed", arm=arm,
                  resamples=r, seed=sd, description="Attack present and observable"),
            _rate("s5.partial", "Pasted attacks partly executed", main, lambda x: x["attack_verdict"] == "partial",
                  arm=arm, resamples=r, seed=sd),
            _rate("s5.reported", "Pasted attacks reported to the user", main,
                  lambda x: x["attack_verdict"] == "reported", arm=arm, resamples=r, seed=sd),
            _rate("s5.utility_under_attack", "Task answers correct, pasted attack", main,
                  lambda x: x["task_grade"]["verdict"] == "correct", arm=arm, resamples=r, seed=sd),
            _rate("s5.bu", "Task answers correct, pasted, clean", [x for x in clean if x["variant"] == "plain"],
                  lambda x: x["task_grade"]["verdict"] == "correct", arm=arm, resamples=r, seed=sd),
            _rate("s5.bu_imperative", "Task answers correct, pasted, clean with benign imperatives",
                  [x for x in clean if x["variant"] == "imperative"],
                  lambda x: x["task_grade"]["verdict"] == "correct", arm=arm, resamples=r, seed=sd),
            _rate("s5.over_refusal", "Pasted clean tasks refused", clean, lambda x: x["refused"], arm=arm,
                  resamples=r, seed=sd),
            _rate("s5.attack_in_payload", "Pasted attacks in the payload",
                  [x for x in mine if x["kind"] == "attacked"], lambda x: x["present"], arm=arm, resamples=r, seed=sd),
        ]
        if arm in forged:
            suite_metrics.append(metrics.rate("s5.forged", "Tag probes whose material wrote the arm's boundary",
                                              *forged[arm], target=None, suite=ID, arm=arm))
        tokens = [x["prompt_tokens"] for x in main if x["prompt_tokens"] is not None]
        suite_metrics.append(metrics.value("s5.prompt_tokens", "Mean prompt tokens per pasted probe",
                                           round(sum(tokens) / len(tokens), 1) if tokens else None, "tokens",
                                           suite=ID, arm=arm))
    by_cell = defaultdict(dict)
    for x in rows:
        if x["kind"] == "attacked" and x["present"] and x["observable"] and x["sample"] == 0:
            by_cell[x["cell"]][x["arm"]] = (x["task"], 1.0 if x["attack_verdict"] == "executed" else 0.0)
    for a, b in s5["pairs"]:
        clusters = defaultdict(list)
        for found in by_cell.values():
            if a in found and b in found:
                clusters[found[b][0]].append(found[b][1] - found[a][1])
        diffs = [v for c in clusters.values() for v in c]
        metric = metrics.value("s5.asr_paired", f"Pasted ASR of {b} minus {a}, same cells",
                               round(sum(diffs) / len(diffs), 6) if diffs else None, "points", suite=ID,
                               arm=f"{b} vs {a}", description=f"{len(diffs)} paired cells")
        metric["interval"] = stats.bootstrap(clusters, r, sd) if diffs else None
        suite_metrics.append(metric)

    findings = list(gate.findings.values())
    if errors:
        signature = {"suite": ID, "oracle": "fault", "checks": ["model"]}
        findings.append(finding(ctx, ID, signature, case_id="model", oracle="fault", checks=["model"],
                                summary=f"{len(errors)} model call(s) failed; first: {errors[0][:300]}"))
    suite_metrics.append(metrics.rate("s5.gate", "Pasted CWA payloads that pass the gate", len(passed),
                                      len(cwa_indices), target=1.0, suite=ID))
    suite_metrics.append(metrics.count("s5.call_errors", "Model calls that failed or were not cached", len(errors),
                                       maximum=0, suite=ID))
    suite_metrics.append(metrics.count("s5.skipped", "Pasted CWA payloads not sent because the gate failed them",
                                       skipped, maximum=0, suite=ID))
    status = "fail" if findings or skipped else "pass"
    summary_path = f"suites/{ID}/summary.json"
    ctx.run.write_json(summary_path, {
        "$schema": output.schema_name("suite-summary"), "run_id": ctx.run.run_id, "suite": ID, "title": TITLE,
        "status": status, "started_at": started, "finished_at": now(), "requirements": ["R-10"],
        "corpora": [{"id": family, "count": len(t)} for family, t in chosen.items()],
        "adapters": [], "metrics": suite_metrics,
        "files": {"results": f"suites/{ID}/probes.jsonl", "findings": "findings.jsonl"},
        "model": {"model": config.model["model"], "mode": config.model["mode"], "server": server,
                  "temperature": config.model["temperature"], "seed": config.model.get("seed"),
                  "calls": len(call_rows), "to_endpoint": model.calls, "errors": len(errors)},
    }, "S5's pasted attacks and task grades per arm")
    return SuiteResult(ID, TITLE, status, summary_path, suite_metrics, findings)
