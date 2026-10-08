"""Scale snapshots for S7 (domain-1-plan.md, 7.7): a shape, a number of candidates, a candidate token total, a
tokenizer and a renderer, then a budget.

A cell is a pure function of its parameters and the seed, so rows store parameters and hashes, not the snapshots,
which reach tens of megabytes; `build(Cell(...))` gives the same bytes again.

Each snapshot comes with what the harness computes about it without assembling, from the rules and its own
renderer (canon/render.py):
    full        the charged count with every item rendered whole: the size budget ratios are taken of
    protected   the charged count of the protected items alone. No budget below it can assemble, and with no floor
                every budget at or above it does (Fitting, step 1)
    floor       for the floors shape, the charged count of the protected items and the floored slot: between
                `protected` and this the answer is slot_floor_over_budget, and from it up the snapshot assembles
So every cell's outcome and refusal code is known before any adapter runs.

Bodies are words of three ASCII letters, so a body of t words counts t under fixture-whitespace/v1 and, at 4t − 1
bytes, t under estimate-utf8/v1: a cell's bodies are the same text under either tokenizer.
"""
from __future__ import annotations

import json
import random
from dataclasses import dataclass, field, replace
from functools import cached_property

from ..canon import render as render_mod
from ..contract import Contract
from .labeled.builder import Builder

KB, TR, EX, SU, H = "evidence.knowledge", "evidence.tool_results", "governance.examples", "state.user", \
    "interaction.history"
TOKENIZERS = ("fixture-whitespace/v1", "estimate-utf8/v1")
RENDERERS = ("fixture-xml/v1", "cwa-messages/v1", "cwa-message-blocks/v1")
MIN_BODY = 4  # a cell whose items would average fewer body tokens is not constructible
LETTERS = "abcdefghijklmnopqrstuvwxyz"
VOCAB = [a + b + c for a in "bcdfgklmnprstv" for b in "aeiou" for c in "bdgklmnprst"]

# Each shape: (description, the share of items in each slot, how many variants a compressible item gets, extras).
SHAPES = {
    "droppable-heavy": ("70% droppable examples and user state, 30% knowledge with no variants",
                        {EX: 0.45, SU: 0.25, KB: 0.30}, 0, {}),
    "compressible-v0": ("knowledge and tool results with no variants", {KB: 0.8, TR: 0.2}, 0, {}),
    "compressible-v1": ("knowledge and tool results with one variant each, about 40% of the body",
                        {KB: 0.8, TR: 0.2}, 1, {}),
    "compressible-v3": ("knowledge and tool results with three variants each, about 60%, 30% and 10% of the body",
                        {KB: 0.8, TR: 0.2}, 3, {}),
    "floors": ("90% knowledge with one variant; 10% user state whose slot min_tokens is its whole size, so it is "
               "frozen by its first reduction and the snapshot refuses slot_floor_over_budget below its floor",
               {KB: 0.9, SU: 0.1}, 1, {"floor": SU}),
    "slot-caps": ("knowledge and tool results with one variant, each slot capped by max_tokens at 30% of its size, "
                  "and 20% droppable examples", {KB: 0.6, TR: 0.2, EX: 0.2}, 1, {"caps": {KB: 0.3, TR: 0.3}}),
    "fitting-order": ("history, knowledge with one variant and examples, with the route's fitting_order omitting "
                      "history first and then compressing knowledge", {H: 0.3, KB: 0.5, EX: 0.2}, 1,
                      {"fitting_order": [{"slot": H, "action": "omit"}, {"slot": KB, "action": "compress"}]}),
    "surfaced-conflicts": ("knowledge with one variant, half of it in fact groups of three that no precedence "
                           "decides, so every group surfaces and its kept members render marked", {KB: 1.0}, 1,
                           {"conflicts": 0.5}),
}
VARIANT_SHARES = {0: (), 1: (0.4,), 3: (0.6, 0.3, 0.1)}


@dataclass(frozen=True)
class Cell:
    shape: str
    candidates: int
    tokens: int  # the target candidate token total; the exact full count is Built.full
    tokenizer: str = "fixture-whitespace/v1"
    renderer: str = "fixture-xml/v1"
    seed: int = 20261006

    @property
    def key(self) -> str:
        return (f"{self.shape}/n{self.candidates}/t{self.tokens}/{self.tokenizer.split('/')[0]}/"
                f"{self.renderer.split('/')[0]}")

    def as_json(self) -> dict:
        return {"shape": self.shape, "candidates": self.candidates, "candidate_tokens": self.tokens,
                "tokenizer": self.tokenizer, "renderer": self.renderer, "seed": self.seed}


class NotConstructible(Exception):
    pass


@dataclass
class Built:
    cell: Cell
    snapshot: dict  # budget.input is filled in by at()
    protected_items: list[dict]
    floor_items: list[dict]
    all_items: list[dict]
    surfaced: dict[str, str]
    margin_percent: int = 0
    _counts: dict = field(default_factory=dict)

    def _charged(self, items: list[dict], surfaced: dict[str, str] | None = None) -> int:
        rendered = render_mod.render(self.snapshot, items, surfaced=surfaced)
        return render_mod.charged(rendered.count(self.cell.tokenizer), self.margin_percent)

    @cached_property
    def full(self) -> int:
        return self._charged(self.all_items, self.surfaced)

    @cached_property
    def protected(self) -> int:
        return self._charged(self.protected_items)

    @cached_property
    def floor(self) -> int | None:
        return self._charged(self.protected_items + self.floor_items) if self.floor_items else None

    def expect(self, budget: int) -> tuple[str, str | None]:
        """The outcome and refusal code the rules give at `budget` (see the module docstring)."""
        if budget < self.protected:
            return "refused", "protected_content_over_budget"
        if self.floor is not None and budget < self.floor:
            return "refused", "slot_floor_over_budget"
        return "assembled", None

    def at(self, budget: int) -> bytes:
        self.snapshot["budget"]["input"] = max(1, int(budget))
        return json.dumps(self.snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def budget_for(self, ratio: float) -> int:
        return max(1, round(self.full * ratio))


def _body(rng: random.Random, tag: str, words: int) -> str:
    """`words` three-letter words, the first unique to the item."""
    return " ".join([tag] + rng.choices(VOCAB, k=max(0, words - 1)))


def _tag(n: int) -> str:
    """A unique three-letter tag for the nth body or variant (26^3 = 17,576; capitals extend it past that)."""
    alphabet = LETTERS + LETTERS.upper()
    return alphabet[n // 2704 % 52] + alphabet[n // 52 % 52] + alphabet[n % 52]


def build(contract: Contract, cell: Cell) -> Built:
    if cell.shape not in SHAPES:
        raise ValueError(f"unknown shape {cell.shape}")
    _, shares, n_variants, extras = SHAPES[cell.shape]
    rng = random.Random(f"{cell.seed}/{cell.key}")
    b = Builder(contract, f"s7-{cell.shape}")
    b.tokenizer, b.renderer = cell.tokenizer, cell.renderer
    if cell.renderer != "fixture-xml/v1":
        b.placement = [{"slot": p["slot"], "wrap": "system" if p["slot"] == "governance.instructions" else p["wrap"]}
                       for p in b.placement]
    b.item("gov:base", "governance.instructions", _body(rng, "gov", 40))
    b.item("q:base", "interaction.query", _body(rng, "qry", 12))
    protected = list(b.items())

    # How many tokens an item's wrapper costs, from a probe item rendered with and without a body.
    probe_snapshot = b.snapshot()
    probe = {"id": "kb:00000", "slot": KB, "body": "", "freshness": "2026-09-22T11:59:30Z"}
    empty = render_mod.render(probe_snapshot, [probe]).count(cell.tokenizer)
    one = render_mod.render(probe_snapshot, [{**probe, "body": "abc"}]).count(cell.tokenizer)
    overhead = max(0, one - 1) if cell.tokenizer == "fixture-whitespace/v1" else empty
    mean = cell.tokens / cell.candidates - overhead
    if mean < MIN_BODY:
        raise NotConstructible(f"{cell.candidates} candidates cannot share {cell.tokens} tokens: "
                               f"{mean:.1f} body tokens each, fewer than {MIN_BODY}")

    counts = {slot: int(cell.candidates * share) for slot, share in shares.items()}
    counts[next(iter(shares))] += cell.candidates - sum(counts.values())
    if extras.get("fitting_order"):
        b.route["fitting_order"] = extras["fitting_order"]
    serial, made = 0, []
    for slot, count in counts.items():
        prefix = {KB: "kb", TR: "tr", EX: "ex", SU: "su", H: "h"}[slot]
        for i in range(count):
            words = max(1, rng.randint(max(1, round(mean * 0.5)), max(1, round(mean * 1.5))))
            fields = {}
            if slot in (KB, TR, H) and n_variants:
                variants = []
                for k, share in enumerate(VARIANT_SHARES[n_variants]):
                    vw = max(1, round(words * share))
                    if vw < words and all(vw < len(v["body"].split()) for v in variants):
                        serial += 1
                        variants.append({"id": f"{prefix}:{i:05d}~v{k + 1}", "body": _body(rng, _tag(serial), vw),
                                         "method": "stub-lead", "lineage": "summarised"})
                fields["variants"] = variants
            if slot == KB:
                fields["relevance"] = round(rng.uniform(0.5, 1.0), 3)
            if slot == H:
                fields["freshness"] = f"2026-09-22T11:{rng.randint(0, 58):02d}:{rng.randint(0, 59):02d}Z"
            serial += 1
            made.append(b.item(f"{prefix}:{i:05d}", slot, _body(rng, _tag(serial), words), **fields))

    floor_items = []
    if extras.get("floor"):
        floor_slot = extras["floor"]
        floor_items = [i for i in made if i["slot"] == floor_slot]
        b.rule(floor_slot, min_tokens=max(1, _slot_size(floor_items, cell.tokenizer)))
    for slot, share in extras.get("caps", {}).items():
        mine = [i for i in made if i["slot"] == slot]
        if mine:
            b.rule(slot, max_tokens=max(1, round(_slot_size(mine, cell.tokenizer) * share)))
    surfaced = {}
    if extras.get("conflicts"):
        b.route["facts"] = {"claim": {"precedence": ["kb"], "on_unresolved": "surface"}}
        kb_items = [i for i in made if i["slot"] == KB]
        grouped = kb_items[: int(len(kb_items) * extras["conflicts"]) // 3 * 3]
        for g in range(len(grouped) // 3):
            members = [m["id"] for m in grouped[3 * g: 3 * g + 3]]
            b.group(f"f:{g:05d}", "fact", members, fact="claim")
            surfaced.update({m: f"f:{g:05d}" for m in members})

    snapshot = b.snapshot()
    return Built(cell, snapshot, protected, floor_items, protected + made, surfaced)


def _slot_size(items: list[dict], tokenizer: str) -> int:
    """A slot's size: the sum of its items' rendered bodies' counts (conformance/README.md, Fitting)."""
    from ..canon.payloads import render_body
    from ..canon.tokenizers import TOKENIZERS

    count = TOKENIZERS[tokenizer]
    return sum(count(render_body(i["body"], "xml")) for i in items)


def cells(settings: dict) -> list[Cell]:
    """The grid S7 runs: every shape × candidates × candidate tokens, plus each shape under every tokenizer and
    renderer at one size. Budgets come later, per ratio."""
    shapes = settings.get("shapes", list(SHAPES))
    seed = int(settings.get("seed", 20261006))
    out = [Cell(s, n, t, seed=seed) for s in shapes for n in settings.get("candidates", [10, 100, 500, 1000, 5000, 10000])
           for t in settings.get("candidate_tokens", [10000, 100000, 500000, 2000000])]
    n, t = settings.get("components_cell", [500, 100000])
    for s in shapes:
        for tokenizer in TOKENIZERS:
            for renderer in RENDERERS:
                cell = Cell(s, n, t, tokenizer, renderer, seed)
                if cell not in out:
                    out.append(cell)
    return out


def with_budget(cell: Cell, **changes) -> Cell:
    return replace(cell, **changes)
