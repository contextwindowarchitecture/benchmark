"""Comparing traces the way conformance/README.md (Running a case, step 4) compares them."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass


def normalize(trace: dict) -> dict:
    """The trace without the fields that may differ between runs: trace_id, timings and recovery.detail (R-23)."""
    out = copy.deepcopy(trace)
    out.pop("trace_id", None)
    out.pop("timings", None)
    recovery = out.get("recovery")
    if isinstance(recovery, dict):
        recovery.pop("detail", None)
    return out


def _escape(token: str) -> str:
    return token.replace("~", "~0").replace("/", "~1")


def _kind(value) -> str:
    # bool before int: in Python True == 1, but JSON true is not the number 1.
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


@dataclass(frozen=True)
class Difference:
    pointer: str  # RFC 6901 JSON Pointer
    expected: object
    actual: object
    missing: str | None = None  # "expected" or "actual" when the member exists on one side only

    def as_json(self, limit: int = 400) -> dict:
        def clip(value):
            text = json.dumps(value, ensure_ascii=False)
            return value if len(text) <= limit else text[:limit] + "…"

        out = {"pointer": self.pointer}
        if self.missing != "expected":
            out["expected"] = clip(self.expected)
        if self.missing != "actual":
            out["actual"] = clip(self.actual)
        if self.missing:
            out["missing_in"] = self.missing
        return out


def diff(expected, actual, pointer: str = "", limit: int = 50) -> list[Difference]:
    """Every place two JSON values differ, type-strictly, up to `limit` differences, in document order."""
    found: list[Difference] = []

    def walk(a, b, at: str) -> None:
        if len(found) >= limit:
            return
        if _kind(a) != _kind(b):
            found.append(Difference(at, a, b))
        elif isinstance(a, dict):
            for key in a:
                if key not in b:
                    found.append(Difference(f"{at}/{_escape(key)}", a[key], None, missing="actual"))
                else:
                    walk(a[key], b[key], f"{at}/{_escape(key)}")
            for key in b:
                if key not in a:
                    found.append(Difference(f"{at}/{_escape(key)}", None, b[key], missing="expected"))
        elif isinstance(a, list):
            for i, (x, y) in enumerate(zip(a, b)):
                walk(x, y, f"{at}/{i}")
            for i in range(len(b), len(a)):
                found.append(Difference(f"{at}/{i}", a[i], None, missing="actual"))
            for i in range(len(a), len(b)):
                found.append(Difference(f"{at}/{i}", None, b[i], missing="expected"))
        elif a != b:
            found.append(Difference(at, a, b))

    walk(expected, actual, pointer)
    return found[:limit]


def charged_tokens(trace: dict) -> int | None:
    """The count the fit test compares with budget.input: tokens × (100 + margin) / 100, rounded up."""
    result = trace.get("result")
    budget = trace.get("budget")
    if not isinstance(result, dict) or not isinstance(budget, dict):
        return None
    tokens = result.get("input_tokens")
    margin = budget.get("margin_percent", 0)
    if not isinstance(tokens, int) or isinstance(tokens, bool) or not isinstance(margin, int):
        return None
    return (tokens * (100 + margin) + 99) // 100


def coverage_tags(trace: dict | None, snapshot: dict | None) -> list[str]:
    """What a trace exercises, as stable tags: reasons with their slots, refusals, conflict decisions, components."""
    tags: set[str] = set()
    if isinstance(snapshot, dict):
        for kind in ("tokenizer", "renderer"):
            if isinstance(snapshot.get(kind), str):
                tags.add(f"{kind}:{snapshot[kind]}")
    if not isinstance(trace, dict):
        return sorted(tags)
    for row in trace.get("excluded") or []:
        if isinstance(row, dict) and isinstance(row.get("reason"), str):
            tags.add(f"reason:{row['reason']}")
            tags.add(f"reason:{row['reason']}@{row.get('slot') or '-'}")
            tags.add(f"stage:{row.get('stage')}")
    refused = trace.get("refused")
    if isinstance(refused, dict) and refused.get("bool") and isinstance(refused.get("reason"), str):
        tags.add(f"refusal:{refused['reason']}")
    recovery = trace.get("recovery")
    if isinstance(recovery, dict) and isinstance(recovery.get("action"), str):
        tags.add(f"recovery:{recovery['action']}")
    for group in trace.get("conflicts") or []:
        if isinstance(group, dict):
            tags.add(f"conflict:{group.get('kind')}:{group.get('decided_by')}")
    for row in trace.get("included") or []:
        if isinstance(row, dict) and isinstance(row.get("slot"), str):
            tags.add(f"included:{row['slot']}")
    if trace.get("compressed"):
        tags.add("fit:compressed")
    if any(isinstance(r, dict) and r.get("reason") == "over_budget" for r in trace.get("excluded") or []):
        tags.add("fit:omitted")
    if trace.get("defaults_filled"):
        tags.add("defaults_filled")
    return sorted(tags)
