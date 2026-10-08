"""S2 · Repeatability across environments (domain-1-plan.md, 7.2; R-23).

Every snapshot runs through every adapter many times: repeatedly in the host's own environment, then in cells that
change one thing each (time zone, locale, thread counts, an empty environment, no HOME, the working directory, a
burst of concurrent runs), and on Linux in a container, where the wall clock can also be shifted 30 years either
way. With `matrix` set, each named container variant adds one cell: another platform (linux/amd64, emulated on an
arm64 host) or one toolchain swapped for another version (an older Python, Node, Go, or Rust at its MSRV). Each answer
is compared with that snapshot's first baseline answer on the host. A variant that cannot be built or run is a warning
finding, and the suite reports partial.
"""
from __future__ import annotations

from collections import defaultdict

from .. import container, corpora, determinism, metrics, output
from ..determinism import HOST_CELLS, HOST_PLATFORM
from ..rundir import now
from . import SuiteContext, SuiteResult

ID = "S2"
TITLE = "Repeatability across environments"
DEFAULT_CELLS = ["baseline", "tz:UTC", "tz:Pacific/Kiritimati", "tz:America/St_Johns", "tz:Asia/Kathmandu",
                 "locale:C", "locale:en_US.UTF-8", "locale:tr_TR.UTF-8", "locale:ja_JP.UTF-8", "threads:1",
                 "threads:16", "env:minimal", "home:unset", "cwd:empty", "burst:32"]


def host_baseline(ctx: SuiteContext, corpus, repetitions: int) -> list:
    """The host baseline answers, computed once per run and shared with S10."""
    cached = ctx.shared.get("host_baseline")
    if cached is not None and len(cached) >= repetitions * len(corpus) * len(ctx.adapters):
        return cached
    answers = determinism.run_host(ctx.adapters, corpus, HOST_CELLS["baseline"], repetitions, ctx.config.timeout_s,
                                   ctx.config.concurrency, ctx.config.root, ctx.log)
    ctx.shared["host_baseline"] = answers
    return answers


def container_answers(ctx: SuiteContext, profile: str, corpus, repetitions: dict) -> tuple[list, dict]:
    """A container profile's answers and their context, built and run once per run."""
    key = f"container:{profile}"
    if key not in ctx.shared:
        image = ctx.shared.get("container_image")
        if image is None:
            image = container.build_image(ctx.config, ctx.log)
            ctx.shared["container_image"] = image
        answers, probes, versions = container.run_profile(
            ctx.config, image, container.PROFILES[profile], corpus, list(ctx.adapters), repetitions, ctx.log)
        ctx.shared[key] = (answers, {"image": image, "probes": probes, "versions": versions})
    return ctx.shared[key]


def run(ctx: SuiteContext) -> SuiteResult:
    started = now()
    settings = ctx.config.section("s2")
    corpus = corpora.load(ctx.contract, settings.get("corpus", ["conformance"]))
    repetitions = int(settings.get("repetitions", 10))
    cell_repetitions = int(settings.get("cell_repetitions", 2))
    cells = settings.get("cells", DEFAULT_CELLS)
    unknown = [c for c in cells if c not in HOST_CELLS]
    if unknown:
        raise ValueError(f"[s2].cells names unknown cells: {', '.join(unknown)}")
    if "baseline" not in cells:
        cells = ["baseline", *cells]

    answers = list(host_baseline(ctx, corpus, repetitions))
    for name in cells:
        if name == "baseline":
            continue
        ctx.log(f"S2: host cell {name}")
        answers += determinism.run_host(ctx.adapters, corpus, HOST_CELLS[name], cell_repetitions,
                                        ctx.config.timeout_s, ctx.config.concurrency, ctx.config.root)

    container_info, container_error = None, None
    use_container = settings.get("container", True) and ctx.config.section("container").get("enabled", True)
    if use_container:
        try:
            linux, container_info = container_answers(
                ctx, "linux", corpus, {"linux:baseline": int(settings.get("linux_repetitions", 3))})
            answers += linux
        except container.ContainerError as error:
            container_error = str(error)
            ctx.log(f"S2: container cells unavailable: {error.args[0].splitlines()[0]}")

    variants, matrix_info, matrix_errors = settings.get("matrix", []), {}, {}
    if use_container:
        for name in variants:
            try:
                chosen = container.variant(ctx.config, name)
                image = container.build_image(ctx.config, ctx.log, chosen)
                found, _, versions = container.run_profile(
                    ctx.config, image, chosen.profile(), corpus, list(ctx.adapters),
                    {chosen.cell: int(settings.get("matrix_repetitions", 1))}, ctx.log)
                answers += found
                matrix_info[name] = {"cell": chosen.cell, "description": chosen.description, "image": image,
                                     "versions": versions}
            except container.ContainerError as error:
                matrix_errors[name] = str(error)
                ctx.log(f"S2: matrix variant {name} unavailable: {error.args[0].splitlines()[0]}")

    reference = determinism.references(answers)
    rows = [determinism.row(ctx, ID, a, reference.get((a.snapshot.digest, a.adapter))) for a in answers]
    findings = determinism.findings(ctx, ID, rows, "environment")
    for name, error in sorted(matrix_errors.items()):
        findings.append(_variant_finding(ctx, name, error))

    descriptions = {name: HOST_CELLS[name].description for name in cells}
    if container_info:
        descriptions.update({c.name: c.description for c in container.PROFILES["linux"].cells})
    descriptions.update({info["cell"]: info["description"] for info in matrix_info.values()})
    by_cell = defaultdict(list)
    for r in rows:
        by_cell[(r["env_cell"], r["platform"], r["adapter"])].append(r)
    matrix = [
        {"cell": cell, "platform": platform, "adapter": adapter, "description": descriptions.get(cell, ""),
         **determinism.tally(members)}
        for (cell, platform, adapter), members in sorted(by_cell.items(), key=lambda kv: (
            list(descriptions).index(kv[0][0]) if kv[0][0] in descriptions else 99, kv[0][2]))
    ]

    suite_metrics = []
    labels = {"decision": "Decision invariance", "payload": "Payload invariance", "trace": "Trace invariance"}
    for adapter in ctx.adapters:
        mine = determinism.tally([r for r in rows if r["adapter"] == adapter])
        for stage, label in labels.items():
            suite_metrics.append(metrics.rate(
                f"s2.{stage}_invariance", label, mine[stage]["numerator"], mine[stage]["denominator"], suite=ID,
                adapter=adapter, description=f"Answers whose {stage} equals the snapshot's first host baseline answer, "
                                             "across every repetition and cell."))
        suite_metrics.append(metrics.count(
            "s2.faults", "Faults across cells", mine["faults"], maximum=0, suite=ID, adapter=adapter,
            description="Invocations that crashed, timed out or printed invalid output in any cell."))
    probes = (container_info or {}).get("probes", {})
    for cell, by_adapter in sorted(probes.items()):
        for adapter, probe in sorted(by_adapter.items()):
            if adapter in ctx.adapters and not probe.get("effective"):
                suite_metrics.append(metrics.count(
                    "s2.clock_shift_not_applied", f"Clock shift not applied ({cell})", 1, suite=ID, adapter=adapter,
                    description="libfaketime does not reach this runtime, so the cell says nothing about its clock "
                                "reads. Its answers are recorded but not counted."))

    errors = [f for f in findings if f["severity"] == "error"]
    status = "fail" if errors or any(m["status"] == "fail" for m in suite_metrics) else "pass"
    if status == "pass" and (ctx.unavailable or (use_container and container_error) or matrix_errors):
        status = "partial"

    base = f"suites/{ID}"
    ctx.run.write_jsonl(f"{base}/results.jsonl", rows, "determinism-row",
                        "S2: one row per snapshot × adapter × cell × repetition")
    ctx.run.write_jsonl(f"{base}/findings.jsonl", findings, "finding", "S2: one row per adapter × cell × stage that differs")
    summary_path = f"{base}/summary.json"
    ctx.run.write_json(summary_path, {
        "$schema": output.schema_name("suite-summary"),
        "run_id": ctx.run.run_id,
        "suite": ID,
        "title": TITLE,
        "status": status,
        "started_at": started,
        "finished_at": now(),
        "requirements": ["R-23"],
        "corpora": [{"id": c, "count": sum(s.corpus == c for s in corpus)} for c in sorted({s.corpus for s in corpus})],
        "adapters": [],
        "metrics": suite_metrics,
        "files": {"results": f"{base}/results.jsonl", "findings": f"{base}/findings.jsonl"},
        "cells": matrix,
        "environment": {
            "host_platform": HOST_PLATFORM,
            "repetitions": {"baseline": repetitions, "cells": cell_repetitions,
                            "linux:baseline": int(settings.get("linux_repetitions", 3))},
            "reference": "each snapshot's first baseline answer on the host, per adapter",
            "container": container_info,
            "container_error": container_error,
            "matrix": [{"variant": name, **matrix_info[name]} for name in variants if name in matrix_info],
            "matrix_errors": [{"variant": name, "error": error[-2000:]}
                              for name, error in sorted(matrix_errors.items())],
        },
    }, "S2: invariance per adapter × cell, clock-shift probes, container image")
    ctx.log(f"S2: {len(rows)} answers, {len(findings)} finding(s)")
    return SuiteResult(ID, TITLE, status, summary_path, suite_metrics, findings, [])


def _variant_finding(ctx: SuiteContext, name: str, error: str) -> dict:
    """A matrix variant that could not be built or run: the cell says nothing, which is worth a warning, not a
    determinism failure. A toolchain at an assembler's declared minimum that cannot build it is still worth reading."""
    import hashlib

    signature = {"suite": ID, "variant": name, "check": "variant"}
    last = error.strip().splitlines()[-1] if error.strip() else "no message"
    return {
        "$schema": output.schema_name("finding"),
        "finding_id": hashlib.sha256(output.dumps(signature, compact=True).encode()).hexdigest()[:12],
        "run_id": ctx.run.run_id,
        "suite": ID,
        "adapter": None,
        "case_id": f"variant:{name}",
        "oracle": "fault",
        "checks": [f"variant:{name}"],
        "severity": "warning",
        "summary": f"matrix variant {name} could not be built or run: {last}"[:1000],
        "first_pointer": None,
        "requirements": ["R-23"],
        "occurrences": 1,
        "reproducer": None,
    }
