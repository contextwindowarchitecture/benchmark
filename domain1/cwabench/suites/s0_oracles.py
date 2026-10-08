"""S0 · Oracle self-check (domain-1-plan.md, section 11). Runs before any adapter, on the spec's own corpus:

- the independent primitives reproduce every published digest, payload hash and token count;
- the independent renderer (canon/render.py), given only what each expected trace included, writes every published
  payload byte for byte and counts its input_tokens, which S7's predicted thresholds rest on;
- the auditor passes every expected output (no false positives);
- the auditor catches mutated outputs (its kill rate), with every surviving mutant listed.

Every number the other suites report rests on these oracles, so each run measures them again.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from collections import Counter, defaultdict

from .. import metrics, output
from ..canon import digest as digest_mod
from ..canon import render as render_mod
from ..canon.payloads import ParseError, parse
from ..canon.tokenizers import TOKENIZERS
from ..oracles.auditor import CHECKS, audit
from ..oracles.mutants import Mutator
from ..rundir import now
from . import SuiteContext, SuiteResult

ID = "S0"
TITLE = "Oracle self-check"
KILL_TARGET = 0.95


def _reference_digest(ctx: SuiteContext):
    """The spec's own digest generator, used only to cross-check ours; None when it cannot be loaded."""
    path = ctx.contract.path / "conformance" / "generators" / "digest.py"
    try:
        spec = importlib.util.spec_from_file_location("cwa_reference_digest", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.snapshot_digest
    except Exception:  # an optional cross-check; its absence is reported, not fatal
        return None


def _row(ctx, case_id, check, status, detail=None, **extra) -> dict:
    return {
        "$schema": output.schema_name("self-check"),
        "run_id": ctx.run.run_id,
        "suite": ID,
        "case_id": case_id,
        "check": check,
        "operator": extra.get("operator"),
        "position": extra.get("position"),
        "status": status,
        "killed_by": extra.get("killed_by", []),
        "detail": detail,
        "trace": extra.get("trace"),
        "payload": extra.get("payload"),
    }


def run(ctx: SuiteContext) -> SuiteResult:
    started = now()
    reference = _reference_digest(ctx)
    rows, findings = [], []
    tally = defaultdict(lambda: [0, 0])  # check → [passed, total]
    operators = defaultdict(lambda: [0, 0])  # operator → [killed, total]
    kills_by_check = Counter()
    survivors = []

    def record(row: dict, *, severity: str = "error", requirements=()) -> None:
        rows.append(row)
        if row["status"] == "fail":
            signature = {"suite": ID, "case_id": row["case_id"], "check": row["check"]}
            finding_id = hashlib.sha256(output.dumps(signature, compact=True).encode()).hexdigest()[:12]
            findings.append({
                "$schema": output.schema_name("finding"),
                "finding_id": finding_id,
                "run_id": ctx.run.run_id,
                "suite": ID,
                "adapter": None,
                "case_id": row["case_id"],
                "oracle": "self-check",
                "checks": [row["check"]],
                "severity": severity,
                "summary": f"oracle self-check {row['check']} fails on {row['case_id']}: {row['detail']}",
                "first_pointer": None,
                "requirements": list(requirements),
                "occurrences": 1,
                "reproducer": None,
            })

    for case in ctx.contract.cases:
        snapshot = json.loads(case.snapshot_bytes.decode("utf-8"))
        trace, payload = case.expected_trace, case.expected_payload
        context = trace.get("context") or {}

        ours = digest_mod.snapshot_digest(snapshot)
        ok = ours == context.get("snapshot_digest")
        tally["digest"][0] += ok
        tally["digest"][1] += 1
        record(_row(ctx, case.id, "digest", "pass" if ok else "fail",
                    None if ok else f"computed {ours}, published {context.get('snapshot_digest')}"), requirements=["R-22"])
        if reference is not None:
            theirs = reference(snapshot)
            ok = ours == theirs
            tally["digest_reference"][0] += ok
            tally["digest_reference"][1] += 1
            record(_row(ctx, case.id, "digest_reference", "pass" if ok else "fail",
                        None if ok else f"ours {ours}, the spec generator's {theirs}"))

        if payload is not None:
            result = trace.get("result") or {}
            ok = hashlib.sha256(payload).hexdigest() == result.get("hash")
            tally["payload_hash"][0] += ok
            tally["payload_hash"][1] += 1
            record(_row(ctx, case.id, "payload_hash", "pass" if ok else "fail",
                        None if ok else "sha256 of expected.payload.txt is not result.hash"), requirements=["R-21"])

            count = TOKENIZERS.get(snapshot.get("tokenizer"))
            try:
                parsed = parse(snapshot.get("renderer"), payload)
            except ParseError as error:
                parsed = None
                record(_row(ctx, case.id, "input_tokens", "fail", f"payload does not parse: {error}"))
                tally["input_tokens"][1] += 1
            if parsed is not None and count is not None:
                total = sum(count(text) for text in parsed.counted)
                ok = total == result.get("input_tokens")
                tally["input_tokens"][0] += ok
                tally["input_tokens"][1] += 1
                record(_row(ctx, case.id, "input_tokens", "pass" if ok else "fail",
                            None if ok else f"counted {total}, published {result.get('input_tokens')}"),
                       requirements=["R-16"])

            try:
                rendered = render_mod.from_trace(snapshot, trace)
                same = rendered.payload == payload
                counted = rendered.count(snapshot["tokenizer"]) if snapshot.get("tokenizer") in TOKENIZERS else None
                ok = same and counted == result.get("input_tokens")
                detail = None if ok else (f"rendered {len(rendered.payload)} bytes, "
                                          f"{'identical' if same else 'different'}; counted {counted}, "
                                          f"published {result.get('input_tokens')}")
            except (KeyError, StopIteration, ValueError) as error:
                ok, detail = False, f"cannot render from the trace: {error!r}"
            tally["render"][0] += ok
            tally["render"][1] += 1
            record(_row(ctx, case.id, "render", "pass" if ok else "fail", detail), requirements=["R-7", "R-16"])

        verdict = audit(ctx.contract, case.snapshot_bytes, payload, trace)
        ok = verdict.status == "pass"
        tally["audit_expected"][0] += ok
        tally["audit_expected"][1] += 1
        detail = None if ok else "; ".join(f"{id}: {verdict.checks[id].violations[0]}" for id in verdict.failed)
        record(_row(ctx, case.id, "audit_expected", "pass" if ok else "fail", detail,
                    killed_by=verdict.failed), requirements=case.rules)

        for mutant in Mutator(ctx.contract, snapshot).mutants(trace, payload):
            verdict = audit(ctx.contract, case.snapshot_bytes, mutant.payload, mutant.trace)
            killed = verdict.status == "fail"
            operators[mutant.operator][1] += 1
            operators[mutant.operator][0] += killed
            kills_by_check.update(verdict.failed)
            extra = {}
            if not killed:
                survivors.append({"case_id": case.id, "operator": mutant.operator, "position": mutant.position})
                extra = {"trace": ctx.run.blobs.put_json(mutant.trace),
                         "payload": ctx.run.blobs.put_text(mutant.payload) if mutant.payload is not None else None}
            rows.append(_row(ctx, case.id, "mutant", "killed" if killed else "survived", operator=mutant.operator,
                             position=mutant.position, killed_by=verdict.failed, **extra))

    killed = sum(k for k, _ in operators.values())
    mutants = sum(n for _, n in operators.values())
    labels = {
        "digest": ("Snapshot digests reproduced", "Our RFC 8785 digest equals every published context.snapshot_digest."),
        "digest_reference": ("Digest agrees with the spec's generator", "Our digest equals the one conformance/"
                             "generators/digest.py computes."),
        "payload_hash": ("Payload hashes reproduced", "SHA-256 of every expected payload equals its result.hash."),
        "input_tokens": ("Input token counts reproduced", "Our tokenizers and payload parsers reproduce every "
                         "published result.input_tokens."),
        "render": ("Payloads re-rendered", "Our renderer, given each expected trace's included items, writes the "
                   "published payload byte for byte and counts its input_tokens."),
        "audit_expected": ("Expected outputs pass the audit", "Zero false positives: the auditor accepts every published "
                           "expected output."),
    }
    suite_metrics = [
        metrics.rate(f"s0.{check}", labels[check][0], tally[check][0], tally[check][1], suite=ID,
                     description=labels[check][1])
        for check in labels if tally[check][1]
    ]
    suite_metrics.append(metrics.rate(
        "s0.auditor.kill_rate", "Auditor kill rate", killed, mutants, target=KILL_TARGET, suite=ID,
        description="Mutants of expected outputs the auditor reports. Survivors are listed in the suite summary."))

    base = f"suites/{ID}"
    ctx.run.write_jsonl(f"{base}/results.jsonl", rows, "self-check", "S0: one row per primitive check, audit and mutant")
    ctx.run.write_jsonl(f"{base}/findings.jsonl", findings, "finding", "S0: oracle failures")
    status = "fail" if findings or any(m["status"] == "fail" for m in suite_metrics) else "pass"
    summary_path = f"{base}/summary.json"
    ctx.run.write_json(summary_path, {
        "$schema": output.schema_name("suite-summary"),
        "run_id": ctx.run.run_id,
        "suite": ID,
        "title": TITLE,
        "status": status,
        "started_at": started,
        "finished_at": now(),
        "requirements": ["R-16", "R-21", "R-22"],
        "corpora": [{"id": "conformance.cases", "count": len(ctx.contract.cases)}],
        "adapters": [],
        "metrics": suite_metrics,
        "files": {"results": f"{base}/results.jsonl", "findings": f"{base}/findings.jsonl"},
        "mutation": {
            "mutants": mutants,
            "killed": killed,
            "operators": [{"operator": op, "mutants": n, "killed": k} for op, (k, n) in sorted(operators.items())],
            "by_check": [{"check": id, "label": CHECKS[id], "kills": kills_by_check[id]} for id in CHECKS],
            "survivors": survivors,
        },
    }, "S0: primitive reproduction, auditor false positives and kill rate")
    ctx.log(f"S0: auditor kill rate {killed}/{mutants}, {len(survivors)} survivor(s)")
    return SuiteResult(ID, TITLE, status, summary_path, suite_metrics, findings, [])
