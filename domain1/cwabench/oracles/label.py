"""The label oracle: does an answer record exactly the decisions its snapshot's label states?
(cwabench/corpora/labeled/__init__.py describes labels.)"""
from __future__ import annotations

from collections import defaultdict

from ..adapters import Outcome

REFS = ("duplicate_of", "superseded_by")


def _check(id: str, problems: list[str]) -> dict:
    out = {"oracle": "label", "id": id, "status": "fail" if problems else "pass"}
    if problems:
        out["detail"] = "; ".join(problems[:3]) + (f" (+{len(problems) - 3} more)" if len(problems) > 3 else "")
    return out


def _matches(row: dict, fate: dict) -> bool:
    if row.get("reason") != fate["reason"]:
        return False
    if any(fate.get(ref) is not None and row.get(ref) != fate[ref] for ref in REFS):
        return False
    return "slot" not in fate or row.get("slot") == fate["slot"]


def judge(label: dict, outcome: Outcome) -> list[dict]:
    problems_outcome, problems_fates, problems_conflicts = [], [], []
    trace = outcome.trace if isinstance(outcome.trace, dict) else None

    if outcome.kind != label["outcome"]:
        problems_outcome.append(f"expected {label['outcome']}, got {outcome.kind}")
    elif outcome.refusal_reason != label["refusal"]:
        problems_outcome.append(f"refused with {outcome.refusal_reason}, expected {label['refusal']}")
    if trace is not None and label["outcome"] == outcome.kind:
        recovery = trace.get("recovery")
        action = recovery.get("action") if isinstance(recovery, dict) else None
        if action != label["recovery"]:
            problems_outcome.append(f"recovery.action {action}, expected {label['recovery']}")
    if trace is None:
        detail = ["no trace to compare"]
        return [_check("label:outcome", problems_outcome or detail), _check("label:fates", detail),
                _check("label:conflicts", detail)]

    rows = defaultdict(list)
    for row in trace.get("excluded") or []:
        if isinstance(row, dict) and row.get("stage") == "assembler":
            rows[row.get("item_id")].append(row)
    included = {r.get("item_id") for r in trace.get("included") or [] if isinstance(r, dict)}
    compressed = defaultdict(set)
    for r in trace.get("compressed") or []:
        if isinstance(r, dict):
            compressed[r.get("item_id")].add(r.get("variant_id"))
    assembled = outcome.kind == "assembled"

    for item_id, fates in label["fates"].items():
        fates = fates if isinstance(fates, list) else [fates]
        remaining = list(rows.pop(item_id, []))
        for fate in fates:
            if fate["fate"] == "excluded":
                match = next((r for r in remaining if _matches(r, fate)), None)
                if match is None:
                    got = ", ".join(sorted({str(r.get("reason")) for r in remaining})) or (
                        "included" if item_id in included else "no row")
                    problems_fates.append(f"{item_id!r} should be excluded with {fate['reason']}"
                                          + "".join(f" ({k} {fate[k]})" for k in REFS if fate.get(k))
                                          + f"; got {got}")
                else:
                    remaining.remove(match)
                    if item_id in included and len(fates) == 1:
                        problems_fates.append(f"{item_id!r} is excluded and also included")
            elif assembled and item_id not in included:
                problems_fates.append(f"{item_id!r} should be included")
            elif assembled and fate["fate"] == "compressed" and fate["variant_id"] not in compressed.get(item_id, set()):
                problems_fates.append(f"{item_id!r} should be compressed to {fate['variant_id']}; got "
                                      f"{sorted(compressed.get(item_id, set())) or 'its own body'}")
            elif fate["fate"] == "kept" and assembled and compressed.get(item_id):
                problems_fates.append(f"{item_id!r} should keep its own body; compressed to "
                                      f"{sorted(compressed[item_id])}")
        for row in remaining:
            problems_fates.append(f"{item_id!r} unexpectedly excluded with {row.get('reason')}")
    for item_id, extra in rows.items():
        problems_fates.append(f"{item_id!r} is not in the label but was excluded with "
                              f"{', '.join(str(r.get('reason')) for r in extra)}")

    records = {c.get("group_id"): c for c in trace.get("conflicts") or [] if isinstance(c, dict)}
    for group_id, expected in label["conflicts"].items():
        record = records.get(group_id)
        if record is None:
            problems_conflicts.append(f"no record for group {group_id!r}")
            continue
        actual = {k: record.get(k) for k in ("decided_by", "resolution", "winner")}
        if actual != expected:
            problems_conflicts.append(f"group {group_id!r}: {actual}, expected {expected}")

    return [_check("label:outcome", problems_outcome), _check("label:fates", problems_fates),
            _check("label:conflicts", problems_conflicts)]
