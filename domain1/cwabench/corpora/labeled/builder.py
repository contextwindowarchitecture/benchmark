"""Building valid snapshots item by item, so a generator changes exactly one thing from a known-good base.

Every item the builder makes is admissible as made: the authority its slot takes, verified trust in governance,
the injection marker where its slot's default needs one, every policy field written out (so nothing is filled by
default unless a generator removes it), a relevance in knowledge, an expiry and a turn source in memory, and the
request's tenant in its scope. Every body is distinct, so no two items are duplicates unless a generator says so.
"""
from __future__ import annotations

import copy
import json

from ...contract import Contract

T = "2026-09-22T12:00:00Z"  # assembly_time
RECENT = "2026-09-22T11:59:30Z"  # freshness of every item unless a generator sets one
SCOPE = {"tenant": "acme", "user": "u_1", "task": "t_1"}

SLOTS = (
    "governance.instructions", "governance.capabilities", "governance.examples", "governance.output_contract",
    "state.user", "state.task", "evidence.knowledge", "evidence.tool_results",
    "interaction.memory", "interaction.history", "interaction.query",
)
AUTHORITY = {
    "governance.instructions": "governing", "governance.capabilities": "governing", "governance.examples": "governing",
    "governance.output_contract": "governing", "state.user": "state", "state.task": "state",
    "evidence.knowledge": "reference_only", "evidence.tool_results": "observation", "interaction.memory": "generated",
    "interaction.history": "user", "interaction.query": "user",
}
POLICY_FIELDS = ("token_budget", "variants", "conflict_policy", "lineage", "eligibility", "injection_risk")

# The base route's producers: id → (kind, slots). Each slot has one producer that normally sends it.
PRODUCERS = {
    "policy": ("policy", ["governance.instructions", "governance.examples", "governance.output_contract"]),
    "caps": ("capability_policy", ["governance.capabilities"]),
    "state": ("state", ["state.user", "state.task"]),
    "kb": ("retrieval", ["evidence.knowledge", "evidence.tool_results"]),
    "tools": ("mcp", ["evidence.tool_results"]),
    "memory": ("memory", ["interaction.memory"]),
    "chat": ("interaction", ["interaction.history", "interaction.query"]),
}
HOME = {
    "governance.instructions": "policy", "governance.examples": "policy", "governance.output_contract": "policy",
    "governance.capabilities": "caps", "state.user": "state", "state.task": "state", "evidence.knowledge": "kb",
    "evidence.tool_results": "tools", "interaction.memory": "memory", "interaction.history": "chat",
    "interaction.query": "chat",
}
WORDS = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa quebec "
         "romeo sierra tango uniform victor whiskey xray yankee zulu").split()


def tag(slot: str) -> str:
    return slot.replace(".", "_")


def body(seed: str, words: int = 4) -> str:
    """A distinct body of exactly `words` whitespace-separated words."""
    return " ".join([f"[{seed}]"] + [WORDS[(len(seed) * 7 + i * 3) % len(WORDS)] for i in range(words - 1)])


class Builder:
    """A snapshot under construction. `base()` adds the items every assembly needs; generators add the rest."""

    def __init__(self, contract: Contract, name: str):
        self.contract = contract
        self.name = name
        self.route = {
            "route": "bench", "version": f"bench/{name}", "clock_skew_seconds": 0,
            "producers": {p: {"kind": k, "slots": list(s)} for p, (k, s) in PRODUCERS.items()},
            "slots": {},
        }
        self.placement = [{"slot": s, "wrap": f"xml:{tag(s)}"} for s in SLOTS]
        self.budget = {"input": 100000, "reserved_output": 1000}
        self.batches: dict[str, dict] = {}  # producer → {"kind", "items", "excluded"}
        self.conflicts: list[dict] = []
        self.capabilities = {"policy_producer": "caps", "allow_list_version": "v1", "allowed_ids": []}
        self.scope = dict(SCOPE)
        self.renderer = "fixture-xml/v1"
        self.tokenizer = "fixture-whitespace/v1"

    # Items -----------------------------------------------------------------------------------------------------------

    def item(self, id, slot: str, text: str | None = None, *, producer: str | None = None, words: int = 4,
             omit=(), **fields) -> dict:
        """Add an admissible item, then apply `fields` and `omit`. Returns the item, which stays editable."""
        defaults = self.contract.slot_defaults.get(slot, {})
        item = {
            "id": id, "slot": slot, "source": f"src:{id}", "source_version": "1",
            "authority": AUTHORITY.get(slot, "reference_only"),
            "trust": "verified" if slot.startswith("governance.") else "unverified",
            "freshness": RECENT, "body": text if text is not None else body(str(id), words),
            "scope": {"tenant": "acme"},
        }
        for name in POLICY_FIELDS:
            item[name] = copy.deepcopy(defaults.get(name))
        if slot == "evidence.knowledge":
            item["relevance"] = 0.9
        if slot == "interaction.memory":
            item["expires"] = "2026-12-31T00:00:00Z"
            item["source"] = f"turn:{id}"
        if slot == "governance.capabilities":
            self.capabilities["allowed_ids"].append(id)
        item.update(fields)
        for name in omit:
            item.pop(name, None)
        self.add(item, producer or HOME.get(slot, "kb"))
        return item

    def add(self, item, producer: str, kind: str | None = None) -> None:
        """Put any batch entry into a producer's batch, as given."""
        listed = self.route["producers"].get(producer)
        batch = self.batches.setdefault(producer, {"kind": kind or (listed["kind"] if listed else "retrieval"),
                                                   "items": [], "excluded": []})
        batch["items"].append(item)

    def exclude(self, producer: str, item_id: str, reason: str, **refs) -> None:
        """A row the producer reports for an entry it suppressed."""
        listed = self.route["producers"].get(producer)
        batch = self.batches.setdefault(producer, {"kind": listed["kind"] if listed else "retrieval",
                                                   "items": [], "excluded": []})
        batch["excluded"].append({"item_id": item_id, "reason": reason, "stage": "producer", **refs})

    def base(self, *, parser: bool = False) -> None:
        """The items no assembly may lack: instructions and the query, and the output contract on a parser route."""
        self.item("gov:base", "governance.instructions", "Answer from the evidence and cite it.")
        self.item("q:base", "interaction.query", "What does the plan include?")
        if parser:
            self.route["parser"] = True
            self.item("fmt:base", "governance.output_contract", "Reply as JSON with an answer field.")

    def rule(self, slot: str, **rules) -> None:
        self.route.setdefault("slots", {}).setdefault(slot, {}).update(rules)

    def unplace(self, slot: str) -> None:
        self.placement = [p for p in self.placement if p["slot"] != slot]

    def group(self, id: str, kind: str, items: list[str], fact: str | None = None) -> None:
        group = {"id": id, "kind": kind, "items": list(items)}
        if fact is not None:
            group["fact"] = fact
        self.conflicts.append(group)

    def items(self) -> list[dict]:
        return [i for b in self.batches.values() for i in b["items"] if isinstance(i, dict)]

    def find(self, id) -> dict:
        return next(i for i in self.items() if i.get("id") == id)

    # Output -----------------------------------------------------------------------------------------------------------

    def snapshot(self) -> dict:
        profile = {
            "spec": "cwa/draft", "id": f"bench-{self.name}", "version": 1, "route": self.route["route"],
            "model_family": None, "route_policy_version": self.route["version"], "placement": self.placement,
            "evaluation": {"status": "unevaluated", "suite": None, "date": None, "result": None, "artifact": None},
        }
        route = copy.deepcopy(self.route)
        if not route.get("slots"):
            route.pop("slots", None)
        out = {
            "assembly_time": T, "scope": self.scope, "budget": self.budget, "profile": profile, "route_policy": route,
            "tokenizer": self.tokenizer, "renderer": self.renderer,
            "batches": [{"producer": {"id": p, "kind": b["kind"]}, "items": b["items"], "excluded": b["excluded"]}
                        for p, b in self.batches.items()],
            "conflicts": self.conflicts,
        }
        if self.capabilities is not None:
            out["capabilities"] = self.capabilities
        return out

    def bytes(self) -> bytes:
        return json.dumps(self.snapshot(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def cost(text: str) -> int:
    """Tokens one item adds to a fixture-xml payload under fixture-whitespace: <tag, id="…">, </tag> and the body.
    Valid only for ids without whitespace and items not marked as conflicting."""
    from ...canon.tokenizers import fixture_whitespace

    return 3 + fixture_whitespace(text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def payload_tokens(b: Builder, bodies: dict | None = None) -> int:
    """The fixture-xml payload's count if every item in the builder renders, with `bodies` overriding some items'
    bodies (a chosen variant). Valid only when every item is admitted, placed once and not marked conflicting."""
    bodies = bodies or {}
    return sum(cost(bodies.get(i["id"], i["body"])) for i in b.items())


def words(text: str) -> int:
    from ...canon.tokenizers import fixture_whitespace

    return fixture_whitespace(text)
