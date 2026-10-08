"""Suites: each reads a corpus, runs it through the adapters, and writes rows, findings and a summary."""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Callable

from ..adapters import Adapter
from ..config import Config
from ..contract import Contract
from ..rundir import RunDir


@dataclass
class SuiteContext:
    config: Config
    contract: Contract
    run: RunDir
    adapters: dict[str, Adapter]  # set up and usable, in config order
    unavailable: dict[str, str]  # adapter name → why setup failed
    log: Callable[[str], None] = lambda message: print(message, file=sys.stderr, flush=True)
    shared: dict = field(default_factory=dict)  # results one suite computes and a later one reuses


@dataclass
class Coverage:
    """One observation for coverage.json: what a row exercised and whether the adapter got it right."""

    adapter: str
    rules: list[str]
    reasons: list[tuple[str, str | None]]  # (recorded reason, slot or None)
    tags: list[str]
    ok: bool


@dataclass
class SuiteResult:
    id: str
    title: str
    status: str
    summary_path: str
    metrics: list[dict]
    findings: list[dict]
    coverage: list[Coverage] = field(default_factory=list)
