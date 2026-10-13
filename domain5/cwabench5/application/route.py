"""The CWA arms' route policy and profiles (domain-5-plan.md, 7).

**The route** is named for this benchmark, because its producers are the benchmark's: a policy registry for the
instructions and the output contract, the conversation for the query, a content index (kind `retrieval`) for
retrieved content as `evidence.knowledge`, and a content tool (kind `mcp`) for content returned as
`evidence.tool_results`. The graders are a downstream parser (`parser: true`, so the output contract is never omitted,
R-4), and a route that retrieves content requires it (`requires_evidence`). The slot rules are the specification's
`support-chat` route's for its two evidence slots (examples/route-policies.json), read from the pinned checkout: a
minimum relevance, an age limit, the tenant scope, exact deduplication and three items a source for knowledge, and
supersession by source for tool results. The content tool is also listed for `governance.capabilities`, so a tool
specification it sends there is excluded as `capability_not_allowed` (R-15), which S1's field claims test. S5's
route, `cwa-bench-d5-pasted`, is the same without `requires_evidence`: there the user pastes the content into the
query, and nothing is retrieved.

**The profiles** are the specification's, as published, under this route: `policy-first-chat` for `cwa`,
`cwa-boundary` and `cwa-observation` (instructions as the system message; evidence, observations, the output contract
and the query inside the one user message), and `document-analysis` for `cwa-reinforced` (the same, with the
instructions placed again after the evidence).
"""
from __future__ import annotations

import copy
import json

ROUTE = "cwa-bench-d5"
VERSION = "cwa-bench-d5/v1"
PASTED_ROUTE = "cwa-bench-d5-pasted"  # S5: the user pastes the content; nothing is retrieved
SPEC_ROUTE = "support-chat"
SPEC_PROFILES = {"chat": "policy-first-chat", "document": "document-analysis"}
PROFILE_OF = {"cwa": "chat", "cwa-boundary": "chat", "cwa-observation": "chat", "cwa-reinforced": "document"}
PRODUCERS = {
    "policy-registry": {"kind": "policy", "slots": ["governance.instructions", "governance.output_contract"]},
    "conversation": {"kind": "interaction", "slots": ["interaction.query"]},
    "content-index": {"kind": "retrieval", "slots": ["evidence.knowledge"]},
    "content-tool": {"kind": "mcp", "slots": ["evidence.tool_results", "governance.capabilities"]},
}


class ProfileError(Exception):
    pass


def _spec(contract, name: str, key: str, value: str) -> dict:
    documents = json.loads((contract.path / "examples" / name).read_text(encoding="utf-8"))
    found = [d for d in documents if d.get(key) == value]
    if len(found) != 1:
        raise ProfileError(f"examples/{name} no longer has exactly one {value}")
    return copy.deepcopy(found[0])


def route_policy(contract, pasted: bool = False) -> dict:
    """The route; with `pasted`, S5's: the same producers and rules, but no evidence required, since the content
    arrives in the user's own message."""
    spec = _spec(contract, "route-policies.json", "route", SPEC_ROUTE)
    return {
        "route": PASTED_ROUTE if pasted else ROUTE,
        "version": f"{PASTED_ROUTE}/v1" if pasted else VERSION,
        "clock_skew_seconds": 5,
        "parser": True,
        "requires_evidence": not pasted,
        "producers": copy.deepcopy(PRODUCERS),
        "slots": {slot: spec["slots"][slot] for slot in ("evidence.knowledge", "evidence.tool_results")},
    }


def profile(contract, which: str, pasted: bool = False) -> dict:
    placed = _spec(contract, "profiles.json", "id", SPEC_PROFILES[which])["placement"]
    route = PASTED_ROUTE if pasted else ROUTE
    return {
        "spec": "cwa/draft",
        "id": f"{route}-{which}",
        "version": 1,
        "route": route,
        "model_family": None,
        "route_policy_version": f"{route}/v1",
        "placement": placed,
        "evaluation": {"status": "unevaluated", "suite": None, "date": None, "result": None, "artifact": None},
    }


def check(contract) -> list[str]:
    """Problems with the route and profiles against the pinned spec; empty when they pass."""
    try:
        route = route_policy(contract)
        documents = [("route_policy.schema.json", route), ("route_policy.schema.json", route_policy(contract, True))]
        documents += [("profile.schema.json", profile(contract, which, pasted)) for which in SPEC_PROFILES
                      for pasted in (False, True)]
    except (ProfileError, KeyError) as error:
        return [str(error)]
    problems = []
    for name, document in documents:
        errors = list(contract.validator(name).iter_errors(document))
        if errors:
            problems.append(f"{name}: {errors[0].message[:200]}")
    for arm, which in PROFILE_OF.items():
        content = "evidence.tool_results" if arm == "cwa-observation" else "evidence.knowledge"
        used = {"governance.instructions", "governance.output_contract", "interaction.query", content}
        missing = sorted(used - {p["slot"] for p in profile(contract, which)["placement"]})
        if missing:
            problems.append(f"{arm}'s profile does not place {', '.join(missing)}")
    return problems
