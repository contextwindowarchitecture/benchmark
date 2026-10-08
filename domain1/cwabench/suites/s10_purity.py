"""S10 · Purity and isolation (domain-1-plan.md, 7.10; R-18, R-23).

The strongest evidence available from outside that assembly calls no model and reads nothing ambient:

- On Linux, every snapshot runs with no network namespace, a read-only root and no usable HOME, and its answers
  must equal the host's. A second pass runs under strace: any attempt to open an IP socket is a violation, whether
  or not it could have succeeded. File writes outside /dev and reads outside the runtime are listed.
- On macOS, every snapshot runs under a sandbox that denies all network access (best effort: sandbox-exec is
  deprecated, but it is the only isolation the host offers).

Clock reads are not judged here. strace cannot see clock_gettime served by the vDSO, and reading a clock is harmless
unless it changes the answer, which S2's clock-shift cells test.
"""
from __future__ import annotations

import hashlib
import shutil
from collections import defaultdict

from .. import container, corpora, determinism, metrics, output
from ..determinism import HOST_CELLS, HOST_PLATFORM
from ..rundir import now
from . import SuiteContext, SuiteResult
from .s2_repeatability import container_answers, host_baseline

ID = "S10"
TITLE = "Purity and isolation"


def _finding(ctx, adapter: str, check: str, severity: str, summary: str, occurrences: int, snapshot=None) -> dict:
    signature = {"suite": ID, "adapter": adapter, "check": check}
    return {
        "$schema": output.schema_name("finding"),
        "finding_id": hashlib.sha256(output.dumps(signature, compact=True).encode()).hexdigest()[:12],
        "run_id": ctx.run.run_id,
        "suite": ID,
        "adapter": adapter,
        "case_id": "*",
        "oracle": "purity",
        "checks": [check],
        "severity": severity,
        "summary": summary[:1000],
        "first_pointer": None,
        "requirements": ["R-18", "R-23"],
        "occurrences": max(1, occurrences),
        "reproducer": {"snapshot": snapshot, "spec_path": None} if snapshot else None,
    }


def run(ctx: SuiteContext) -> SuiteResult:
    started = now()
    settings = ctx.config.section("s10")
    corpus = corpora.load(ctx.contract, settings.get("corpus", ["conformance"]))
    baseline = host_baseline(ctx, corpus, 1)
    reference = determinism.references(baseline)
    answers, problems = [], []

    if settings.get("macos_sandbox", True) and HOST_PLATFORM.startswith("darwin") and shutil.which("sandbox-exec"):
        ctx.log("S10: macOS sandbox with no network")
        answers += determinism.run_host(ctx.adapters, corpus, HOST_CELLS["sandbox:no-network"], 1,
                                        ctx.config.timeout_s, ctx.config.concurrency, ctx.config.root)

    container_info = None
    if ctx.config.section("container").get("enabled", True):
        try:
            isolated, container_info = container_answers(ctx, "linux-isolated", corpus, {})
            answers += isolated
        except container.ContainerError as error:
            problems.append(f"container unavailable: {error}")
            ctx.log(f"S10: container unavailable: {error.args[0].splitlines()[0]}")

    rows = [determinism.row(ctx, ID, a, reference.get((a.snapshot.digest, a.adapter))) for a in answers]
    findings = determinism.findings(ctx, ID, rows, "isolation")

    # Syscall analysis of the traced cell.
    traces = defaultdict(list)
    for answer in answers:
        if answer.strace is not None:
            text = answer.strace.read_text(encoding="utf-8", errors="replace") if answer.strace.exists() else ""
            traces[answer.adapter].append((container.parse_strace(text), answer))
    purity = []
    suite_metrics = []
    for adapter in ctx.adapters:
        reports = traces.get(adapter, [])
        merged = container.merge_strace([r for r, _ in reports]) if reports else None
        empty = sum(1 for r, _ in reports if r["calls"] == 0)
        if merged:
            purity.append({"adapter": adapter, "traced_invocations": len(reports), "empty_traces": empty, **merged})
            violations = sum(e["count"] for e in merged["network"])
            suite_metrics.append(metrics.count(
                "s10.network_violations", "IP network syscalls", violations, maximum=0, suite=ID, adapter=adapter,
                description="socket, connect, bind, send or listen calls on an IP or packet socket, under strace, with "
                            "no network namespace. Any one is a violation, successful or not."))
            if violations:
                example = next(a for r, a in reports if r["network"])
                findings.append(_finding(ctx, adapter, "network", "error",
                                         f"{adapter} makes IP network syscalls during assembly: "
                                         + "; ".join(e["event"] for e in merged["network"][:3]), violations,
                                         ctx.run.blobs.put(example.snapshot.data, "application/json")))
            writes = sum(e["count"] for e in merged["writes"])
            suite_metrics.append(metrics.count(
                "s10.file_writes", "File writes outside /dev", writes, maximum=0, suite=ID, adapter=adapter,
                description="open calls for writing outside /dev. Assembly has nothing to write."))
            if writes:
                findings.append(_finding(ctx, adapter, "writes", "warning",
                                         f"{adapter} opens files for writing: "
                                         + "; ".join(e["event"] for e in merged["writes"][:3]), writes))
            suite_metrics.append(metrics.count(
                "s10.reads_outside_runtime", "Distinct reads outside the runtime", len(merged["reads"]), suite=ID,
                adapter=adapter, description="Distinct paths opened for reading outside /usr, /lib, /opt/cwa, /proc, "
                                             "/sys and /dev, listed in the summary. Informational."))
            if empty == len(reports):
                problems.append(f"{adapter}: every strace log is empty; tracing did not work")
        elif container_info is not None:
            problems.append(f"{adapter}: no traced invocations")

        mine_rows = [r for r in rows if r["adapter"] == adapter and r["applied"] and r["matches_reference"]]
        same = sum(r["matches_reference"]["decision"] and r["matches_reference"]["payload"] is not False
                   for r in mine_rows)
        mine = determinism.tally(mine_rows)
        suite_metrics.append(metrics.rate(
            "s10.isolated_invariance", "Answers unchanged in isolation", same, len(mine_rows), suite=ID,
            adapter=adapter, description="Answers, in every isolation cell, whose decision and payload equal the "
                                         "host baseline's."))
        suite_metrics.append(metrics.rate(
            "s10.isolated_trace_invariance", "Traces unchanged in isolation", mine["trace"]["numerator"],
            mine["trace"]["denominator"], suite=ID, adapter=adapter,
            description="Answers, in every isolation cell, whose normalized trace equals the host baseline's."))

    by_cell = defaultdict(list)
    for r in rows:
        by_cell[(r["env_cell"], r["platform"], r["adapter"])].append(r)
    descriptions = {"sandbox:no-network": HOST_CELLS["sandbox:no-network"].description}
    descriptions.update({c.name: c.description for c in container.PROFILES["linux-isolated"].cells})
    matrix = [{"cell": cell, "platform": platform, "adapter": adapter, "description": descriptions.get(cell, ""),
               **determinism.tally(members)} for (cell, platform, adapter), members in sorted(by_cell.items())]

    status = "fail" if any(f["severity"] == "error" for f in findings) or any(
        m["status"] == "fail" for m in suite_metrics) else "pass"
    if status == "pass" and (problems or ctx.unavailable):
        status = "partial"

    base = f"suites/{ID}"
    ctx.run.write_jsonl(f"{base}/results.jsonl", rows, "determinism-row", "S10: one row per snapshot × adapter × cell")
    ctx.run.write_jsonl(f"{base}/findings.jsonl", findings, "finding", "S10: violations and isolation differences")
    summary_path = f"{base}/summary.json"
    ctx.run.write_json(summary_path, {
        "$schema": output.schema_name("suite-summary"),
        "run_id": ctx.run.run_id,
        "suite": ID,
        "title": TITLE,
        "status": status,
        "started_at": started,
        "finished_at": now(),
        "requirements": ["R-18", "R-23"],
        "corpora": [{"id": c, "count": sum(s.corpus == c for s in corpus)} for c in sorted({s.corpus for s in corpus})],
        "adapters": [],
        "metrics": suite_metrics,
        "files": {"results": f"{base}/results.jsonl", "findings": f"{base}/findings.jsonl"},
        "cells": matrix,
        "purity": {
            "adapters": purity,
            "problems": problems,
            "allowed_read_prefixes": list(container.READ_ALLOWED),
            "container": container_info,
            "clock_note": "Clock reads are not judged: the vDSO hides them from strace, and S2's clock-shift cells "
                          "test whether one changes an answer.",
        },
    }, "S10: isolation invariance, syscall report per adapter")
    ctx.log(f"S10: {len(rows)} answers, {len(findings)} finding(s)")
    return SuiteResult(ID, TITLE, status, summary_path, suite_metrics, findings, [])
