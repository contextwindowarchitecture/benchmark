"""The differential oracle (domain-1-plan.md, 7.3): every adapter given the same snapshot must agree on the outcome,
the payload bytes and the normalized trace. Comparison is staged so a disagreement names where it starts."""
from __future__ import annotations

import hashlib
from collections import defaultdict
from itertools import combinations

from ..canon import jcs
from .. import traces


def signature(outcome_kind: str, payload: bytes | None, trace: dict | None) -> dict:
    """What two adapters must share to agree, in comparison order."""
    normalized = None
    if isinstance(trace, dict):
        try:
            normalized = hashlib.sha256(jcs.serialize_bytes(traces.normalize(trace))).hexdigest()
        except jcs.CanonicalizationError:
            normalized = "uncanonicalizable"
    return {
        "outcome": outcome_kind,
        "payload": hashlib.sha256(payload).hexdigest() if payload is not None else None,
        "trace": normalized,
    }


def first_difference(a: dict, b: dict) -> str | None:
    for stage in ("outcome", "payload", "trace"):
        if a[stage] != b[stage]:
            return stage
    return None


def compare(signatures: dict[str, dict]) -> dict:
    """signatures: adapter → signature, for one snapshot. Returns agreement, the groups of agreeing adapters, and the
    first differing stage between each pair."""
    groups: dict[tuple, list[str]] = defaultdict(list)
    for adapter, sig in signatures.items():
        groups[(sig["outcome"], sig["payload"], sig["trace"])].append(adapter)
    pairs = {}
    for a, b in combinations(sorted(signatures), 2):
        pairs[(a, b)] = first_difference(signatures[a], signatures[b])
    return {"agree": len(groups) <= 1, "groups": sorted(groups.values(), key=len, reverse=True), "pairs": pairs}
