"""coverage.json: which requirements, reason codes and behaviours each adapter was tested on, and passed.

Every requirement and every reason code in the contract is listed, exercised or not, so gaps show as zeros.
"""
from __future__ import annotations

from collections import defaultdict

from . import output
from .contract import Contract
from .suites import Coverage


def _cell() -> dict:
    return {"exercised": 0, "passed": 0}


def build(run_id: str, contract: Contract, adapters: list[str], sources: list[str], observed: list[Coverage]) -> dict:
    requirements = {r["id"]: {a: _cell() for a in adapters} for r in contract.requirements}
    reasons = {r["code"]: {"total": {a: _cell() for a in adapters}, "slots": defaultdict(lambda: {a: _cell() for a in adapters})}
               for r in contract.reasons}
    tags: dict[str, dict] = defaultdict(lambda: {a: _cell() for a in adapters})
    unknown: set[str] = set()

    for item in observed:
        if item.adapter not in adapters:
            continue
        for rule in set(item.rules):
            if rule in requirements:
                cell = requirements[rule][item.adapter]
                cell["exercised"] += 1
                cell["passed"] += item.ok
        seen_codes: set[str] = set()
        for reason, slot in set(item.reasons):
            code = contract.reason_template(reason)
            if code is None:
                unknown.add(reason)
                continue
            cell = reasons[code]["slots"][slot or ""][item.adapter]
            cell["exercised"] += 1
            cell["passed"] += item.ok
            seen_codes.add(code)
        for code in seen_codes:
            cell = reasons[code]["total"][item.adapter]
            cell["exercised"] += 1
            cell["passed"] += item.ok
        for tag in set(item.tags):
            cell = tags[tag][item.adapter]
            cell["exercised"] += 1
            cell["passed"] += item.ok

    return {
        "$schema": output.schema_name("coverage"),
        "run_id": run_id,
        "sources": sources,
        "adapters": adapters,
        "unit": "rows: one per case, snapshot or invocation a suite judged, per adapter",
        "requirements": [
            {"id": r["id"], "scope": contract.scopes.get(r["id"], {}).get("scope"), "by_adapter": requirements[r["id"]]}
            for r in contract.requirements
        ],
        "reasons": [
            {
                "code": r["code"],
                "kind": r["kind"],
                "order": i,
                "by_adapter": reasons[r["code"]]["total"],
                "by_slot": [
                    {"slot": slot or None, "by_adapter": cells}
                    for slot, cells in sorted(reasons[r["code"]]["slots"].items())
                ],
            }
            for i, r in enumerate(contract.reasons)
        ],
        "tags": [{"tag": tag, "by_adapter": tags[tag]} for tag in sorted(tags)],
        "unknown_reasons": sorted(unknown),
    }
