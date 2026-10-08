"""The minimizer (domain-1-plan.md, section 10): delta debugging over a failing snapshot.

It removes batches, then candidates, producer exclusions, conflict groups, placements and route rules, then optional
item fields and variants, then shortens bodies, keeping each reduction only while `test` still fails the same way.
`test` is the caller's: it decides validity (a reduced valid snapshot must stay valid; a mutant must still break its
one check) and re-runs whatever oracle failed, so the minimizer itself knows nothing about assemblers.

Removing a candidate also removes what names it (conflict-group members, capability grants, producer exclusions that
point at it), so most reductions stay valid instead of being thrown away by the validity check.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass, field
from typing import Callable

REQUIRED_ITEM_FIELDS = ("id", "slot", "source", "source_version", "authority", "trust", "freshness", "body")
PROTECTED_PLACEMENTS = ("governance.instructions", "interaction.query", "governance.output_contract")
ROUTE_OPTIONAL = ("clock_skew_seconds", "parser", "requires_evidence", "on_unresolved_instruction", "slots",
                  "default_overrides", "tier_upgrades", "fitting_order", "facts")


@dataclass
class Result:
    document: dict
    tests: int
    reductions: int
    exhausted: bool  # stopped by the test budget rather than by running out of reductions
    before: dict = field(default_factory=dict)
    after: dict = field(default_factory=dict)


def size(document: dict) -> dict:
    items = [i for b in document.get("batches", []) for i in b.get("items", [])]
    return {"batches": len(document.get("batches", [])), "items": len(items),
            "producer_rows": sum(len(b.get("excluded", [])) for b in document.get("batches", [])),
            "conflicts": len(document.get("conflicts", [])),
            "bytes": len(json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))}


class _Budget(Exception):
    pass


class Minimizer:
    def __init__(self, test: Callable[[dict], bool], max_tests: int = 300):
        self.test = test
        self.max_tests = max_tests
        self.tests = 0
        self.reductions = 0
        self._cache: dict[str, bool] = {}

    def holds(self, document: dict) -> bool:
        key = hashlib.sha256(json.dumps(document, sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        if key not in self._cache:
            if self.tests >= self.max_tests:
                raise _Budget
            self.tests += 1
            try:
                self._cache[key] = bool(self.test(document))
            except Exception:  # a reduction the test cannot even evaluate does not reproduce the failure
                self._cache[key] = False
        return self._cache[key]

    def ddmin(self, document: dict, parts: list, remove: Callable[[dict, list], dict]) -> dict:
        """Zeller's ddmin over `parts` (handles into the document): find a 1-minimal set to keep."""
        keep = list(parts)
        n = 2
        while len(keep) >= 1:
            chunk = max(1, len(keep) // n)
            subsets = [keep[i:i + chunk] for i in range(0, len(keep), chunk)]
            reduced = False
            for subset in subsets:
                complement = [p for p in keep if p not in subset]
                candidate = remove(document, [p for p in parts if p not in complement])
                if self.holds(candidate):
                    keep, n, reduced = complement, max(n - 1, 2), True
                    self.reductions += 1
                    break
            if not reduced:
                if n >= len(keep):
                    break
                n = min(len(keep), n * 2)
        return remove(document, [p for p in parts if p not in keep])

    def run(self, document: dict) -> Result:
        before = size(document)
        current = copy.deepcopy(document)
        exhausted = False
        try:
            for step in (self._batches, self._items, self._producer_rows, self._groups, self._placements,
                         self._route, self._fields, self._bodies):
                current = step(current)
        except _Budget:
            exhausted = True
        return Result(current, self.tests, self.reductions, exhausted, before, size(current))

    # Steps ----------------------------------------------------------------------------------------------------------

    def _batches(self, document):
        parts = list(range(len(document["batches"])))
        return self.ddmin(document, parts, lambda d, gone: _drop_items(d, {(b, i) for b in gone
                                                                         for i in range(len(d["batches"][b]["items"]))},
                                                                     drop_batches=set(gone)))

    def _items(self, document):
        parts = [(b, i) for b, batch in enumerate(document["batches"]) for i in range(len(batch["items"]))]
        return self.ddmin(document, parts, lambda d, gone: _drop_items(d, set(gone)))

    def _producer_rows(self, document):
        parts = [(b, r) for b, batch in enumerate(document["batches"]) for r in range(len(batch["excluded"]))]

        def remove(d, gone):
            out = copy.deepcopy(d)
            gone = set(gone)
            for b, batch in enumerate(out["batches"]):
                kept = [row for r, row in enumerate(batch["excluded"]) if (b, r) not in gone]
                removed = {row["item_id"] for r, row in enumerate(batch["excluded"]) if (b, r) in gone}
                batch["excluded"] = kept
                _forget(out, removed - _all_ids(out))
            return out
        return self.ddmin(document, parts, remove)

    def _groups(self, document):
        parts = list(range(len(document["conflicts"])))

        def remove(d, gone):
            out = copy.deepcopy(d)
            out["conflicts"] = [g for i, g in enumerate(out["conflicts"]) if i not in set(gone)]
            return out
        return self.ddmin(document, parts, remove)

    def _placements(self, document):
        parts = [i for i, p in enumerate(document["profile"]["placement"])
                 if isinstance(p, dict) and p.get("slot") not in PROTECTED_PLACEMENTS]

        def remove(d, gone):
            out = copy.deepcopy(d)
            out["profile"]["placement"] = [p for i, p in enumerate(out["profile"]["placement"]) if i not in set(gone)]
            return out
        return self.ddmin(document, parts, remove)

    def _route(self, document):
        route = document["route_policy"]
        parts = [(k,) for k in ROUTE_OPTIONAL if k in route]
        for slot, rules in (route.get("slots") or {}).items():
            parts += [("slots", slot, rule) for rule in rules]
        used = {b["producer"]["id"] for b in document["batches"]}
        parts += [("producers", p) for p in route.get("producers", {}) if p not in used]

        def remove(d, gone):
            out = copy.deepcopy(d)
            r = out["route_policy"]
            for path in gone:
                if path[0] == "slots" and len(path) == 3:
                    (r.get("slots") or {}).get(path[1], {}).pop(path[2], None)
                elif path[0] == "producers":
                    r["producers"].pop(path[1], None)
                else:
                    r.pop(path[0], None)
            if isinstance(r.get("slots"), dict):
                r["slots"] = {s: rules for s, rules in r["slots"].items() if rules}
            return out
        return self.ddmin(document, parts, remove)

    def _fields(self, document):
        parts = []
        for b, batch in enumerate(document["batches"]):
            for i, item in enumerate(batch["items"]):
                if not isinstance(item, dict):
                    continue
                parts += [(b, i, key) for key in item if key not in REQUIRED_ITEM_FIELDS]
                parts += [(b, i, "variants", v) for v in range(len(item.get("variants") or []))
                          if isinstance(item.get("variants"), list)]

        def remove(d, gone):
            out = copy.deepcopy(d)
            variants = {}
            for path in gone:
                if len(path) == 4:
                    variants.setdefault(path[:2], set()).add(path[3])
            for path in gone:
                item = out["batches"][path[0]]["items"][path[1]]
                if len(path) == 3:
                    item.pop(path[2], None)
            for (b, i), drop in variants.items():
                item = out["batches"][b]["items"][i]
                if isinstance(item.get("variants"), list):
                    item["variants"] = [v for k, v in enumerate(item["variants"]) if k not in drop]
            return out
        return self.ddmin(document, parts, remove)

    def _bodies(self, document):
        current = document
        for b, batch in enumerate(document["batches"]):
            for i, item in enumerate(batch["items"]):
                if not isinstance(item, dict) or not isinstance(item.get("body"), str):
                    continue
                for shorter in _shorter(item["body"]):
                    candidate = copy.deepcopy(current)
                    candidate["batches"][b]["items"][i]["body"] = shorter
                    if self.holds(candidate):
                        current = candidate
                        self.reductions += 1
                        break
        return current


def _shorter(body: str) -> list[str]:
    words = body.split(" ")
    out = []
    if len(words) > 2:
        out += [" ".join(words[: len(words) // 2]), words[0]]
    elif len(body) > 8:
        out.append(body[: len(body) // 2])
    return [s for s in out if s.strip() and s != body]


def _all_ids(document) -> set:
    out = set()
    for batch in document["batches"]:
        out |= {i.get("id") for i in batch["items"] if isinstance(i, dict) and isinstance(i.get("id"), str)}
        out |= {row["item_id"] for row in batch["excluded"]}
    return out


def _forget(document: dict, ids: set) -> None:
    """Remove what names ids that no longer exist: group members (and groups left with fewer than two), grants, and
    producer exclusions pointing at them."""
    if not ids:
        return
    groups = []
    for group in document["conflicts"]:
        group["items"] = [i for i in group["items"] if i not in ids]
        if len(group["items"]) >= 2:
            groups.append(group)
    document["conflicts"] = groups
    grant = document.get("capabilities")
    if isinstance(grant, dict):
        grant["allowed_ids"] = [i for i in grant["allowed_ids"] if i not in ids]
    for batch in document["batches"]:
        batch["excluded"] = [row for row in batch["excluded"]
                             if row.get("duplicate_of") not in ids and row.get("superseded_by") not in ids]


def _drop_items(document: dict, gone: set, drop_batches: set | None = None) -> dict:
    out = copy.deepcopy(document)
    for b, batch in enumerate(out["batches"]):
        batch["items"] = [item for i, item in enumerate(batch["items"]) if (b, i) not in gone]
    if drop_batches:
        out["batches"] = [batch for b, batch in enumerate(out["batches"]) if b not in drop_batches]
    removed = {item.get("id") for b, batch in enumerate(document["batches"]) for i, item in enumerate(batch["items"])
               if (b, i) in gone and isinstance(item, dict) and isinstance(item.get("id"), str)}
    _forget(out, removed - _all_ids(out))
    return out
