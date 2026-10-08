"""A whole run: set up the adapters, run the suites, and write the run directory."""
from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Callable

from . import coverage, output
from .adapters import Adapter, SetupError, setup
from .config import Config
from .contract import Contract
from .rundir import RunDir, now, worst
from .suites import (SuiteContext, SuiteResult, s0_oracles, s1_conformance, s2_repeatability, s4_metamorphic, s5_fuzz,
                     s6_admission, s7_scale, s8_refusal, s9_conflicts, s10_purity, s11_summarizer, s12_goldens)

# Canonical order: S12 reads the rows of S1, S6, S8 and S9; S4, S5 and S7 are the longest and run after the labeled
# suites and S11.
SUITES = {"S0": s0_oracles, "S1": s1_conformance, "S2": s2_repeatability, "S6": s6_admission, "S8": s8_refusal,
          "S9": s9_conflicts, "S11": s11_summarizer, "S4": s4_metamorphic, "S5": s5_fuzz, "S7": s7_scale,
          "S10": s10_purity, "S12": s12_goldens}


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def setup_adapters(config: Config, build: bool, log: Callable[[str], None] = _log):
    ready: dict[str, Adapter] = {}
    unavailable: dict[str, str] = {}
    for name, adapter_config in config.adapters.items():
        log(f"setup: {name} …")
        try:
            ready[name] = setup(config, adapter_config, build=build)
        except SetupError as error:
            unavailable[name] = str(error)
            log(f"setup: {name} unavailable: {error}")
            continue
        adapter = ready[name]
        stale = f", stale artifacts: {', '.join(adapter.stale_artifacts)}" if adapter.stale_artifacts else ""
        log(f"setup: {name} ready at {(adapter.checkout.commit or '?')[:12]} ({adapter.toolchain}){stale}")
    return ready, unavailable


def _adapter_entries(config: Config, ready: dict[str, Adapter], unavailable: dict[str, str]) -> dict:
    entries = {}
    for name, adapter_config in config.adapters.items():
        if name in ready:
            entries[name] = {"available": True, "error": None, **ready[name].manifest_entry()}
        else:
            entries[name] = {
                "available": False, "error": unavailable.get(name), "language": adapter_config.language,
                "checkout": str(adapter_config.checkout),
            }
    return entries


def run(config: Config, build: bool = True, log: Callable[[str], None] = _log,
        ci: dict | None = None) -> tuple[Path, str]:
    """Run the configured suites. With `ci` ({"profile", "source", "mirrors"}), the run is one of a CI profile's:
    the manifest says so, and ci.json compares it with the profile's previous run (cwabench/ci.py)."""
    contract = Contract(config.contract_path, config.contract_commit, config.allow_dirty)
    run_dir = RunDir(config)
    log(f"run {run_dir.run_id} → {run_dir.path}")
    run_dir.write_json("contract.json", contract.as_document(run_dir.run_id),
                       "Enumerations and texts from the pinned specification")

    ready, unavailable = setup_adapters(config, build, log)
    adapters = _adapter_entries(config, ready, unavailable)
    run_dir.write_json("manifest.json", run_dir.manifest(contract, adapters, "running", None, ci=ci),
                       "What this run was made from: harness, config, contract, adapters, host")

    results: list[SuiteResult] = []
    status = "error"
    context = SuiteContext(config, contract, run_dir, ready, unavailable, log)
    try:
        for suite in [s for s in SUITES if s in config.suites]:  # canonical order: S12 reads S1's rows
            results.append(SUITES[suite].run(context))
            log(f"{suite}: {results[-1].status}")

        observed = [item for result in results for item in result.coverage]
        run_dir.write_json("coverage.json",
                           coverage.build(run_dir.run_id, contract, list(config.adapters), config.suites, observed),
                           "Requirement, reason-code and tag coverage per adapter")
        findings = [f for result in results for f in result.findings]
        run_dir.write_jsonl("findings.jsonl", findings, "finding", "Every failure in this run, one row per signature")

        status = worst(r.status for r in results) if results else "error"
        summary = {
            "$schema": output.schema_name("summary"),
            "run_id": run_dir.run_id,
            "status": status,
            "started_at": run_dir.started_at,
            "finished_at": now(),
            "suites": [{"id": r.id, "title": r.title, "status": r.status, "summary": r.summary_path} for r in results],
            "adapters": [{"adapter": n, "available": e["available"], "error": e["error"]} for n, e in adapters.items()],
            "metrics": [m for r in results for m in r.metrics],
            "findings": {
                "total": len(findings),
                "by_suite": {r.id: len(r.findings) for r in results},
                "file": "findings.jsonl",
            },
        }
        run_dir.write_json("summary.json", summary, "Headline metrics and per-suite status")
        if ci is not None:
            from .ci import report

            image = context.shared.get("container_image")
            report(run_dir, run_dir.manifest(contract, adapters, status, now(), container=image, ci=ci), summary,
                   findings, ci)
    except BaseException:
        log(traceback.format_exc())
        status = "error"
        raise
    finally:
        image = context.shared.get("container_image")
        run_dir.write_json("manifest.json", run_dir.manifest(contract, adapters, status, now(), container=image, ci=ci),
                           "What this run was made from: harness, config, contract, adapters, host")
        run_dir.finalize(status, [r.id for r in results])
    return run_dir.path, status
