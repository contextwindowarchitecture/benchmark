"""labeled.pipeline: conflict resolution, supersession, deduplication and the source-diversity cap, each alone and
where their order changes the answer (SPEC.md R-11, R-24, R-25, R-26; conformance/README.md, Supersession,
Deduplication, Source diversity, Fitting's rank).

Rank, wherever a stage needs one, is the slot's order_by keys (default -relevance, -freshness) and then id, with the
first item ranked highest. Tool results here carry no relevance, so they rank by freshness and then id.
"""
from __future__ import annotations

from ...contract import Contract
from . import Label, excluded, kept, labeled
from .builder import Builder
from .faults import recorded_id

CORPUS = "labeled.pipeline"
KB, TR = "evidence.knowledge", "evidence.tool_results"
BY_AUTHORITY = {"decided_by": "authority", "resolution": "resolved", "winner": "gov:base"}


def _named(b: Builder, item_id: str, group: str = "g:named") -> dict:
    """Name an item in a conflict group that excludes nothing: its only instructing member is the base instruction,
    which wins by authority. Being named exempts the item from supersession, deduplication and the cap."""
    b.group(group, "instruction", [item_id, "gov:base"])
    return {group: dict(BY_AUTHORITY)}


def build(contract: Contract):
    cases = []

    def case(name, note, rules):
        def wrap(setup):
            b = Builder(contract, name)
            b.base()
            fates, conflicts = setup(b)
            label = Label(fates={recorded_id(b, i): kept() for i in b.items()}, conflicts=conflicts or {}, notes=note)
            label.fates.update(fates)
            cases.append(labeled(CORPUS, name, b.bytes(), label, rules))
            return setup
        return wrap

    @case("supersede-basic", "older observations from the same producer and source go; another source stays", ("R-25",))
    def _(b):
        b.rule(TR, supersede="source")
        b.item("obs:a1", TR, source="src:A", freshness="2026-09-22T11:57:00Z")
        b.item("obs:a2", TR, source="src:A", freshness="2026-09-22T11:58:00Z")
        b.item("obs:a3", TR, source="src:A", freshness="2026-09-22T11:59:00Z")
        b.item("obs:b1", TR, source="src:B", freshness="2026-09-22T11:58:00Z")
        return {"obs:a1": excluded("superseded", superseded_by="obs:a3", slot=TR),
                "obs:a2": excluded("superseded", superseded_by="obs:a3", slot=TR)}, None

    @case("supersede-equal-instants", "the latest items tie however they are written, and all stay; the older one "
          "names the highest-ranked of them, by id", ("R-25", "R-2"))
    def _(b):
        b.rule(TR, supersede="source")
        b.item("obs:q", TR, source="src:A", freshness="2026-09-22T13:58:00+02:00")
        b.item("obs:p", TR, source="src:A", freshness="2026-09-22T11:58:00.000Z")
        b.item("obs:r", TR, source="src:A", freshness="2026-09-22T11:57:59.999999Z")
        return {"obs:r": excluded("superseded", superseded_by="obs:p", slot=TR)}, None

    @case("supersede-per-producer", "the same source from two producers is two calls (R-15: source alone proves "
          "nothing)", ("R-25",))
    def _(b):
        b.rule(TR, supersede="source")
        b.item("obs:old", TR, source="src:A", freshness="2026-09-22T11:50:00Z", producer="tools")
        b.item("kbobs:new", TR, source="src:A", freshness="2026-09-22T11:59:00Z", producer="kb")
        return {}, None

    @case("supersede-ignores-bodies-and-versions", "source and freshness decide alone", ("R-25",))
    def _(b):
        b.rule(TR, supersede="source")
        b.item("obs:old", TR, "Same words.", source="src:A", source_version="9", freshness="2026-09-22T11:50:00Z")
        b.item("obs:new", TR, "Other words.", source="src:A", source_version="1", freshness="2026-09-22T11:59:00Z")
        return {"obs:old": excluded("superseded", superseded_by="obs:new", slot=TR)}, None

    @case("supersede-exempts-named", "an older observation a conflict group names stays", ("R-25", "R-11"))
    def _(b):
        b.rule(TR, supersede="source")
        b.item("obs:old", TR, source="src:A", freshness="2026-09-22T11:50:00Z")
        b.item("obs:new", TR, source="src:A", freshness="2026-09-22T11:59:00Z")
        return {}, _named(b, "obs:old")

    @case("supersede-exempts-protected", "tool results the route protects are never superseded", ("R-25", "R-16"))
    def _(b):
        b.rule(TR, supersede="source")
        b.route["tier_upgrades"] = {TR: "protected"}
        b.item("obs:old", TR, source="src:A", freshness="2026-09-22T11:50:00Z")
        b.item("obs:new", TR, source="src:A", freshness="2026-09-22T11:59:00Z")
        return {}, None

    @case("supersede-only-where-asked", "a slot without supersede keeps every observation", ("R-25",))
    def _(b):
        b.item("obs:old", TR, source="src:A", freshness="2026-09-22T11:50:00Z")
        b.item("obs:new", TR, source="src:A", freshness="2026-09-22T11:59:00Z")
        return {}, None

    @case("dedupe-basic", "equal bodies after whitespace is collapsed: the lower-ranked goes", ("R-24",))
    def _(b):
        b.rule(KB, dedupe="exact")
        b.item("kb:hi", KB, "Pro plan includes  support.", relevance=0.95)
        b.item("kb:lo", KB, " Pro plan includes support. ", relevance=0.9)
        return {"kb:lo": excluded("duplicate_content", duplicate_of="kb:hi", slot=KB)}, None

    @case("dedupe-whitespace-set", "every ECMAScript whitespace run collapses (U+3000, U+00A0, U+FEFF, tab); U+001C "
          "is not whitespace", ("R-24",))
    def _(b):
        b.rule(KB, dedupe="exact")
        b.item("kb:w1", KB, "alpha bravo charlie", relevance=0.95)
        b.item("kb:w2", KB, "alpha　 bravo\tcharlie﻿", relevance=0.9)
        b.item("kb:w3", KB, "alpha\u001cbravo charlie", relevance=0.85)
        return {"kb:w2": excluded("duplicate_content", duplicate_of="kb:w1", slot=KB)}, None

    @case("dedupe-no-normalization", "no Unicode normalization and no case folding", ("R-24",))
    def _(b):
        b.rule(KB, dedupe="exact")
        b.item("kb:nfc", KB, "café refund", relevance=0.95)
        b.item("kb:nfd", KB, "café refund", relevance=0.9)
        b.item("kb:up", KB, "Refund window", relevance=0.85)
        b.item("kb:low", KB, "refund window", relevance=0.8)
        return {}, None

    @case("dedupe-rank-ties-by-id", "equal scores and freshness: the first id ranks highest and stays", ("R-24",))
    def _(b):
        b.rule(KB, dedupe="exact")
        b.item("kb:b", KB, "Same body.", relevance=0.9)
        b.item("kb:a", KB, "Same body.", relevance=0.9)
        return {"kb:b": excluded("duplicate_content", duplicate_of="kb:a", slot=KB)}, None

    @case("dedupe-per-slot", "bodies are never compared across slots", ("R-24",))
    def _(b):
        b.rule(KB, dedupe="exact")
        b.rule(TR, dedupe="exact")
        b.item("kb:x", KB, "Shared body.")
        b.item("obs:x", TR, "Shared body.")
        return {}, None

    @case("dedupe-only-where-asked", "a slot without dedupe keeps equal bodies", ("R-24",))
    def _(b):
        b.item("h:1", "interaction.history", "Thanks.", freshness="2026-09-22T11:58:00Z")
        b.item("h:2", "interaction.history", "Thanks.", freshness="2026-09-22T11:59:00Z")
        return {}, None

    @case("dedupe-exempt-member-is-kept", "a set with a named member keeps it, even when it ranks lower, and the "
          "other names it", ("R-24", "R-11"))
    def _(b):
        b.rule(KB, dedupe="exact")
        b.item("kb:named", KB, "Same body.", relevance=0.6)
        b.item("kb:top", KB, "Same body.", relevance=0.95)
        return {"kb:top": excluded("duplicate_content", duplicate_of="kb:named", slot=KB)}, _named(b, "kb:named")

    @case("dedupe-all-protected", "protected items are never deduplicated", ("R-24", "R-16"))
    def _(b):
        b.rule(KB, dedupe="exact")
        b.route["tier_upgrades"] = {KB: "protected"}
        b.item("kb:a", KB, "Same body.", relevance=0.9)
        b.item("kb:b", KB, "Same body.", relevance=0.8)
        return {}, None

    @case("supersede-before-dedupe", "a superseded item takes no part in deduplication, so its twin from another "
          "source stays", ("R-24", "R-25"))
    def _(b):
        b.rule(TR, supersede="source", dedupe="exact")
        b.item("obs:a1", TR, "Body X.", source="src:A", freshness="2026-09-22T11:50:00Z")
        b.item("obs:a2", TR, "Body Y.", source="src:A", freshness="2026-09-22T11:59:00Z")
        b.item("obs:c", TR, "Body X.", source="src:C", freshness="2026-09-22T11:40:00Z")
        return {"obs:a1": excluded("superseded", superseded_by="obs:a2", slot=TR)}, None

    @case("dedupe-before-diversity", "a duplicate is gone before the cap counts, so the cap removes the next item "
          "instead", ("R-24", "R-26"))
    def _(b):
        b.rule(KB, dedupe="exact", max_per_source=1)
        b.item("kb:a", KB, "Body X.", source="src:S", relevance=0.9)
        b.item("kb:b", KB, "Body X.", source="src:S", relevance=0.8)
        b.item("kb:c", KB, "Body Z.", source="src:S", relevance=0.7)
        return {"kb:b": excluded("duplicate_content", duplicate_of="kb:a", slot=KB),
                "kb:c": excluded("source_diversity_cap", slot=KB)}, None

    @case("diversity-basic", "each source keeps its highest-ranked items up to the cap", ("R-26",))
    def _(b):
        b.rule(KB, max_per_source=2)
        for n, score in enumerate((0.9, 0.8, 0.7, 0.6)):
            b.item(f"kb:s{n}", KB, source="src:S", relevance=score)
        b.item("kb:t0", KB, source="src:T", relevance=0.5)
        return {"kb:s2": excluded("source_diversity_cap", slot=KB),
                "kb:s3": excluded("source_diversity_cap", slot=KB)}, None

    @case("diversity-per-producer", "the same source from two producers is two sources", ("R-26",))
    def _(b):
        b.route["producers"]["kb2"] = {"kind": "retrieval", "slots": [KB]}
        b.rule(KB, max_per_source=1)
        b.item("kb:one", KB, source="src:S", relevance=0.9)
        b.item("kb2:one", KB, source="src:S", relevance=0.8, producer="kb2")
        return {}, None

    @case("diversity-exempt-first", "a named item takes its source's place first, even beyond the cap",
          ("R-26", "R-11"))
    def _(b):
        b.rule(KB, max_per_source=1)
        b.item("kb:named", KB, source="src:S", relevance=0.5)
        b.item("kb:top", KB, source="src:S", relevance=0.9)
        return {"kb:top": excluded("source_diversity_cap", slot=KB)}, _named(b, "kb:named")

    @case("conflict-before-supersede", "a newer observation that loses its fact group is gone before supersession, "
          "so the older one is the latest of its call and stays", ("R-11", "R-25"))
    def _(b):
        b.rule(TR, supersede="source")
        b.route["facts"] = {"plan_price": {"precedence": ["kb"], "on_unresolved": "refuse"}}
        b.item("kb:price", KB, "Pro costs 20 EUR.")
        b.item("obs:old", TR, "Pro costs 18 EUR.", source="src:A", freshness="2026-09-22T11:50:00Z")
        b.item("obs:new", TR, "Pro costs 25 EUR.", source="src:A", freshness="2026-09-22T11:59:00Z")
        b.group("f:price", "fact", ["kb:price", "obs:new"], fact="plan_price")
        return {"obs:new": excluded("conflict_lost", slot=TR)}, {
            "f:price": {"decided_by": "policy", "resolution": "resolved", "winner": "kb:price"}}

    return cases
