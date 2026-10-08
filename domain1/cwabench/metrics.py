"""Headline metrics: every one carries value, numerator, denominator, target and status (domain-1-plan.md, 12.5)."""
from __future__ import annotations


def rate(
    id: str,
    label: str,
    numerator: int,
    denominator: int,
    *,
    target: float | None = 1.0,
    suite: str | None = None,
    adapter: str | None = None,
    description: str = "",
) -> dict:
    """A fraction in 0–1. With no denominator the value is null and the status "na"; with no target, "info"."""
    value = None if denominator == 0 else numerator / denominator
    if value is None:
        status = "na"
    elif target is None:
        status = "info"
    else:
        status = "pass" if value >= target else "fail"
    return {
        "id": id,
        "label": label,
        "suite": suite,
        "adapter": adapter,
        "unit": "rate",
        "value": value,
        "numerator": numerator,
        "denominator": denominator,
        "target": target,
        "status": status,
        "description": description,
    }


def count(
    id: str,
    label: str,
    value: int,
    *,
    maximum: int | None = None,
    suite: str | None = None,
    adapter: str | None = None,
    description: str = "",
) -> dict:
    """A count, passing when at most `maximum`, or informational without one."""
    status = "info" if maximum is None else ("pass" if value <= maximum else "fail")
    return {
        "id": id,
        "label": label,
        "suite": suite,
        "adapter": adapter,
        "unit": "count",
        "value": value,
        "numerator": None,
        "denominator": None,
        "target": maximum,
        "status": status,
        "description": description,
    }


def value(
    id: str,
    label: str,
    value: float,
    unit: str,
    *,
    suite: str | None = None,
    adapter: str | None = None,
    description: str = "",
) -> dict:
    """A measured quantity with no target: milliseconds or an exponent. Always informational."""
    return {
        "id": id,
        "label": label,
        "suite": suite,
        "adapter": adapter,
        "unit": unit,
        "value": value,
        "numerator": None,
        "denominator": None,
        "target": None,
        "status": "info",
        "description": description,
    }


def percentiles(values: list[float]) -> dict:
    """p50, p95 and max by nearest rank, in the unit given, plus the total; empty input gives nulls."""
    if not values:
        return {"count": 0, "p50": None, "p95": None, "max": None, "total": None}
    ordered = sorted(values)

    def rank(p: float) -> float:
        index = max(0, min(len(ordered) - 1, -(-len(ordered) * p // 100) - 1))
        return round(ordered[int(index)], 3)

    return {"count": len(ordered), "p50": rank(50), "p95": rank(95), "max": round(ordered[-1], 3), "total": round(sum(ordered), 3)}
