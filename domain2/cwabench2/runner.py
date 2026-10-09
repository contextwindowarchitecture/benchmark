"""A whole run: generate the conversations, run the suites, and write the run directory (domain-2-plan.md, 11).

The run directory is Domain 1's (cwabench.rundir) under Domain 2's prefix: index.json, manifest.json, config.toml,
contract.json, summary.json, findings.jsonl, suites/<id>/, blobs/, plus conversations/<family>/index.json, which lists
each script this run generated with the digest of its blob.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Callable

from dataclasses import replace

from cwabench import config as d1_config
from cwabench import rundir as rundir_mod
from cwabench.contract import Contract
from cwabench.runner import adapter_entries, setup_adapters
from cwabench.rundir import RunDir, now, worst

from . import __version__, output
from .config import ADAPTER_SUITES, Config, ConfigError
from .conversations import FAMILIES, generate
from .suites import SuiteContext, SuiteResult, s0_selfcheck, s1_gate, s7_goldens

SUITES = {"S0": s0_selfcheck, "S1": s1_gate, "S7": s7_goldens}  # canonical order: S7 reads S1's rows

ROOT = Path(__file__).resolve().parent.parent
# The harness digest covers Domain 2's own files and the Domain 1 code it runs (pyproject's path dependency).
SOURCES = ((ROOT, ("cwabench2/**/*.py", "schemas/*.json")),
           (rundir_mod.HARNESS_ROOT, ("cwabench/**/*.py", "adapters/**/*")))


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def conversations(config: Config) -> dict[str, list[dict]]:
    """Every script this run uses: per family, `sizes[size]` conversations for each turn count, in order."""
    scripts: dict[str, list[dict]] = {}
    for name, family in config.families.items():
        scripts[name] = [generate(name, family.seed, turns, index, config.checkpoint_every, family.parameters)
                         for turns in config.turn_counts for index in range(family.sizes[config.size])]
    return scripts


def write_conversations(run_dir: RunDir, config: Config, scripts: dict[str, list[dict]]) -> None:
    for name, family_scripts in scripts.items():
        family = config.families[name]
        entries = []
        for script in family_scripts:
            output.validate(script)
            entries.append({
                "conversation_id": script["conversation_id"],
                "seed": script["seed"],
                "turn_count": script["turn_count"],
                "probes": len(script["probes"]),
                "task": script["ground_truth"]["task"],
                "user_words": sum(len(t["user"].split()) for t in script["turns"]),
                "script": run_dir.blobs.put_json(script),
            })
        run_dir.write_json(f"conversations/{name}/index.json", {
            "$schema": output.schema_name("conversation-index"),
            "run_id": run_dir.run_id,
            "family": name,
            "generator": {"name": FAMILIES[name].GENERATOR, "version": FAMILIES[name].VERSION},
            "seed": family.seed,
            "size": config.size,
            "turn_counts": config.turn_counts,
            "checkpoint_every": config.checkpoint_every,
            "parameters": dict(sorted(family.parameters.items())),
            "conversations": entries,
        }, f"The {name} scripts this run generated, each a conversation blob")


def adapters_config(config: Config):
    """Domain 1's configuration with only the adapters Domain 2 uses, in its order."""
    d1 = d1_config.load(config.adapters.config)
    missing = [n for n in config.adapters.use if n not in d1.all_adapters]
    if missing:
        raise ConfigError(f"[adapters].use names adapters {config.adapters.config} has no table for: "
                          f"{', '.join(missing)}")
    return replace(d1, adapters={n: d1.all_adapters[n] for n in config.adapters.use})


def manifest(run_dir: RunDir, config: Config, contract: Contract, status: str, finished_at: str | None,
             adapters: dict) -> dict:
    """Domain 1's manifest shape. Domain 2 runs in no container."""
    return {
        "$schema": output.schema_name("manifest"),
        "run_id": run_dir.run_id,
        "status": status,
        "started_at": run_dir.started_at,
        "finished_at": finished_at,
        "command": sys.argv,
        "harness": run_dir.harness(__version__),
        "config": {"path": str(config.path), "sha256": config.sha256, "copy": "config.toml"},
        "contract": {"path": str(contract.path), "pinned": contract.expected_commit, **contract.checkout.as_json(),
                     "spec_draft": contract.spec_draft},
        "adapters": adapters,
        "host": rundir_mod.host(),
        "host_suspended_seconds": run_dir.suspended_seconds(),
        "env_cells": ["baseline"],
        "container": None,
        "seed": None,
        "suites": config.suites,
        "settings": {"timeout_s": config.timeout_s, "concurrency": config.concurrency},
        "ci": None,
    }


def run(config: Config, build: bool = True, log: Callable[[str], None] = _log) -> tuple[Path, str]:
    contract = Contract(config.contract_path, config.contract_commit, config.allow_dirty)
    d1 = adapters_config(config) if set(config.suites) & set(ADAPTER_SUITES) else None
    run_dir = RunDir(config, domain=output.D2, sources=SOURCES)
    log(f"run {run_dir.run_id} → {run_dir.path}")
    describe = "What this run was made from: harness, config, contract, adapters, host"
    run_dir.write_json("contract.json", contract.as_document(run_dir.run_id, output.D2),
                       "Enumerations and texts from the pinned specification")
    ready, unavailable = setup_adapters(d1, build, log) if d1 is not None else ({}, {})
    adapters = adapter_entries(d1, ready, unavailable) if d1 is not None else {}
    run_dir.write_json("manifest.json", manifest(run_dir, config, contract, "running", None, adapters), describe)

    results: list[SuiteResult] = []
    status = "error"
    try:
        scripts = conversations(config)
        write_conversations(run_dir, config, scripts)
        log("conversations: " + ", ".join(f"{name} {len(s)}" for name, s in scripts.items()))
        context = SuiteContext(config, contract, run_dir, scripts, ready, unavailable, log)
        for suite in [s for s in SUITES if s in config.suites]:
            results.append(SUITES[suite].run(context))
            log(f"{suite}: {results[-1].status}")

        findings = [f for result in results for f in result.findings]
        run_dir.write_jsonl("findings.jsonl", findings, "finding", "Every failure in this run, one row per signature")
        status = worst(r.status for r in results) if results else "error"
        run_dir.write_json("summary.json", {
            "$schema": output.schema_name("summary"),
            "run_id": run_dir.run_id,
            "status": status,
            "started_at": run_dir.started_at,
            "finished_at": now(),
            "suites": [{"id": r.id, "title": r.title, "status": r.status, "summary": r.summary_path} for r in results],
            "adapters": [{"adapter": n, "available": e["available"], "error": e["error"]}
                         for n, e in adapters.items()],
            "metrics": [m for r in results for m in r.metrics],
            "findings": {"total": len(findings), "by_suite": {r.id: len(r.findings) for r in results},
                         "file": "findings.jsonl"},
        }, "Headline metrics and per-suite status")
    except BaseException:
        log(traceback.format_exc())
        status = "error"
        raise
    finally:
        run_dir.write_json("manifest.json", manifest(run_dir, config, contract, status, now(), adapters), describe)
        run_dir.finalize(status, [r.id for r in results])
    return run_dir.path, status
