"""Admission faults: each makes an admissible item fail exactly one admission check (SPEC.md R-1–R-3, R-8–R-10,
R-13–R-16, R-18, R-20; contract/reasons.json). A fault may also change the route, the profile or other items, and
may add candidates of its own; it returns their fates so the label stays complete.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ...canon.strings import usable_id
from . import NotConstructible, excluded
from .builder import HOME, SLOTS, T, Builder

PROTECTED_BY_DEFAULT = ("governance.instructions", "governance.capabilities", "governance.output_contract",
                        "state.task", "interaction.query")
UNMARKED_SLOTS = ("evidence.knowledge", "evidence.tool_results", "interaction.memory", "interaction.history",
                  "interaction.query")
WRONG_AUTHORITY = {
    "governance.instructions": "user", "governance.capabilities": "state", "governance.examples": "user",
    "governance.output_contract": "observation", "state.user": "generated", "state.task": "user",
    "evidence.knowledge": "observation", "evidence.tool_results": "reference_only", "interaction.memory": "user",
    "interaction.history": "state", "interaction.query": "untrusted",
}
# A producer whose kind rules the slot out, whatever the route lists (R-8, R-13, R-14, R-15).
FORBIDDING = {
    "state.user": "kb", "state.task": "kb", "evidence.knowledge": "memory", "evidence.tool_results": "memory",
    "interaction.memory": "kb", "governance.instructions": "kb", "governance.examples": "kb",
    "governance.output_contract": "kb", "governance.capabilities": "memory", "interaction.history": "kb",
    "interaction.query": "kb",
}
MISSING = ("id", "source", "source_version", "authority", "freshness", "trust", "body")


@dataclass(frozen=True)
class Fault:
    code: str  # the reason the item must be excluded with
    variant: str
    slots: tuple[str, ...] | None  # where it applies; None: it takes the item's slot away
    apply: Callable[[Builder, dict], dict]  # changes the item (and the builder); returns fate overrides, by id

    @property
    def name(self) -> str:
        return f"{self.code}/{self.variant}"


def move(b: Builder, item: dict, producer: str, kind: str | None = None) -> None:
    for batch in b.batches.values():
        if any(i is item for i in batch["items"]):
            batch["items"] = [i for i in batch["items"] if i is not item]
    b.add(item, producer, kind)


def _others(b: Builder, item: dict, slot: str) -> list[dict]:
    return [i for i in b.items() if i is not item and i.get("slot") == slot]


def _set(**fields):
    def apply(b, item):
        item.update(fields)
        return {}
    return apply


def _omit(name):
    def apply(b, item):
        item.pop(name, None)
        return {}
    return apply


def _unlisted(b, item):
    move(b, item, "rogue", "retrieval")
    return {}


def _wrong_kind(b, item):
    b.route["producers"]["kb2"] = {"kind": "retrieval", "slots": [item["slot"]]}
    move(b, item, "kb2", "mcp")
    return {}


def _no_slot(b, item):
    item.pop("slot", None)
    return {}


def _needs_id(item):
    if not usable_id(item.get("id")):
        raise NotConstructible("the item has no usable id to share")


def _twin(b, item):
    _needs_id(item)
    twin = dict(item, body=item["body"] + " twin")
    b.add(twin, next(p for p, bt in b.batches.items() if any(i is item for i in bt["items"])))
    fate = excluded("duplicate_item_id", slot=item.get("slot"))
    return {item["id"]: [fate, dict(fate)]}  # both copies are excluded: two rows for the one id


def _shares_producer_row(b, item):
    _needs_id(item)
    b.exclude("memory", item["id"], "expired")
    return {}


def _route_unlisted(b, item):
    producer = "policy" if item["slot"] in ("interaction.history", "interaction.query") else "chat"
    move(b, item, producer)
    return {}


def _kind_forbids(b, item):
    producer = FORBIDDING[item["slot"]]
    listed = b.route["producers"][producer]["slots"]
    if item["slot"] not in listed:
        listed.append(item["slot"])
    move(b, item, producer)
    return {}


def _generated_turn(b, item):
    item.update(lineage="generated", authority="user")
    return {}


def _cap_not_allowed(b, item):
    b.capabilities["allowed_ids"] = [i for i in b.capabilities["allowed_ids"] if i != item["id"]]
    return {}


def _cap_from_mcp(b, item):
    b.route["producers"]["tools"]["slots"].append("governance.capabilities")
    move(b, item, "tools")
    return {}


def _cap_no_grant(b, item):
    b.capabilities = None
    return {}


def _cap_wrong_kind(b, item):
    b.capabilities["policy_producer"] = "policy"
    b.route["producers"]["policy"]["slots"].append("governance.capabilities")
    move(b, item, "policy")
    return {}


def _variants(ids):
    def apply(b, item):
        item["variants"] = [{"id": v if v != "@" else item["id"], "body": f"short {n}", "method": "extract",
                             "lineage": "extracted"} for n, v in enumerate(ids)]
        return {}
    return apply


def _stale(b, item):
    b.rule(item["slot"], max_age_seconds=60)
    item["freshness"] = "2026-09-22T11:58:59Z"
    return {}


def _old(b, item):
    b.rule(item["slot"], max_age_seconds=3600)
    item["freshness"] = "2026-09-22T10:00:00Z"
    return {}


def _bad_source(b, item):
    b.rule(item["slot"], source_prefix="turn:" if item["slot"] == "interaction.memory" else "src:")
    item["source"] = "bad:elsewhere"
    return {}


def _required_scope(b, item):
    b.rule(item["slot"], required_scope=["user"])
    for other in _others(b, item, item["slot"]):
        other.setdefault("scope", {})["user"] = "u_1"
    item["scope"] = {"tenant": "acme"}
    return {}


def _low(b, item):
    b.rule(item["slot"], min_relevance=0.8)
    for other in _others(b, item, item["slot"]):
        other["relevance"] = 1.0
    item["relevance"] = 0.5
    return {}


def _unscored(b, item):
    b.rule(item["slot"], min_relevance=0.8)
    for other in _others(b, item, item["slot"]):
        other["relevance"] = 1.0
    item.pop("relevance", None)
    return {}


def _unplace(b, item):
    b.unplace(item["slot"])
    return {}


ALL = tuple(SLOTS)
NOT_PROTECTED = tuple(s for s in SLOTS if s not in PROTECTED_BY_DEFAULT)
GOVERNANCE = tuple(s for s in SLOTS if s.startswith("governance."))

FAULTS: list[Fault] = [
    Fault("producer_not_authenticated", "unlisted", ALL, _unlisted),
    Fault("producer_not_authenticated", "wrong-kind", ALL, _wrong_kind),
    *[Fault(f"missing_field:{f}", "omitted", ALL, _omit(f)) for f in MISSING],
    Fault("missing_field:expires", "omitted", ("interaction.memory",), _omit("expires")),
    Fault("missing_field:relevance", "omitted", ("evidence.knowledge",), _omit("relevance")),
    Fault("missing_field:slot", "omitted", None, _no_slot),
    Fault("unknown_slot", "name", None, _set(slot="evidence.web")),
    Fault("unknown_slot", "number", None, _set(slot=5)),
    Fault("unknown_slot", "null", None, _set(slot=None)),
    Fault("unknown_authority", "name", ALL, _set(authority="reference")),
    Fault("unknown_authority", "number", ALL, _set(authority=7)),
    Fault("invalid_structure", "extra-field", ALL, _set(colour="blue")),
    Fault("invalid_structure", "impossible-date", ALL, _set(freshness="2026-02-30T12:00:00Z")),
    Fault("invalid_structure", "wrong-type", ALL, _set(token_budget="many")),
    Fault("invalid_structure", "blank-eligibility", ALL, _set(eligibility=" ")),
    Fault("invalid_structure", "variant-missing-method", ALL,
          lambda b, i: i.update(variants=[{"id": f"{i['id']}~v", "body": "short", "lineage": "extracted"}]) or {}),
    Fault("duplicate_item_id", "twin", ALL, _twin),
    Fault("duplicate_item_id", "producer-row", ALL, _shares_producer_row),
    Fault("producer_slot_not_allowed", "route-unlisted", ALL, _route_unlisted),
    Fault("producer_slot_not_allowed", "kind-forbids", ALL, _kind_forbids),
    *[Fault("authority_not_allowed", "wrong-role", (s,), _set(authority=WRONG_AUTHORITY[s])) for s in SLOTS],
    Fault("authority_not_allowed", "generated-turn-as-user", ("interaction.history",), _generated_turn),
    Fault("capability_not_allowed", "not-on-allow-list", ("governance.capabilities",), _cap_not_allowed),
    Fault("capability_not_allowed", "from-mcp", ("governance.capabilities",), _cap_from_mcp),
    Fault("capability_not_allowed", "no-grant", ("governance.capabilities",), _cap_no_grant),
    Fault("capability_not_allowed", "grant-producer-wrong-kind", ("governance.capabilities",), _cap_wrong_kind),
    Fault("untrusted_in_governance", "unverified", GOVERNANCE, _set(trust="unverified")),
    Fault("untrusted_in_governance", "marked", GOVERNANCE, _set(injection_risk="untrusted_content")),
    Fault("untrusted_content_unmarked", "unmarked", UNMARKED_SLOTS, _set(injection_risk="none")),
    Fault("protected_tier_changed", "lowered", PROTECTED_BY_DEFAULT, _set(tier="compressible")),
    *[Fault("tier_upgrade_not_allowed", "raised", (s,), _set(tier="compressible" if s in ("governance.examples",
                                                                                          "state.user") else "protected"))
      for s in NOT_PROTECTED],
    Fault("duplicate_variant_id", "repeated", ALL, _variants(["v1", "v1"])),
    Fault("duplicate_variant_id", "parent-id", ALL, _variants(["@"])),
    Fault("revoked", "revoked-by", ALL, _set(revoked_by="turn:0")),
    Fault("expired", "at-assembly-time", ALL, _set(expires=T)),
    Fault("expired", "same-instant-offset", ALL, _set(expires="2026-09-22T14:00:00+02:00")),
    Fault("future_freshness", "one-microsecond", ALL, _set(freshness="2026-09-22T12:00:00.000001Z")),
    Fault("stale_state", "max-age", ("state.user", "state.task"), _stale),
    Fault("source_invalid", "prefix", ALL, _bad_source),
    Fault("out_of_scope", "other-value", ALL, _set(scope={"tenant": "globex"})),
    Fault("out_of_scope", "key-request-lacks", ALL, _set(scope={"tenant": "acme", "session": "s_9"})),
    Fault("out_of_scope", "required-key-missing", ALL, _required_scope),
    Fault("below_threshold", "low-score", ALL, _low),
    Fault("below_threshold", "unscored", tuple(s for s in SLOTS if s != "evidence.knowledge"), _unscored),
    Fault("not_eligible", "too-old", tuple(s for s in SLOTS if not s.startswith("state.")), _old),
    Fault("slot_unplaced", "no-placement", NOT_PROTECTED, _unplace),
]


def target(b: Builder, slot: str, id: str = "x:target") -> dict:
    """The item a fault is applied to: admissible, in its slot's usual producer."""
    return b.item(id, slot if slot else "evidence.knowledge", producer=HOME.get(slot, "kb"))


def recorded_id(b: Builder, item: dict) -> str:
    """How the trace will record the item: its id, or {producer}#invalid-{n} when it has no usable id (R-2)."""
    from ...canon.strings import usable_id

    if isinstance(item, dict) and usable_id(item.get("id")):
        return item["id"]
    for producer, batch in b.batches.items():
        n = 0
        for entry in batch["items"]:
            if entry is item:
                return f"{producer}#invalid-{n}"
            if not (isinstance(entry, dict) and usable_id(entry.get("id"))):
                n += 1
    raise KeyError("item is in no batch")
