"""Suites (domain-2-plan.md, 8): each reads the run's conversations and writes rows, findings and a summary."""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field
from typing import Callable

from cwabench.contract import Contract
from cwabench.rundir import RunDir

from .. import output
from ..config import Config


@dataclass
class SuiteContext:
    config: Config
    contract: Contract
    run: RunDir
    conversations: dict[str, list[dict]]  # family → its scripts in this run, in order
    adapters: dict = field(default_factory=dict)  # Domain 1's adapters, set up and usable, by name
    unavailable: dict[str, str] = field(default_factory=dict)  # adapter name → why setup failed
    log: Callable[[str], None] = lambda message: print(message, file=sys.stderr, flush=True)
    shared: dict = field(default_factory=dict)  # results one suite computes and a later one reuses


@dataclass
class SuiteResult:
    id: str
    title: str
    status: str
    summary_path: str
    metrics: list[dict]
    findings: list[dict]


def produced(ctx: SuiteContext, script: dict):
    """The model-based producers' outputs for a script (application/producers.py), or None when none ran."""
    return ctx.shared.get("produced", {}).get(script["conversation_id"])


def finding(ctx: SuiteContext, suite: str, signature: dict, *, case_id: str, oracle: str, checks: list[str],
            summary: str, severity: str = "error", requirements: list[str] | None = None) -> dict:
    """A finding row in Domain 1's shape: its id is a digest of what makes two failures the same."""
    return {
        "$schema": output.schema_name("finding"),
        "finding_id": hashlib.sha256(output.dumps(signature, compact=True).encode()).hexdigest()[:12],
        "run_id": ctx.run.run_id,
        "suite": suite,
        "adapter": None,
        "adapters": [],
        "case_id": case_id,
        "oracle": oracle,
        "checks": checks,
        "severity": severity,
        "summary": summary,
        "first_pointer": None,
        "requirements": requirements or [],
        "occurrences": 1,
        "reproducer": None,
        "signature": signature,
        "minimized": None,
    }
