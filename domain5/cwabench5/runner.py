"""A whole run: generate the task instances, list the attack templates, run the suites, and write the run directory
(domain-5-plan.md, 11).

The run directory is Domain 1's (cwabench.rundir) under Domain 5's prefix: index.json, manifest.json, config.toml,
contract.json, summary.json, findings.jsonl, suites/<id>/, blobs/, plus tasks/<family>/index.json, which lists each
instance this run generated with the digest of its blob, and attacks/index.json, the templates probes are composed
from.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import Callable

from cwabench import rundir as rundir_mod
from cwabench.contract import Contract
from cwabench.rundir import RunDir, now, worst

from . import __version__, attacks, output, tasks
from .attacks.families import APPLIES, BODIES, OPENERS
from .config import Config
from .suites import SuiteContext, SuiteResult, s0_selfcheck

SUITES = {"S0": s0_selfcheck}

ROOT = Path(__file__).resolve().parent.parent
D2_ROOT = ROOT.parent / "domain2"
# The harness digest covers Domain 5's own files and the Domain 1 and 2 code it runs (pyproject's path dependencies).
SOURCES = ((ROOT, ("cwabench5/**/*.py", "schemas/*.json")), (D2_ROOT, ("cwabench2/**/*.py",)),
           (rundir_mod.HARNESS_ROOT, ("cwabench/**/*.py", "adapters/**/*")))


def _log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def instances(config: Config) -> dict[str, list[dict]]:
    """Every task instance this run uses: per family, `sizes[size]` plain ones, then as many imperative ones."""
    return {name: [tasks.generate(name, family.seed, variant, index, family.parameters)
                   for variant in tasks.VARIANTS for index in range(family.sizes[config.size])]
            for name, family in config.tasks.items()}


def write_tasks(run_dir: RunDir, config: Config, found: dict[str, list[dict]]) -> None:
    for name, family_tasks in found.items():
        family = config.tasks[name]
        entries = []
        for task in family_tasks:
            output.validate(task)
            entries.append({"task_id": task["task_id"], "variant": task["variant"], "seed": task["seed"],
                            "contract": task["contract"], "items": len(task["items"]),
                            "words": sum(len(i["text"].split()) for i in task["items"]),
                            "task": run_dir.blobs.put_json(task)})
        module = tasks.FAMILIES[name]
        run_dir.write_json(f"tasks/{name}/index.json", {
            "$schema": output.schema_name("task-index"), "run_id": run_dir.run_id, "family": name,
            "generator": {"name": module.GENERATOR, "version": module.VERSION}, "seed": family.seed,
            "size": config.size, "parameters": dict(sorted(family.parameters.items())), "tasks": entries,
        }, f"The {name} task instances this run generated, each a task blob")


def write_attacks(run_dir: RunDir, config: Config) -> None:
    templates = [{"template_id": t.template_id, "family": t.family, "split": t.split, "opener": t.opener,
                  "body": t.body, "form": t.form,
                  "applies_to": list(APPLIES[t.family]) if APPLIES[t.family] else None,
                  "text": OPENERS[t.opener].replace("{body}", BODIES[t.family][t.body]).replace(
                      "{Body}", BODIES[t.family][t.body][:1].upper() + BODIES[t.family][t.body][1:])}
                 for t in attacks.every_template() if t.family in config.attacks.families]
    run_dir.write_json("attacks/index.json", {
        "$schema": output.schema_name("attack-index"), "run_id": run_dir.run_id, "seed": config.attacks.seed,
        "chat_template": config.attacks.chat_template, "families": config.attacks.families, "templates": templates,
    }, "The attack templates this run composes probes from")


def manifest(run_dir: RunDir, config: Config, contract: Contract, status: str, finished_at: str | None) -> dict:
    """Domain 1's manifest shape. Domain 5 runs in no container."""
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
        "adapters": {},
        "host": rundir_mod.host(),
        "host_suspended_seconds": run_dir.suspended_seconds(),
        "env_cells": ["baseline"],
        "container": None,
        "seed": None,
        "suites": config.suites,
        "settings": {"timeout_s": config.timeout_s, "concurrency": config.concurrency},
        "ci": None,
    }


def run(config: Config, log: Callable[[str], None] = _log) -> tuple[Path, str]:
    contract = Contract(config.contract_path, config.contract_commit, config.allow_dirty)
    run_dir = RunDir(config, domain=output.D5, sources=SOURCES)
    log(f"run {run_dir.run_id} → {run_dir.path}")
    describe = "What this run was made from: harness, config, contract, host"
    run_dir.write_json("contract.json", contract.as_document(run_dir.run_id, output.D5),
                       "Enumerations and texts from the pinned specification")
    run_dir.write_json("manifest.json", manifest(run_dir, config, contract, "running", None), describe)

    results: list[SuiteResult] = []
    status = "error"
    try:
        found = instances(config)
        write_tasks(run_dir, config, found)
        write_attacks(run_dir, config)
        log("tasks: " + ", ".join(f"{name} {len(t)}" for name, t in found.items()))
        context = SuiteContext(config, contract, run_dir, found, log)
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
            "adapters": [],
            "metrics": [m for r in results for m in r.metrics],
            "findings": {"total": len(findings), "by_suite": {r.id: len(r.findings) for r in results},
                         "file": "findings.jsonl"},
        }, "Headline metrics and per-suite status")
    except BaseException:
        log(traceback.format_exc())
        status = "error"
        raise
    finally:
        run_dir.write_json("manifest.json", manifest(run_dir, config, contract, status, now()), describe)
        run_dir.finalize(status, [r.id for r in results])
    return run_dir.path, status
