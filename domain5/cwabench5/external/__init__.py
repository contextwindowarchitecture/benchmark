"""The external sets (domain-5-plan.md, 6.6): SEP and LLMail-Inject, vendored under `external/` (vendor.py), as
task-shaped probes every arm can build.

An external probe is a task dict like a generated instance's (instructions, output contract, question, items), so the
CWA snapshot (application/snapshot.py) and every baseline (baselines.py) are built from it unchanged; its `external`
field holds what S3 grades by and the attack's text and item, for the attack-in-payload oracle. A run uses each
set's pilot cases at pilot size and all of them at recorded size.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import llmail, sep

SETS = ("sep", "llmail")
KINDS = SETS  # a cell's kind is its set (grid.py)

__all__ = ["KINDS", "SETS", "load", "tasks"]


def load(directory: Path, name: str) -> list[dict]:
    file = {"sep": "cases.json", "llmail": "submissions.json"}[name]
    return json.loads((directory / name / file).read_text(encoding="utf-8"))


def tasks(directory: Path, sets: list[str], size: str) -> dict[str, list[dict]]:
    """Each set's probes for a run of `size`, in the vendored order: SEP's case by case, instruction then data."""
    found: dict[str, list[dict]] = {}
    for name in sets:
        cases = [c for c in load(directory, name) if size == "recorded" or c["pilot"]]
        if name == "sep":
            found[name] = [sep.task(case, placement) for case in cases for placement in sep.PLACEMENTS]
        else:
            scenarios = json.loads((directory / "llmail" / "scenarios.json").read_text(encoding="utf-8"))
            found[name] = [llmail.task(case, scenarios) for case in cases]
    return found
