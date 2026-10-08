"""labeled.boundaries: items at the edge of an admission rule, on the side the rule admits, with a few on the
excluding side where the edge is easy to get wrong. A false exclusion here is as wrong as a missed one."""
from __future__ import annotations

from ...contract import Contract
from . import Label, excluded, kept, labeled
from .builder import Builder
from .faults import recorded_id

CORPUS = "labeled.boundaries"


def _case(contract, name, setup, note, rules=("R-2", "R-3")):
    b = Builder(contract, name)
    b.base()
    special = setup(b) or {}
    fates = {recorded_id(b, i): kept() for i in b.items()}
    fates.update(special)
    return labeled(CORPUS, name, b.bytes(), Label(fates=fates, notes=note), rules)


def build(contract: Contract):
    cases = []

    def add(name, note, rules=("R-2", "R-3")):
        def wrap(setup):
            cases.append(_case(contract, name, setup, note, rules))
            return setup
        return wrap

    @add("skew-edge-admitted", "freshness exactly clock_skew_seconds after assembly_time is admitted (R-2)")
    def _(b):
        b.route["clock_skew_seconds"] = 5
        b.item("x:skew", "evidence.knowledge", freshness="2026-09-22T12:00:05Z")

    @add("skew-past-edge-excluded", "one microsecond past clock_skew_seconds is future_freshness (R-2)")
    def _(b):
        b.route["clock_skew_seconds"] = 5
        b.item("x:skew", "evidence.knowledge", freshness="2026-09-22T12:00:05.000001Z")
        return {"x:skew": excluded("future_freshness", slot="evidence.knowledge")}

    @add("expiry-after-instant-admitted", "expires one nanosecond after assembly_time is admitted (R-9)", ("R-9",))
    def _(b):
        b.item("m:later", "interaction.memory", expires="2026-09-22T12:00:00.000000001Z")

    @add("expiry-equal-instant-other-offset", "expires equal to assembly_time written with another offset is "
         "expired (R-2, R-9)", ("R-2", "R-9"))
    def _(b):
        b.item("m:now", "interaction.memory", expires="2026-09-22T07:00:00-05:00")
        return {"m:now": excluded("expired", slot="interaction.memory")}

    @add("relevance-equal-threshold-admitted", "a score equal to min_relevance passes (R-13)", ("R-13",))
    def _(b):
        b.rule("evidence.knowledge", min_relevance=0.8)
        b.item("kb:edge", "evidence.knowledge", relevance=0.8)

    @add("max-age-equal-admitted", "observed exactly max_age_seconds ago is not longer ago (R-3)")
    def _(b):
        b.rule("evidence.knowledge", max_age_seconds=3600)
        b.item("kb:hour", "evidence.knowledge", freshness="2026-09-22T11:00:00Z")

    @add("state-max-age-equal-admitted", "state observed exactly max_age_seconds ago is current (R-8)", ("R-8",))
    def _(b):
        b.rule("state.task", max_age_seconds=60)
        b.item("st:minute", "state.task", freshness="2026-09-22T11:59:00Z")

    @add("verified-mcp-unmarked-admitted", "output from an MCP server the route verifies may be unmarked (R-10, R-15)",
         ("R-10", "R-15"))
    def _(b):
        b.route["producers"]["tools"]["verified"] = True
        b.item("obs:verified", "evidence.tool_results", injection_risk="none")

    @add("untrusted-where-allowed", "tool results, memory and history may carry untrusted (R-1)", ("R-1",))
    def _(b):
        b.item("obs:u", "evidence.tool_results", authority="untrusted")
        b.item("m:u", "interaction.memory", authority="untrusted")
        b.item("h:u", "interaction.history", authority="untrusted")

    @add("generated-turn-untrusted-admitted", "a prior model turn carries untrusted (R-1)", ("R-1", "R-7"))
    def _(b):
        b.item("h:model", "interaction.history", lineage="generated", authority="untrusted")

    @add("route-raised-tier-may-be-lowered", "in a slot only the route raised, an item may lower its own tier (R-16)",
         ("R-16",))
    def _(b):
        b.route["tier_upgrades"] = {"state.user": "protected"}
        b.item("su:low", "state.user", tier="droppable")

    @add("upgrade-at-or-below-default-changes-nothing", "a tier_upgrades value below the slot's default leaves it "
         "protected, so lowering an item there is still protected_tier_changed (R-16)", ("R-16",))
    def _(b):
        b.route["tier_upgrades"] = {"state.task": "compressible"}
        b.item("st:low", "state.task", tier="compressible")
        return {"st:low": excluded("protected_tier_changed", slot="state.task")}

    @add("own-tier-equal-to-slot-admitted", "an item may state its slot's own tier (R-16)", ("R-16",))
    def _(b):
        b.item("kb:same", "evidence.knowledge", tier="compressible")
        b.item("ex:same", "governance.examples", tier="droppable")

    @add("unscoped-item-admitted", "an item carrying no scope keys, in a slot that requires none, is in scope (R-2)")
    def _(b):
        b.item("kb:noscope", "evidence.knowledge", omit=("scope",))

    @add("defaults-filled", "an item omitting every policy field is admitted with the slot's defaults (R-3)",
         ("R-3", "R-22"))
    def _(b):
        for slot, id in (("evidence.knowledge", "kb:bare"), ("interaction.history", "h:bare"),
                         ("governance.examples", "ex:bare"), ("interaction.memory", "m:bare")):
            b.item(id, slot, omit=("token_budget", "variants", "conflict_policy", "lineage", "eligibility",
                                   "injection_risk"))

    @add("route-override-fills-defaults", "a route's default_overrides fill omitted fields before slot defaults (R-3)",
         ("R-3",))
    def _(b):
        b.route["default_overrides"] = {"evidence.knowledge": {"eligibility": "route says so", "token_budget": 400}}
        b.item("kb:over", "evidence.knowledge", omit=("eligibility", "token_budget"))

    @add("control-character-id-is-ordinary", "an id of U+001C is not blank, so it is an ordinary id (R-2)", ("R-2",))
    def _(b):
        b.item("\u001c", "evidence.knowledge")

    @add("bom-id-is-blank", "an id of U+FEFF alone is blank: recorded by producer and position, invalid_structure "
         "(R-2)", ("R-2",))
    def _(b):
        b.item("﻿", "evidence.knowledge")
        return {"kb#invalid-0": excluded("invalid_structure", slot="evidence.knowledge")}

    @add("producer-rows-from-unadmitted-batch", "an unlisted producer's own exclusion rows still reach the trace "
         "(R-9); its candidate is producer_not_authenticated", ("R-9", "R-15"))
    def _(b):
        b.add(dict(b.item("rogue:1", "evidence.knowledge", producer="kb")), "rogue", "retrieval")
        b.batches["kb"]["items"] = [i for i in b.batches["kb"]["items"] if i.get("id") != "rogue:1"]
        b.exclude("rogue", "rogue:0", "below_threshold")
        return {"rogue:1": excluded("producer_not_authenticated", slot="evidence.knowledge")}

    return cases
