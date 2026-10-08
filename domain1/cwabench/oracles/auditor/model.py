"""What the auditor knows about a snapshot: its candidates under the ids the trace records them by, each item's
effective tier and filled policy fields, and the route's rules. Nothing here assembles."""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from fractions import Fraction
from functools import cached_property

from ...canon import instants, jcs
from ...canon.payloads import render_body, stream_of
from ...canon.strings import usable_id, utf16_key
from ...contract import Contract

SLOTS = (
    "governance.instructions", "governance.capabilities", "governance.examples", "governance.output_contract",
    "state.user", "state.task", "evidence.knowledge", "evidence.tool_results",
    "interaction.memory", "interaction.history", "interaction.query",
)
AUTHORITIES = ("governing", "user", "state", "reference_only", "observation", "generated", "untrusted")
TIERS = ("droppable", "compressible", "protected")  # ascending protection
POLICY_FIELDS = ("token_budget", "variants", "conflict_policy", "lineage", "eligibility", "injection_risk")  # R-3 order
EVIDENCE_SLOTS = ("evidence.knowledge", "evidence.tool_results")

# R-1: the authority each slot takes, and the slots that may carry untrusted instead.
SLOT_AUTHORITY = {
    **{s: {"governing"} for s in SLOTS if s.startswith("governance.")},
    "state.user": {"state"}, "state.task": {"state"},
    "evidence.knowledge": {"reference_only"},
    "evidence.tool_results": {"observation", "untrusted"},
    "interaction.memory": {"generated", "untrusted"},
    "interaction.history": {"user", "untrusted"},
    "interaction.query": {"user"},
}

# Pipeline stages of assembler exclusion rows, in the order the trace lists them (conformance/README.md).
STAGE_OF_REASON = {
    "conflict_deferred": 1, "conflict_lost": 1, "superseded": 2, "duplicate_content": 3,
    "source_diversity_cap": 4, "over_budget": 5,
}  # every other assembler reason is an admission reason, stage 0
STAGE_NAMES = ("admission", "conflict", "supersede", "dedupe", "diversity", "fit")


def stage_of(reason: str) -> int:
    return STAGE_OF_REASON.get(reason, 0)


@dataclass
class Candidate:
    producer: str
    producer_kind: object
    position: int  # in its batch, as supplied
    item: object  # the batch entry as JSON, whatever it is
    recorded_id: str  # the id the trace records it by: its id, or {producer}#invalid-{n} (R-2)

    @property
    def is_object(self) -> bool:
        return isinstance(self.item, dict)

    def get(self, key, default=None):
        return self.item.get(key, default) if isinstance(self.item, dict) else default

    @property
    def slot(self) -> str | None:
        slot = self.get("slot")
        return slot if isinstance(slot, str) and slot in SLOTS else None

    @cached_property
    def canonical(self) -> bytes:
        return jcs.serialize_bytes(self.item)


@dataclass
class View:
    contract: Contract
    snapshot: dict
    candidates: list[Candidate] = field(default_factory=list)
    producer_rows: list[tuple[str, dict]] = field(default_factory=list)  # (producer, row) as reported

    @classmethod
    def build(cls, contract: Contract, snapshot: dict) -> "View":
        view = cls(contract, snapshot)
        for batch in snapshot["batches"]:
            producer = batch["producer"]["id"]
            invalid = 0
            for position, item in enumerate(batch["items"]):
                if isinstance(item, dict) and usable_id(item.get("id")):
                    recorded = item["id"]
                else:
                    recorded = f"{producer}#invalid-{invalid}"
                    invalid += 1
                view.candidates.append(Candidate(producer, batch["producer"].get("kind"), position, item, recorded))
            for row in batch["excluded"]:
                view.producer_rows.append((producer, row))
        return view

    # Snapshot parts ----------------------------------------------------------------------------------------------

    @property
    def route(self) -> dict:
        return self.snapshot["route_policy"]

    @property
    def profile(self) -> dict:
        return self.snapshot["profile"]

    @cached_property
    def assembly_time(self) -> Fraction:
        return instants.parse(self.snapshot["assembly_time"])

    @property
    def clock_skew(self) -> int:
        return self.route.get("clock_skew_seconds", 0)

    def rules(self, slot: str) -> dict:
        return (self.route.get("slots") or {}).get(slot) or {}

    @cached_property
    def placements(self) -> list[dict]:
        return self.profile["placement"]

    @cached_property
    def placed_slots(self) -> set[str]:
        return {p["slot"] for p in self.placements}

    def streams_of(self, slot: str) -> list[str]:
        """The streams a slot's items render into, once per placement."""
        return [stream_of(p["wrap"])[0] for p in self.placements if p["slot"] == slot]

    @cached_property
    def by_id(self) -> dict[str, list[Candidate]]:
        out: dict[str, list[Candidate]] = defaultdict(list)
        for candidate in self.candidates:
            out[candidate.recorded_id].append(candidate)
        return out

    def unique(self, recorded_id: str) -> Candidate | None:
        found = self.by_id.get(recorded_id, [])
        return found[0] if len(found) == 1 else None

    @cached_property
    def producer_exclusion_ids(self) -> set[str]:
        return {row.get("item_id") for _, row in self.producer_rows}

    def admitted_producer(self, candidate: Candidate) -> bool:
        listed = (self.route.get("producers") or {}).get(candidate.producer)
        return isinstance(listed, dict) and listed.get("kind") == candidate.producer_kind

    @cached_property
    def item_validator(self):
        return self.contract.validator("context_item.schema.json")

    def schema_valid(self, candidate: Candidate) -> bool:
        return candidate.is_object and not any(True for _ in self.item_validator.iter_errors(candidate.item))

    # Tiers and defaults --------------------------------------------------------------------------------------------

    def slot_tier(self, slot: str) -> str:
        default = self.contract.slot_defaults[slot]["tier"]
        raised = (self.route.get("tier_upgrades") or {}).get(slot)
        if raised in TIERS and TIERS.index(raised) > TIERS.index(default):
            return raised
        return default

    def tier(self, candidate: Candidate) -> str | None:
        slot = candidate.slot
        if slot is None:
            return None
        own = candidate.get("tier")
        return own if own in TIERS else self.slot_tier(slot)

    def filled(self, candidate: Candidate, name: str):
        """A policy field's value after defaults: the item's own, else the route's override, else the slot default."""
        if candidate.is_object and name in candidate.item:
            return candidate.item[name]
        slot = candidate.slot
        if slot is None:
            return None
        override = (self.route.get("default_overrides") or {}).get(slot) or {}
        if name in override:
            return override[name]
        return self.contract.slot_defaults[slot][name]

    # Rendering sizes -----------------------------------------------------------------------------------------------

    def body_tokens(self, body: str, slot: str, count) -> int:
        """The largest rendering of a body across the slot's placements (conformance/README.md, Fitting)."""
        streams = self.streams_of(slot) or ["xml"]
        return max(count(render_body(body, stream)) for stream in streams)

    # Conflicts -----------------------------------------------------------------------------------------------------

    @cached_property
    def group_of(self) -> dict[str, dict]:
        out = {}
        for group in self.snapshot.get("conflicts", []):
            for item_id in group.get("items", []):
                out[item_id] = group
        return out

    def freshness(self, candidate: Candidate) -> Fraction | None:
        return instants.try_parse(candidate.get("freshness"))

    # Rank and shedding order (conformance/README.md, Fitting) --------------------------------------------------------

    def rank_key(self, candidate: Candidate) -> tuple:
        """Sorting by this puts the highest-ranked item first: the slot's order_by keys, then id."""
        keys = []
        for key in self.rules(candidate.slot).get("order_by", ["-relevance", "-freshness"]):
            if key == "-relevance":
                score = as_double(candidate.get("relevance"))
                keys.append((0, -score) if score is not None else (1, 0))
            else:
                fresh = self.freshness(candidate) or Fraction(0)
                keys.append(-fresh if key == "-freshness" else fresh)
        keys.append(utf16_key(candidate.recorded_id))
        return tuple(keys)

    def slot_order_key(self, slot: str) -> tuple:
        """Shedding order of slots: ascending priority (default 0), then slot name."""
        return (self.rules(slot).get("priority", 0), utf16_key(slot))

    def cap_bound(self, candidate: Candidate, count) -> bool:
        """Whether a reduction of this item may come from a cap (its token_budget or its slot's max_tokens), which
        fitting enforces before, and regardless of, budget pressure."""
        if "max_tokens" in self.rules(candidate.slot):
            return True
        cap = self.filled(candidate, "token_budget")
        body = candidate.get("body")
        return cap is not None and (count is None or not isinstance(body, str)
                                    or self.body_tokens(body, candidate.slot, count) > cap)


def canonical_key(value) -> bytes:
    return jcs.serialize_bytes(value)


def sort_ids(ids) -> list[str]:
    return sorted(ids, key=utf16_key)


def as_double(value) -> float | None:
    """A JSON number as the IEEE 754 double the spec compares (R-2); None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value)
    except OverflowError:
        return None


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)[:200]
