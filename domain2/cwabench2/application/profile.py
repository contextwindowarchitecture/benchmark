"""The CWA arms' profile and route policy (domain-2-plan.md, 7).

The profile takes its placement from the specification's published chat profile, `policy-first-chat`
(examples/profiles.json), checked against the pinned checkout on every run, so the arms render the way the spec's own
chat example does: instructions as the system message, then state, memory, history, the output contract and the
query inside the one user message. It is named for this benchmark, because its route policy is the benchmark's: one
producer per kind the arms use, the graders as a downstream parser (`parser: true`, so the output contract is never
omitted, R-4), the task in state's required scope and the turn prefix on memory (R-9). A route that changes a rule is
a new route policy version, and so a new profile (R-20): the pipeline arm's opts history into supersession and exact
deduplication (R-24, R-25).
"""
from __future__ import annotations

import copy
import json

SPEC_PROFILE = "policy-first-chat"
ROUTE = "cwa-bench-chat"

PRODUCERS = {
    "policy-registry": {"kind": "policy", "slots": ["governance.instructions", "governance.output_contract"]},
    "conversation": {"kind": "interaction", "slots": ["interaction.history", "interaction.query"]},
    "state-svc": {"kind": "state", "slots": ["state.task"]},
    "memory-svc": {"kind": "memory", "slots": ["interaction.memory"]},
}


class ProfileError(Exception):
    pass


def spec_placement(contract) -> list[dict]:
    """The placement of the spec's published chat profile, from the pinned checkout."""
    profiles = json.loads((contract.path / "examples" / "profiles.json").read_text(encoding="utf-8"))
    found = [p for p in profiles if p.get("id") == SPEC_PROFILE]
    if len(found) != 1:
        raise ProfileError(f"examples/profiles.json no longer has exactly one {SPEC_PROFILE} profile")
    return copy.deepcopy(found[0]["placement"])


def route_policy(pipeline: bool) -> dict:
    slots = {
        "state.task": {"required_scope": ["tenant", "task"]},
        "interaction.memory": {"source_prefix": "turn:", "required_scope": ["tenant", "user"]},
    }
    if pipeline:
        slots["interaction.history"] = {"supersede": "source", "dedupe": "exact"}
    return {
        "route": ROUTE,
        "version": "cwa-bench-d2/pipeline/v1" if pipeline else "cwa-bench-d2/v1",
        "clock_skew_seconds": 5,
        "parser": True,
        "producers": copy.deepcopy(PRODUCERS),
        "slots": slots,
    }


def profile(contract, route: dict) -> dict:
    return {
        "spec": "cwa/draft",
        "id": f"{ROUTE}-pipeline" if "interaction.history" in route["slots"] else ROUTE,
        "version": 1,
        "route": route["route"],
        "model_family": None,
        "route_policy_version": route["version"],
        "placement": spec_placement(contract),
        "evaluation": {"status": "unevaluated", "suite": None, "date": None, "result": None, "artifact": None},
    }


def check(contract) -> list[str]:
    """Problems with the arms' profiles and route policies against the pinned spec: its schemas, and the slots the
    placement must hold (R-4, R-20). Empty when they pass."""
    problems = []
    placed = {p["slot"] for p in spec_placement(contract)}
    for pipeline in (False, True):
        route = route_policy(pipeline)
        for name, document in (("route_policy.schema.json", route), ("profile.schema.json", profile(contract, route))):
            errors = list(contract.validator(name).iter_errors(document))
            if errors:
                problems.append(f"{name}: {errors[0].message[:200]}")
        used = {slot for producer in route["producers"].values() for slot in producer["slots"]}
        missing = sorted(used - placed)
        if missing:
            problems.append(f"the placement does not place {', '.join(missing)}")
    return problems
