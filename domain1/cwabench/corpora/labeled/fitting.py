"""labeled.fitting: fitting decisions small enough to label exactly (SPEC.md R-16, R-18; conformance/README.md,
Fitting). Every budget is computed from the payload's exact count, so each case is one token from a different
answer: one token more and nothing would be reduced, one less and something else would go.

Ranks: the slot's order_by keys, default -relevance then -freshness, then id; the first item ranks highest and the
lowest-ranked sheds first. Slots shed by ascending priority (default 0), then by name.
"""
from __future__ import annotations

from ...contract import Contract
from . import Label, compressed, excluded, kept, labeled
from .builder import Builder, payload_tokens
from .faults import recorded_id

CORPUS = "labeled.fitting"
KB, EX, SU, H = "evidence.knowledge", "governance.examples", "state.user", "interaction.history"


def words(n: int, seed: str) -> str:
    return " ".join(f"{seed}{i}" for i in range(n))


def variant(item_id: str, n: int, suffix: str) -> dict:
    return {"id": f"{item_id}~{suffix}", "body": words(n, f"{suffix}-"), "method": "summary", "lineage": "summarised"}


def build(contract: Contract):
    cases = []

    def case(name, note, rules=("R-16",)):
        def wrap(setup):
            b = Builder(contract, name)
            b.base()
            fates = setup(b)
            label = Label(fates={recorded_id(b, i): kept() for i in b.items()}, notes=note)
            label.fates.update(fates)
            cases.append(labeled(CORPUS, name, b.bytes(), label, rules))
            return setup
        return wrap

    def short(b, by: int) -> None:
        b.budget["input"] = payload_tokens(b) - by

    @case("droppable-oldest-first", "one token over: the lowest-ranked droppable item, the oldest, goes")
    def _(b):
        b.item("ex:old", EX, freshness="2026-09-22T11:50:00Z")
        b.item("ex:new", EX, freshness="2026-09-22T11:59:00Z")
        short(b, 1)
        return {"ex:old": excluded("over_budget", slot=EX)}

    @case("droppable-slots-by-name", "equal priority: governance.examples sheds before state.user")
    def _(b):
        b.item("ex:a", EX)
        b.item("su:a", SU)
        short(b, 1)
        return {"ex:a": excluded("over_budget", slot=EX)}

    @case("droppable-slots-by-priority", "a lower priority sheds first, whatever the name")
    def _(b):
        b.rule(SU, priority=-1)
        b.item("ex:a", EX)
        b.item("su:a", SU)
        short(b, 1)
        return {"su:a": excluded("over_budget", slot=SU)}

    @case("droppable-before-compressible", "every droppable item goes before any compressible one is reduced")
    def _(b):
        b.item("ex:a", EX)
        b.item("kb:a", KB, relevance=0.9)
        b.item("kb:b", KB, relevance=0.8)
        short(b, 8)  # the example frees 7, so one knowledge chunk must go too: the lower-ranked
        return {"ex:a": excluded("over_budget", slot=EX), "kb:b": excluded("over_budget", slot=KB)}

    @case("compressible-lowest-rank-first", "with no variants, compressible items go from the lowest rank up")
    def _(b):
        for item_id, score in (("kb:a", 0.9), ("kb:b", 0.8), ("kb:c", 0.7)):
            b.item(item_id, KB, relevance=score)
        short(b, 8)
        return {"kb:c": excluded("over_budget", slot=KB), "kb:b": excluded("over_budget", slot=KB)}

    @case("variant-most-tokens-that-fits", "compress picks the longest variant that makes the payload fit", ("R-16", "R-18"))
    def _(b):
        b.item("kb:v", KB, words(10, "w"), variants=[variant("kb:v", 8, "v8"), variant("kb:v", 2, "v2")])
        short(b, 2)
        return {"kb:v": compressed("kb:v~v8")}

    @case("variant-fewest-when-none-fits", "no variant makes it fit: compress takes the shortest, then omission "
          "continues from the lowest rank", ("R-16", "R-18"))
    def _(b):
        b.item("kb:v", KB, words(10, "w"), relevance=0.9, variants=[variant("kb:v", 8, "v8"), variant("kb:v", 2, "v2")])
        b.item("kb:w", KB, relevance=0.8)
        short(b, 9)  # v2 frees 8; kb:w, the lowest-ranked, then goes
        return {"kb:v": compressed("kb:v~v2"), "kb:w": excluded("over_budget", slot=KB)}

    @case("variant-tie-earlier-wins", "variants of equal length: the earlier in variants wins", ("R-16", "R-18"))
    def _(b):
        b.item("kb:v", KB, words(10, "w"), variants=[variant("kb:v", 6, "first"), variant("kb:v", 6, "second")])
        short(b, 4)
        return {"kb:v": compressed("kb:v~first")}

    @case("token-budget-caps", "an item over its token_budget is reduced whether or not the payload fits: the "
          "longest variant within the cap, else omitted; a droppable item is omitted", ("R-16", "R-18"))
    def _(b):
        b.item("kb:capped", KB, words(10, "w"), token_budget=5, relevance=0.9,
               variants=[variant("kb:capped", 8, "v8"), variant("kb:capped", 4, "v4"), variant("kb:capped", 3, "v3")])
        b.item("kb:nofit", KB, words(10, "w"), token_budget=2, relevance=0.8, variants=[variant("kb:nofit", 4, "v4")])
        b.item("ex:capped", EX, token_budget=2)
        return {"kb:capped": compressed("kb:capped~v4"), "kb:nofit": excluded("over_budget", slot=KB),
                "ex:capped": excluded("over_budget", slot=EX)}

    @case("slot-max-tokens-omits", "a slot over its max_tokens reduces its own items, the budget notwithstanding")
    def _(b):
        b.rule(KB, max_tokens=9)
        for item_id, score in (("kb:a", 0.9), ("kb:b", 0.8), ("kb:c", 0.7)):
            b.item(item_id, KB, relevance=score)  # 4 tokens each: 12 > 9
        return {"kb:c": excluded("over_budget", slot=KB)}

    @case("slot-max-tokens-compresses-first", "the slot's compress step comes before its omit step", ("R-16", "R-18"))
    def _(b):
        b.rule(KB, max_tokens=10)
        b.item("kb:a", KB, relevance=0.9)
        b.item("kb:b", KB, relevance=0.8)
        b.item("kb:c", KB, relevance=0.7, variants=[variant("kb:c", 2, "v2")])
        return {"kb:c": compressed("kb:c~v2")}

    @case("default-order-compresses-by-slot-name", "without a fitting_order, compress steps run in shedding order: "
          "evidence.knowledge before interaction.history", ("R-16", "R-18"))
    def _(b):
        b.item("h:old", H, freshness="2026-09-22T11:50:00Z")
        b.item("kb:v", KB, words(10, "w"), variants=[variant("kb:v", 8, "v8")])
        short(b, 2)
        return {"kb:v": compressed("kb:v~v8")}

    @case("route-fitting-order-first", "the route's fitting_order steps come before the default ones", ("R-16",))
    def _(b):
        b.route["fitting_order"] = [{"slot": H, "action": "omit"}]
        b.item("h:old", H, freshness="2026-09-22T11:50:00Z")
        b.item("kb:v", KB, words(10, "w"), variants=[variant("kb:v", 8, "v8")])
        short(b, 2)
        return {"h:old": excluded("over_budget", slot=H)}

    @case("floor-holds-droppable", "a slot at its min_tokens floor keeps its droppable item while knowledge "
          "compresses", ("R-16", "R-18"))
    def _(b):
        b.rule(EX, min_tokens=4)
        b.item("ex:a", EX)  # 4 tokens: omitting it would break the floor
        b.item("kb:v", KB, words(10, "w"), variants=[variant("kb:v", 8, "v8")])
        short(b, 2)
        return {"kb:v": compressed("kb:v~v8")}

    @case("margin-charges-the-count", "the payload fits only when its count with margin_percent, rounded up, is at "
          "most budget.input", ("R-16",))
    def _(b):
        b.item("ex:a", EX)
        total = payload_tokens(b)
        b.budget["input"] = total
        b.budget["margin_percent"] = 10
        assert (total * 110 + 99) // 100 > total >= ((total - 7) * 110 + 99) // 100, "margin case is not tight"
        return {"ex:a": excluded("over_budget", slot=EX)}

    @case("exactly-fits", "a payload exactly at budget.input fits: nothing is reduced", ("R-16",))
    def _(b):
        b.item("ex:a", EX)
        b.item("kb:v", KB, words(10, "w"), variants=[variant("kb:v", 8, "v8")])
        short(b, 0)
        return {}

    return cases
