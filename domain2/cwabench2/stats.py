"""Intervals for S2's rates (domain-2-plan.md, 9): a seeded cluster bootstrap over conversations.

Probes and samples of one conversation are not independent (they share its history), so the unit resampled is the
conversation: each resample draws conversations with replacement and pools all their observations. The interval is
the percentile interval of the resampled means. A paired comparison resamples the per-observation differences
between two arms on the same probes and samples, so every difference pairs equal inputs.
"""
from __future__ import annotations

import random

LEVEL = 0.95


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def bootstrap(clusters: dict[str, list[float]], resamples: int, seed: int, level: float = LEVEL) -> dict | None:
    """{"low", "high", "level", "method", "resamples"} for the pooled mean, or None with no observations."""
    names = sorted(k for k, v in clusters.items() if v)
    if not names:
        return None
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        total = count = 0
        for name in rng.choices(names, k=len(names)):
            total += sum(clusters[name])
            count += len(clusters[name])
        means.append(total / count)
    means.sort()
    tail = (1 - level) / 2
    low = means[max(0, int(tail * resamples))]
    high = means[min(resamples - 1, int((1 - tail) * resamples) - 1)]
    return {"low": round(low, 6), "high": round(high, 6), "level": level, "method": "cluster-bootstrap/conversation",
            "resamples": resamples}
