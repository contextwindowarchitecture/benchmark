"""Headline metrics (domain-2-plan.md, 10): Domain 1's shape with `arm`, `family` and `tier` where Domain 1 has
`adapter`. Every metric carries its value, numerator, denominator, target and status; most are measurements (`info`),
and the gates are the assembly gate's rates and the self-check."""
from __future__ import annotations

UNITS = ("rate", "count", "tokens", "ms", "points")


def _metric(id, label, unit, value, numerator, denominator, target, status, suite, arm, family, tier, description):
    return {"id": id, "label": label, "suite": suite, "arm": arm, "family": family, "tier": tier, "unit": unit,
            "value": value, "numerator": numerator, "denominator": denominator, "target": target, "status": status,
            "description": description}


def rate(id: str, label: str, numerator: int, denominator: int, *, target: float | None = 1.0,
         suite: str | None = None, arm: str | None = None, family: str | None = None, tier: str | None = None,
         description: str = "") -> dict:
    """A fraction in 0–1. With no denominator the value is null and the status "na"; with no target, "info"."""
    value = None if denominator == 0 else numerator / denominator
    if value is None:
        status = "na"
    elif target is None:
        status = "info"
    else:
        status = "pass" if value >= target else "fail"
    return _metric(id, label, "rate", value, numerator, denominator, target, status, suite, arm, family, tier,
                   description)


def count(id: str, label: str, value: int, *, maximum: int | None = None, suite: str | None = None,
          arm: str | None = None, family: str | None = None, tier: str | None = None, description: str = "") -> dict:
    """A count, passing when at most `maximum`, or informational without one."""
    status = "info" if maximum is None else ("pass" if value <= maximum else "fail")
    return _metric(id, label, "count", value, None, None, maximum, status, suite, arm, family, tier, description)
