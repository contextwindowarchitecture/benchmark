"""Valid snapshot generation (domain-1-plan.md, 7.5): random, seeded, and steered toward what has not been exercised.

A snapshot is the labeled corpora's admissible base plus a few *intents*, each a feature chosen by weight: one admission
fault from the labeled fault table in one slot, a conflict group, a pipeline rule with items that trigger it, budget
pressure with variants and slot rules, a refusal condition, or a pool of edge-case values. Every intent names the
coverage tags it aims at, as traces.coverage_tags writes them, so the suite can raise the weight of intents whose tags
real traces have not shown yet (steer.py).

Generated snapshots carry no label: the auditor and the differential oracle judge them. Each must still be valid
(canon/validity.py), since a rejected snapshot exercises nothing past the snapshot checks; mutate.py makes the invalid
ones on purpose.

A fifth of the snapshots are written in a random JSON surface (canon/spelling.py): the same value with keys reordered,
whitespace added, strings escaped and numbers respelled, which a conformant assembler must read as the same snapshot.

Everything here is a pure function of its random generator, so a seed and the weights reproduce a corpus byte for byte.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable

from ...canon import validity
from ...canon.payloads import escape_body
from ...canon.spelling import spell
from ...canon.tokenizers import TOKENIZERS
from ...contract import Contract
from ..labeled import NotConstructible
from ..labeled.builder import HOME, SLOTS, Builder, tag
from ..labeled.faults import FAULTS
from . import pools

RENDERERS = ("fixture-xml/v1", "cwa-messages/v1", "cwa-message-blocks/v1")
TOKENIZER_IDS = ("fixture-whitespace/v1", "estimate-utf8/v1")
EVIDENCE = ("evidence.knowledge", "evidence.tool_results")
RESPELL = 0.2  # the share of snapshots written in a random JSON surface (canon/spelling.py) rather than compactly
NOT_PROTECTED = ("governance.examples", "state.user", "evidence.knowledge", "evidence.tool_results",
                 "interaction.memory", "interaction.history")
SCOPE_KEYS = ("tenant", "user", "session", "task", "locale", "step")
PRODUCER_REASONS = ("expired", "revoked", "below_threshold", "out_of_scope", "over_budget", "missing_field:relevance",
                    "not_eligible", "source_invalid")


class GeneratorError(Exception):
    """The generator built an invalid snapshot: a bug in the generator, never in an assembler."""


@dataclass(frozen=True)
class Intent:
    name: str
    family: str
    targets: tuple[str, ...]  # coverage tags it aims to produce
    apply: Callable[["Gen"], None]


@dataclass(frozen=True)
class Generated:
    case_id: str
    data: bytes
    intents: tuple[str, ...]


@dataclass
class Gen:
    """One snapshot under construction."""

    contract: Contract
    rng: random.Random
    b: Builder
    renderer: str = "fixture-xml/v1"
    tokenizer: str = "fixture-whitespace/v1"
    ratio: float | None = None  # budget as a fraction of the estimated payload; None leaves the base budget
    messages_layout: bool = False
    used: set = field(default_factory=set)
    n: int = 0

    def fresh(self, prefix: str, edgy: float = 0.0) -> str:
        """A new id: sometimes one from the edge-case pool, else prefix plus a counter."""
        if edgy and self.rng.random() < edgy:
            candidate = self.rng.choice(pools.IDS)
            if candidate not in self.used:
                self.used.add(candidate)
                return candidate
        while True:
            self.n += 1
            candidate = f"{prefix}{self.n}"
            if candidate not in self.used:
                self.used.add(candidate)
                return candidate

    def ago(self, age: Fraction | None = None, style: int | None = None) -> str:
        age = self.rng.choice(pools.AGES) if age is None else age
        return pools.instant(pools.T - age, self.rng.randrange(pools.STYLES) if style is None else style)

    def text(self, words: int | None = None) -> str:
        if self.rng.random() < 0.15:
            return self.rng.choice(pools.BODIES)
        words = words or self.rng.randint(1, 12)
        return " ".join(self.rng.choice(("alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
                                         "refund", "policy", "window", "days", "plan", "order", "x"))
                        for _ in range(words)) + f" #{self.n}"

    def item(self, slot: str, *, id=None, producer: str | None = None, edgy: float = 0.1, omit=(), **fields) -> dict:
        """An admissible item in `slot`, with randomized fields that keep it admissible, then `fields` on top."""
        rng = self.rng
        if slot == "governance.capabilities" and self.b.capabilities is None:
            self.b.capabilities = {"policy_producer": "caps", "allow_list_version": "v1", "allowed_ids": []}
        item_id = id if id is not None else self.fresh(slot.split(".")[1][:3] + ":", edgy)
        extra = {"freshness": self.ago()}
        if "body" not in fields:
            extra["body"] = self.text()
        if slot == "evidence.knowledge":
            extra["relevance"] = rng.choice(pools.RELEVANCE) if rng.random() < 0.3 else round(rng.random(), 3)
        elif rng.random() < 0.15:
            extra["relevance"] = round(rng.random(), 2)
        if slot == "interaction.history" and rng.random() < 0.3:
            extra.update(lineage="generated", authority="untrusted")  # an assistant turn: never user authority
        if slot == "evidence.tool_results" and rng.random() < 0.2:
            extra["authority"] = "untrusted"
        if slot == "interaction.memory":
            extra["expires"] = rng.choice(("2026-12-31T00:00:00Z", "2026-09-22T12:00:00.000000000001Z",
                                           "2026-09-22T14:00:00.5+02:00"))
        # Some policy fields left to the slot default, each a defaults_filled row (R-3).
        omit = tuple(omit) + tuple(name for name in ("token_budget", "variants", "conflict_policy", "lineage",
                                                     "eligibility")
                                   if rng.random() < 0.15 and name not in fields and name not in extra)
        extra.update(fields)
        if producer is None and slot == "evidence.tool_results":
            producer = rng.choice(("tools", "tools", "kb"))
        return self.b.item(item_id, slot, extra.pop("body"), producer=producer, omit=omit, **extra)

    def variant(self, parent: dict, words: int | None = None, method: str = "extract") -> dict:
        n = sum(1 for v in parent.get("variants") or [] if isinstance(v, dict))
        return {"id": f"{parent['id']}~v{n}", "body": self.text(words or self.rng.randint(1, 4)), "method": method,
                "lineage": self.rng.choice(("extracted", "summarised"))}


# Intents ---------------------------------------------------------------------------------------------------------------


def _fault_intent(fault, slot):
    def apply(g: Gen) -> None:
        item = g.item(slot or "evidence.knowledge", id=g.fresh("f:"), edgy=0)
        fault.apply(g.b, item)
    row_slot = slot if fault.slots is not None else "-"
    return Intent(f"fault:{fault.name}@{slot or '-'}", "fault", (f"reason:{fault.code}@{row_slot}",), apply)


def _instruction_group(g: Gen) -> None:
    rng = g.rng
    choices = (("governance.instructions", {}), ("governance.examples", {}), ("interaction.history", {}),
               ("interaction.query", {}), ("interaction.history", {"authority": "untrusted", "lineage": "generated"}),
               ("evidence.knowledge", {}))
    members = []
    for _ in range(rng.randint(2, 3)):
        slot, fields = rng.choice(choices)
        if rng.random() < 0.85:
            fields = dict(fields, conflict_policy=rng.choice(("governs", "defers", "escalate")))
        members.append(g.item(slot, **fields)["id"])
    if rng.random() < 0.7:
        g.b.route["on_unresolved_instruction"] = rng.choice(("surface", "request_context", "refuse"))
    g.b.group(g.fresh("gi-"), "instruction", members)


def _fact_group(g: Gen) -> None:
    rng = g.rng
    fact = g.fresh("fact-")
    homes = (("evidence.knowledge", "kb"), ("evidence.tool_results", "tools"), ("evidence.tool_results", "kb"),
             ("state.user", "state"), ("interaction.memory", "memory"))
    policy = {"precedence": rng.sample(["kb", "tools", "state", "memory", "chat", "elsewhere"], rng.randint(1, 3)),
              "on_unresolved": rng.choice(("surface", "request_context", "refuse"))}
    if rng.random() < 0.6:
        policy["scope"] = rng.sample(["tenant", "user", "task", "session"], rng.randint(1, 2))
    if rng.random() < 0.6:
        policy["freshness_tiebreak"] = rng.random() < 0.7
    g.b.route.setdefault("facts", {})[fact] = policy
    shared_age = rng.choice(pools.AGES)
    members = []
    for _ in range(rng.randint(2, 3)):
        slot, producer = rng.choice(homes)
        scope = {k: v for k, v in g.b.scope.items() if rng.random() < 0.6}
        scope.setdefault("tenant", "acme")
        age = shared_age if rng.random() < 0.4 else None  # equal instants, often written differently
        members.append(g.item(slot, producer=producer, scope=scope, freshness=g.ago(age))["id"])
    g.b.group(g.fresh("gf-"), "fact", members, fact)


def _supersede(g: Gen) -> None:
    rng = g.rng
    slot = rng.choice(("evidence.tool_results", "evidence.knowledge", "interaction.memory", "state.user",
                       "interaction.history"))
    g.b.rule(slot, supersede="source")
    producer = "tools" if slot == "evidence.tool_results" else HOME[slot]
    source = ("turn:" if slot == "interaction.memory" else "src:") + g.fresh("call-")
    latest = rng.choice(pools.AGES)
    ids = []
    for _ in range(rng.randint(2, 4)):
        age = latest if rng.random() < 0.35 else latest + rng.choice((1, 60, Fraction(1, 10**12)))
        ids.append(g.item(slot, producer=producer, source=source, freshness=g.ago(age))["id"])
    if rng.random() < 0.15 and len(ids) >= 2:
        g.b.group(g.fresh("gx-"), "instruction", ids[:2])  # named by a group: exempt (R-25)


def _dedupe(g: Gen) -> None:
    rng = g.rng
    slot = rng.choice(("evidence.knowledge", "evidence.tool_results", "interaction.memory", "interaction.history",
                       "governance.examples", "state.user", "state.task"))
    g.b.rule(slot, dedupe="exact")
    family = rng.choice(pools.DEDUPE_EQUAL)
    tie = rng.choice(pools.RELEVANCE_TIES)
    for k, body in enumerate(rng.sample(family, rng.randint(2, len(family)))):
        fields = {"body": body}
        if slot == "evidence.knowledge" and rng.random() < 0.5:
            fields["relevance"] = tie[k % 2]
        g.item(slot, **fields)
    if rng.random() < 0.6:
        for body in rng.choice(pools.DEDUPE_DISTINCT):
            g.item(slot, body=body)


def _diversity(g: Gen) -> None:
    rng = g.rng
    slot = rng.choice(EVIDENCE)
    g.b.rule(slot, max_per_source=rng.randint(1, 3))
    sources = [f"src:{g.fresh('doc-')}" for _ in range(rng.randint(1, 2))]
    producer = "tools" if slot == "evidence.tool_results" else "kb"
    for _ in range(rng.randint(3, 6)):
        g.item(slot, producer=producer, source=rng.choice(sources))


def _pressure(g: Gen) -> None:
    rng = g.rng
    for _ in range(rng.randint(2, 6)):
        slot = rng.choice(NOT_PROTECTED)
        item = g.item(slot, body=g.text(rng.randint(4, 16)))
        if rng.random() < 0.6 and slot not in ("governance.examples", "state.user"):
            item["variants"] = [g.variant(item) for _ in range(rng.choice((1, 1, 2, 3)))]
            if rng.random() < 0.2:
                item["variants"].append(g.variant(item, 20))  # longer than its parent: never chosen to compress
        if rng.random() < 0.2:
            item["token_budget"] = rng.randint(0, 8)
    rules = g.b.route.setdefault("slots", {})
    for slot in rng.sample(NOT_PROTECTED, rng.randint(0, 3)):
        choice = rng.choice(("priority", "order_by", "max_tokens", "min_tokens"))
        value = {"priority": rng.randint(-2, 2), "order_by": rng.sample(["-relevance", "-freshness", "freshness"],
                                                                         rng.randint(1, 2)),
                 "max_tokens": rng.randint(0, 30), "min_tokens": rng.randint(1, 30)}[choice]
        rules.setdefault(slot, {})[choice] = value
    if rng.random() < 0.3:
        steps = [{"slot": s, "action": a} for s in rng.sample(NOT_PROTECTED, 2) for a in ("compress", "omit")]
        g.b.route["fitting_order"] = rng.sample(steps, rng.randint(1, len(steps)))
    if rng.random() < 0.25:
        slot = rng.choice(("governance.examples", "state.user", "evidence.knowledge", "interaction.history"))
        g.b.route.setdefault("tier_upgrades", {})[slot] = rng.choice(("compressible", "protected"))
    if rng.random() < 0.3:
        g.b.budget["margin_percent"] = rng.choice((0, 1, 5, 10, 33, 100))
    g.ratio = rng.choice((0.3, 0.5, 0.7, 0.85, 0.95, 1.0, 1.02))


def _required_missing(g: Gen) -> None:
    item = g.b.find(g.rng.choice(("gov:base", "q:base")))
    item["expires"] = "2026-09-22T12:00:00Z"  # expired at assembly_time, so the slot has no admitted item (R-4)


def _protected_unplaced(g: Gen) -> None:
    rng = g.rng
    if rng.random() < 0.5:
        g.item("state.task")
        g.b.unplace("state.task")
    else:
        slot = rng.choice(NOT_PROTECTED)
        g.b.route.setdefault("tier_upgrades", {})[slot] = "protected"
        g.item(slot)
        g.b.unplace(slot)


def _evidence_required(g: Gen) -> None:
    rng = g.rng
    g.b.route["requires_evidence"] = True
    for slot in rng.sample(EVIDENCE, rng.randint(0, 2)):
        g.b.rule(slot, min_included=rng.randint(1, 3))
    for _ in range(rng.randint(0, 3)):
        item = g.item(rng.choice(EVIDENCE))
        if rng.random() < 0.4:
            item["expires"] = g.ago(0)  # expired: lost to admission
        elif rng.random() < 0.4:
            item["variants"] = [g.variant(item)]
    if rng.random() < 0.5:
        g.ratio = rng.choice((0.4, 0.6, 0.8))


def _protected_over(g: Gen) -> None:
    rng = g.rng
    kind = rng.randrange(3)
    if kind == 0:
        g.ratio = rng.choice((0.05, 0.2, 0.5))
    elif kind == 1:
        g.b.find("gov:base")["token_budget"] = rng.randint(0, 3)
    else:
        g.b.rule("governance.instructions", max_tokens=rng.randint(0, 5))


def _slot_floor(g: Gen) -> None:
    rng = g.rng
    slot = rng.choice(EVIDENCE + ("interaction.history",))
    g.b.rule(slot, min_tokens=rng.randint(5, 40))
    for _ in range(rng.randint(2, 4)):
        g.item(slot, body=g.text(rng.randint(5, 12)))
    g.ratio = rng.choice((0.3, 0.5, 0.7))


def _producer_rows(g: Gen) -> None:
    rng = g.rng
    producer = rng.choice(("kb", "memory", "tools"))
    slot = {"kb": "evidence.knowledge", "memory": "interaction.memory", "tools": "evidence.tool_results"}[producer]
    kept = g.item(slot, producer=producer)
    for _ in range(rng.randint(1, 3)):
        roll = rng.random()
        if roll < 0.3:
            g.b.exclude(producer, g.fresh("pr:"), "duplicate_content", duplicate_of=kept["id"])
        elif roll < 0.5:
            g.b.exclude(producer, g.fresh("pr:"), "superseded", superseded_by=kept["id"])
        else:
            g.b.exclude(producer, g.fresh("pr:", 0.2), rng.choice(PRODUCER_REASONS))


def _duplicate_ids(g: Gen) -> None:
    rng = g.rng
    first = g.item(rng.choice(("evidence.knowledge", "interaction.memory", "state.user")))
    if rng.random() < 0.5:
        g.item(rng.choice(("evidence.tool_results", "interaction.history")), id=first["id"])
    else:
        g.b.exclude(rng.choice(("kb", "memory")), first["id"], "expired")


def _unnamed(g: Gen) -> None:
    rng = g.rng
    slots = [s for s in SLOTS if s != "governance.capabilities"]  # the grant's allow list holds only usable ids
    for _ in range(rng.randint(1, 3)):
        roll = rng.random()
        if roll < 0.4:
            g.item(rng.choice(slots), id=rng.choice(pools.BLANK_IDS))
        elif roll < 0.7:
            g.item(rng.choice(slots), id="unused", omit=("id",))
        else:
            g.item(rng.choice(slots), id=rng.choice((5, None, ["x"])))


def _twice(g: Gen) -> None:
    slot = g.rng.choice(SLOTS)
    g.b.placement.append({"slot": slot, "wrap": f"xml:again_{tag(slot)}"})
    if slot not in ("governance.instructions", "interaction.query"):  # those already hold the base items
        g.item(slot)


def _edge_ids(g: Gen) -> None:
    slot = g.rng.choice(("evidence.knowledge", "evidence.tool_results", "interaction.history", "governance.examples"))
    for _ in range(g.rng.randint(2, 5)):
        g.item(slot, edgy=0.9)


def _edge_bodies(g: Gen) -> None:
    for _ in range(g.rng.randint(1, 4)):
        g.item(g.rng.choice(SLOTS[:1] + NOT_PROTECTED + ("interaction.query",)), body=g.rng.choice(pools.BODIES))


def _edge_numbers(g: Gen) -> None:
    rng = g.rng
    tie = rng.choice(pools.RELEVANCE_TIES)
    if rng.random() < 0.5:
        g.b.rule("evidence.knowledge", min_relevance=rng.choice(tie))
    for value in (*tie, *rng.sample(pools.RELEVANCE, 3)):
        g.item("evidence.knowledge", relevance=value)


def _edge_instants(g: Gen) -> None:
    rng = g.rng
    skew = rng.choice((0, 1, 5, 60))
    g.b.route["clock_skew_seconds"] = skew
    g.item("evidence.tool_results", freshness=pools.instant(pools.T + skew, rng.randrange(pools.STYLES)))  # at the edge
    g.item("evidence.tool_results", freshness=pools.instant(pools.T + skew + Fraction(1, 10**12),
                                                            rng.randrange(pools.STYLES)))  # just beyond
    g.item("interaction.memory", expires=pools.instant(pools.T, rng.randrange(pools.STYLES)))  # expired
    g.item("interaction.memory", expires=pools.instant(pools.T + Fraction(1, 10**12), rng.randrange(pools.STYLES)))
    if rng.random() < 0.5:
        g.b.rule(rng.choice(("evidence.knowledge", "state.user")), max_age_seconds=rng.choice((0, 59, 60, 3600)))


def _capabilities(g: Gen) -> None:
    rng = g.rng
    for _ in range(rng.randint(1, 3)):
        g.item("governance.capabilities")
    if rng.random() < 0.4:
        g.b.route["producers"]["tools"]["slots"] = ["evidence.tool_results", "governance.capabilities"]
        g.item("governance.capabilities", producer="tools")
    if rng.random() < 0.5:
        g.b.route["producers"]["tools"]["verified"] = rng.random() < 0.7
        g.item("evidence.tool_results", producer="tools", injection_risk="none")


def _defaults(g: Gen) -> None:
    rng = g.rng
    slot = rng.choice(SLOTS)
    override = rng.choice(({"token_budget": rng.randint(1, 20)}, {"lineage": "extracted"}, {"eligibility": "custom"},
                           {"conflict_policy": "escalate"}, {"variants": []}))
    g.b.route.setdefault("default_overrides", {})[slot] = override
    g.item(slot, omit=tuple(override))  # takes the route's override, not the slot default


def _history(g: Gen) -> None:
    rng = g.rng
    shared = rng.choice(pools.AGES)
    for _ in range(rng.randint(3, 6)):
        age = shared if rng.random() < 0.3 else None
        g.item("interaction.history", freshness=g.ago(age))


def _scope(g: Gen) -> None:
    rng = g.rng
    slot = rng.choice(("evidence.knowledge", "interaction.memory", "state.user"))
    keys = rng.sample(["user", "task"], rng.randint(1, 2))
    g.b.rule(slot, required_scope=keys)
    for _ in range(rng.randint(2, 4)):
        scope = {"tenant": "acme", **{k: g.b.scope[k] for k in keys if rng.random() < 0.7}}
        g.item(slot, scope=scope)


def _messages_layout(g: Gen) -> None:
    g.messages_layout = True


FEATURES = [
    Intent("conflict:instruction", "conflict", ("conflict:instruction:policy", "conflict:instruction:escalated",
                                                "conflict:instruction:authority", "reason:conflict_deferred@governance.examples",
                                                "reason:conflict_deferred@interaction.history"), _instruction_group),
    Intent("conflict:fact", "conflict", ("conflict:fact:policy", "conflict:fact:freshness", "conflict:fact:escalated",
                                         "reason:conflict_lost@evidence.knowledge",
                                         "reason:conflict_lost@evidence.tool_results"), _fact_group),
    Intent("pipeline:supersede", "pipeline", tuple(f"reason:superseded@{s}" for s in
                                                   ("evidence.tool_results", "evidence.knowledge", "interaction.memory",
                                                    "state.user", "interaction.history")), _supersede),
    Intent("pipeline:dedupe", "pipeline", tuple(f"reason:duplicate_content@{s}" for s in
                                                ("evidence.knowledge", "evidence.tool_results", "interaction.memory",
                                                 "interaction.history", "governance.examples", "state.user")), _dedupe),
    Intent("pipeline:diversity", "pipeline", tuple(f"reason:source_diversity_cap@{s}" for s in EVIDENCE), _diversity),
    Intent("fit:pressure", "fit", ("fit:omitted", "fit:compressed", *(f"reason:over_budget@{s}" for s in NOT_PROTECTED)),
           _pressure),
    Intent("refusal:required_slot_missing", "refusal", ("refusal:required_slot_missing",), _required_missing),
    Intent("refusal:protected_slot_unplaced", "refusal", ("refusal:protected_slot_unplaced",), _protected_unplaced),
    Intent("refusal:evidence_required", "refusal", ("refusal:evidence_required", "recovery:request_context",
                                                    "recovery:precompute_summary", "recovery:retrieve_narrower"),
           _evidence_required),
    Intent("refusal:protected_content_over_budget", "refusal", ("refusal:protected_content_over_budget",),
           _protected_over),
    Intent("refusal:slot_floor_over_budget", "refusal", ("refusal:slot_floor_over_budget",), _slot_floor),
    Intent("rows:producer", "rows", ("stage:producer",), _producer_rows),
    Intent("rows:duplicate-ids", "rows", tuple(f"reason:duplicate_item_id@{s}" for s in
                                               ("evidence.knowledge", "interaction.memory", "state.user")), _duplicate_ids),
    Intent("rows:unnamed", "rows", ("reason:invalid_structure@evidence.knowledge",
                                    "reason:missing_field:id@evidence.knowledge"), _unnamed),
    Intent("layout:twice", "layout", ("layout:twice",), _twice),
    Intent("layout:messages", "layout", ("layout:messages",), _messages_layout),
    Intent("edge:ids", "edge", ("edge:ids",), _edge_ids),
    Intent("edge:bodies", "edge", ("edge:bodies",), _edge_bodies),
    Intent("edge:numbers", "edge", ("reason:below_threshold@evidence.knowledge",), _edge_numbers),
    Intent("edge:instants", "edge", ("reason:future_freshness@evidence.tool_results", "reason:expired@interaction.memory"),
           _edge_instants),
    Intent("edge:capabilities", "edge", ("reason:capability_not_allowed@governance.capabilities",
                                         "included:governance.capabilities"), _capabilities),
    Intent("edge:defaults", "edge", ("defaults_filled",), _defaults),
    Intent("edge:history", "edge", ("included:interaction.history",), _history),
    Intent("edge:scope", "edge", tuple(f"reason:out_of_scope@{s}" for s in
                                       ("evidence.knowledge", "interaction.memory", "state.user")), _scope),
]
FAULT_INTENTS = [_fault_intent(f, s) for f in FAULTS for s in (f.slots or (None,))]
COMPONENTS = [Intent(f"component:{r}|{t}", "component", (f"component:{r}|{t}",), lambda g: None)
              for r in RENDERERS for t in TOKENIZER_IDS]
INTENTS = {i.name: i for i in FEATURES + FAULT_INTENTS + COMPONENTS}
FAMILY_WEIGHT = {"fault": 3.0, "conflict": 1.5, "pipeline": 1.5, "fit": 2.0, "refusal": 1.5, "rows": 1.0,
                 "layout": 0.7, "edge": 1.5}


def targets() -> list[str]:
    """Every tag some intent aims at: the universe coverage steering tries to fill."""
    return sorted({t for i in INTENTS.values() for t in i.targets})


# Assembly ------------------------------------------------------------------------------------------------------------


def _layout(g: Gen) -> None:
    """Make the profile realizable by the renderer: every wrap xml: for fixture-xml; for the message renderers,
    optionally governance slots in system and capabilities in tools, every system placement first (R-7)."""
    b, rng = g.b, g.rng
    if g.renderer == "fixture-xml/v1" or not g.messages_layout:
        return
    system, rest = [], []
    for placement in b.placement:
        slot = placement["slot"]
        if slot.startswith("governance.") and slot != "governance.capabilities" and rng.random() < 0.7:
            system.append({"slot": slot, "wrap": "system"})
            if rng.random() < 0.2:
                rest.append(placement)  # placed twice: system and xml render the body differently
        elif slot == "governance.capabilities" and rng.random() < 0.7:
            rest.append({"slot": slot, "wrap": "tools"})
        else:
            rest.append(placement)
    b.placement = system + rest


def _estimate(g: Gen) -> int:
    """Roughly the payload's count if every item renders once: enough to aim a budget near the boundary."""
    count = TOKENIZERS[g.tokenizer]
    total = 0
    for item in g.b.items():
        body = item.get("body")
        if isinstance(body, str):
            total += count(f'<x id="{item.get("id")}">\n{escape_body(body)}\n</x>\n')
    return total


def _finish(g: Gen) -> dict:
    b = g.b
    b.renderer, b.tokenizer = g.renderer, g.tokenizer
    _layout(g)
    if g.ratio is not None:
        b.budget["input"] = max(0, int(_estimate(g) * g.ratio) + g.rng.randint(-2, 2))
    if b.capabilities is not None:
        b.capabilities["allowed_ids"] = list(dict.fromkeys(b.capabilities["allowed_ids"]))
    return b.snapshot()


def _choose(rng: random.Random, intents: list[Intent], weights: dict[str, float]) -> Intent:
    return rng.choices(intents, [weights.get(i.name, 1.0) for i in intents])[0]


def generate(contract: Contract, rng: random.Random, case_id: str, weights: dict[str, float] | None = None,
             family_weights: dict[str, float] | None = None) -> Generated:
    """One valid snapshot. `weights` scales intents by name and `family_weights` families (steer.py makes both)."""
    weights = weights or {}
    family_weights = {**FAMILY_WEIGHT, **(family_weights or {})}
    for _ in range(20):
        b = Builder(contract, case_id)
        g = Gen(contract, rng, b, used={"gov:base", "q:base", "fmt:base"})
        b.base(parser=rng.random() < 0.15)
        component = _choose(rng, COMPONENTS, weights)
        g.renderer, g.tokenizer = component.name.removeprefix("component:").split("|")
        chosen = [component.name]
        for _ in range(rng.randint(0, 5)):  # background items in random slots
            g.item(rng.choice(SLOTS))
        picks = []
        families = list(family_weights)
        for _ in range(1 + min(4, int(rng.expovariate(0.9)))):
            family = rng.choices(families, [family_weights[f] for f in families])[0]
            pool = FAULT_INTENTS if family == "fault" else [i for i in FEATURES if i.family == family]
            picks.append(_choose(rng, pool, weights))
        picks.sort(key=lambda i: i.family == "fault")  # faults last: some change the route or the grant for good
        try:
            for intent in picks:
                intent.apply(g)
                chosen.append(intent.name)
        except NotConstructible:
            continue
        snapshot = _finish(g)
        if rng.random() < RESPELL:  # the same value in another JSON surface: keys, whitespace, escapes, numbers
            data = spell(snapshot, rng, numbers=True, integers=True).encode("utf-8")
            chosen.append("surface:respelled")
        else:
            data = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        found = validity.problems(contract, data)
        if found:
            raise GeneratorError(f"{case_id}: generated an invalid snapshot ({found[0][0]}: {found[0][1]}); "
                                 f"intents {chosen}")
        return Generated(case_id, data, tuple(chosen))
    raise GeneratorError(f"{case_id}: no constructible combination in 20 tries")


def corpus(contract: Contract, seed: int | str, count: int, prefix: str = "fuzz") -> list[Generated]:
    """`count` unsteered snapshots, each from its own generator seeded by (seed, index)."""
    return [generate(contract, random.Random(f"cwa-fuzz:{seed}:{i}"), f"{prefix}-{i:05d}") for i in range(count)]

