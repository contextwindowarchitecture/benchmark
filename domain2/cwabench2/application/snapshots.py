"""The CWA arms' frozen snapshots, and what assembly must make of each (domain-2-plan.md, 4.3 and 7).

A **point** is where a conversation is assembled: `turn` k, whose query is user turn k with turns 1 to k − 1 as
history (a frame of the conversation, as it would have been sent), or `probe` after turn t, whose query is the probe's
question with turns 1 to t as history (what S2 sends to the model). Each arm freezes one snapshot per point. Its
budget is set per run budget (`Frozen.at`), so a point's snapshots at every budget share every other byte.

The arms are a ladder (7): `cwa-history` is every turn as history; `cwa-state` adds the oracle state writer;
`cwa-state-x` is `cwa-state` with the model-based extractor in place of the oracle (the realistic arm);
`cwa-memory` keeps the last `history_turns` turns and compacts the rest into memory; `cwa-pipeline` adds the route's
supersession and exact deduplication on history. `cwa-format` is the format control: the `window` baseline's exact
selection at each budget (baselines/), frozen as a cwa-history snapshot of only those messages and assembled at its
own full size, so it differs from the baseline in format alone.

**The prediction.** Every candidate is admissible by construction, so fitting alone decides what is kept, and its
rules fix it exactly (conformance/README.md, Fitting). The protected items (the instructions, the output contract,
state and the query) must fit or assembly refuses with protected_content_over_budget. History and memory are
compressible with no variants, so the only reductions are omissions: the omit step for interaction.history comes
before interaction.memory's (slots shed in name order at equal priority), and each sheds from the lowest rank up,
which by the default `order_by` (`-relevance`, `-freshness`) is the oldest first. The kept set is therefore the newest
history that fits, and only when no history fits, the newest memory that does. The harness's own renderer
(cwabench.canon.render, which Domain 1's S0 checks against every published payload) writes the predicted payload, so
every answer is compared with the exact bytes and count it must give.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from cwabench.canon import render
from cwabench.canon.tokenizers import TOKENIZERS

from . import profile as profile_mod
from . import writers
from .writers import Clock, TENANT, USER

PROTECTED = ("governance.instructions", "governance.output_contract", "state.task", "interaction.query")


@dataclass(frozen=True)
class Arm:
    name: str
    state: bool
    memory: bool
    pipeline: bool
    description: str
    selection: str | None = None  # a baseline whose selection the arm takes, at each budget
    writer: str = "oracle"  # which state writer: the oracle, or the model-based extractor (producers.py)


ARMS = {arm.name: arm for arm in (
    Arm("cwa-history", False, False, False, "CWA with history only: every prior turn in interaction.history"),
    Arm("cwa-state", True, False, False, "plus state.task from the oracle state writer"),
    Arm("cwa-state-x", True, False, False, "plus state.task from the model-based extractor", writer="extractor"),
    Arm("cwa-memory", True, True, False, "plus memory: turns before the history window compacted into "
                                          "interaction.memory, with expiry and revocation"),
    Arm("cwa-pipeline", True, True, True, "plus the route's supersession and exact deduplication on history"),
    Arm("cwa-format", False, False, False, "the window baseline's selection rendered as CWA renders it", "window"),
)}


@dataclass(frozen=True)
class Settings:
    turn_seconds: int = 120
    history_turns: int = 10
    memory_ttl_seconds: int = 86400
    reserved_output: int = 1024
    margin_percent: int = 15
    tokenizer: str = "estimate-utf8/v1"
    renderer: str = "cwa-messages/v1"


@dataclass(frozen=True)
class Point:
    kind: str  # "turn" or "probe"
    turn: int  # the turn queried (turn), or the last turn of history (probe)
    probe: dict | None = None

    @property
    def id(self) -> str:
        return self.probe["probe_id"] if self.probe else f"t{self.turn:03d}"


def points(script: dict, frames: bool) -> list[Point]:
    """A script's points: every turn when `frames`, and every probe, in conversation order."""
    found = [Point("probe", p["after_turn"], p) for p in script["probes"]]
    if frames:
        found += [Point("turn", t["turn"]) for t in script["turns"]]
    return sorted(found, key=lambda p: (p.turn + (0.5 if p.kind == "probe" else 0)))


@dataclass(frozen=True)
class Expected:
    outcome: str
    refusal_reason: str | None
    included: frozenset[str]
    payload: bytes | None
    input_tokens: int | None

    @property
    def payload_hash(self) -> str | None:
        return hashlib.sha256(self.payload).hexdigest() if self.payload is not None else None


@dataclass
class Frozen:
    """One arm's snapshot at one point, without its budget, and what any budget must make of it."""

    snapshot: dict
    protected: list[dict]
    history: list[dict]  # newest first: the order fitting keeps them in
    memory: list[dict]  # newest first
    carriers: dict[str, list[str]]  # turn id → the item ids whose content carries that turn's facts
    evidence: dict[str, list[str]]  # turn id → the texts those items carry it in
    deduplicated: list[str] = field(default_factory=list)  # history ids the pipeline's dedupe excludes
    full: int = 0  # the charged count with every candidate included
    floor: int = 0  # the charged count of the protected items alone: below it, assembly must refuse
    _fast: dict | None = None

    def at(self, budget: int) -> bytes:
        snapshot = dict(self.snapshot, budget=dict(self.snapshot["budget"], input=budget))
        return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def charged(self, items: list[dict]) -> int:
        count = render.render(self.snapshot, items).count(self.snapshot["tokenizer"])
        return render.charged(count, self.snapshot["budget"].get("margin_percent", 0))

    def _charged_fast(self, history: int, memory: int) -> int | None:
        """The charged count with the newest `history` and `memory` items, by arithmetic: under cwa-messages/v1 and
        estimate-utf8/v1 the count is the system texts' counts plus the user message's, and the message is the
        concatenation of each item's rendered block. None for any other renderer or tokenizer."""
        if self._fast is None:
            return None
        bytes_ = self._fast["base"] + sum(self._fast["history"][:history]) + sum(self._fast["memory"][:memory])
        count = self._fast["system"] + (bytes_ + 3) // 4
        return render.charged(count, self.snapshot["budget"].get("margin_percent", 0))

    def _fits(self, budget: int, history: int, memory: int) -> bool:
        fast = self._charged_fast(history, memory)
        if fast is not None:
            return fast <= budget
        return self.charged(self.protected + self.history[:history] + self.memory[:memory]) <= budget

    def expect(self, budget: int) -> Expected:
        if self.floor > budget:
            return Expected("refused", "protected_content_over_budget", frozenset(), None, None)

        def most(limit: int, fits) -> int:  # the largest n in [0, limit] that fits; fits is monotone
            low, high = 0, limit
            while low < high:
                middle = (low + high + 1) // 2
                low, high = (middle, high) if fits(middle) else (low, middle - 1)
            return low

        memory = len(self.memory)
        history = most(len(self.history), lambda n: self._fits(budget, n, memory))
        if history == 0 and not self._fits(budget, 0, memory):
            memory = most(len(self.memory), lambda n: self._fits(budget, 0, n))
        items = self.protected + self.history[:history] + self.memory[:memory]
        rendered = render.render(self.snapshot, items)
        count = rendered.count(self.snapshot["tokenizer"])
        if self._fast is not None and self._charged_fast(history, memory) != render.charged(
                count, self.snapshot["budget"].get("margin_percent", 0)):
            raise AssertionError("the arithmetic count differs from the renderer's")  # a harness defect
        return Expected("assembled", None, frozenset(i["id"] for i in items), rendered.payload, count)


def _batch(producer: str, items: list[dict], excluded: list[dict] | None = None) -> dict:
    return {"producer": {"id": producer, "kind": profile_mod.PRODUCERS[producer]["kind"]}, "items": items,
            "excluded": excluded or []}


def _rank_newest(items: list[dict]) -> list[dict]:
    """Highest rank first under the default order_by: newest freshness, then id ascending among equal instants."""
    from cwabench.canon import instants
    from cwabench.canon.strings import utf16_key

    by_id = sorted(items, key=lambda i: utf16_key(i["id"]))
    return sorted(by_id, key=lambda i: instants.parse(i["freshness"]), reverse=True)


def _dedupe(history: list[dict]) -> tuple[list[dict], list[str]]:
    """R-24 on history ranked highest first: the first of equal bodies (whitespace collapsed, trimmed) is kept."""
    kept, seen, dropped = [], set(), []
    for entry in history:
        key = " ".join(entry["body"].split())
        if key in seen:
            dropped.append(entry["id"])
        else:
            seen.add(key)
            kept.append(entry)
    return kept, dropped


def freeze(contract, script: dict, arm: Arm, point: Point, settings: Settings, only: set[str] | None = None,
           produced=None) -> Frozen:
    """`arm`'s snapshot at `point`. `only` keeps just the history messages with those ids (the format control);
    `produced` holds the model-based producers' outputs for the script (producers.Produced), which the extractor arm
    needs."""
    clock = Clock(settings.turn_seconds)
    turns = script["turns"]
    if point.kind == "probe":
        before, now = turns[:point.turn], clock.probe(point.turn)
        query = writers.item(contract, f"probe:{point.probe['probe_id']}", "interaction.query",
                             point.probe["question"], source=f"probe:{point.probe['probe_id']}", freshness=now)
    else:
        before, now = turns[:point.turn - 1], clock.user(point.turn)
        current = turns[point.turn - 1]
        query = writers.item(contract, writers.message_id(current, "user"), "interaction.query", current["user"],
                             source=writers.message_id(current, "user"), freshness=now)
    upto = before[-1]["turn"] if before else 0
    window = before[-settings.history_turns:] if arm.memory and settings.history_turns > 0 else before
    if arm.memory and settings.history_turns == 0:
        window = []
    compacted = before[:len(before) - len(window)]

    governance = [
        writers.item(contract, "policy:instructions", "governance.instructions", script["instructions"],
                     source="policy-registry:instructions", freshness=writers.START, trust="verified"),
        writers.item(contract, "policy:output-contract", "governance.output_contract", script["output_contract"],
                     source="policy-registry:output-contract", freshness=writers.START, trust="verified"),
    ]
    history = writers.history(contract, clock, window)
    if only is not None:
        history = [i for i in history if i["id"] in only]
        window = [t for t in window if writers.message_id(t, "user") in only]
    if not arm.state:
        state = writers.Written()
    elif arm.writer == "extractor":
        if produced is None or upto not in produced.states:
            raise ValueError(f"{arm.name} needs the extractor's state after turn {upto}")
        state = writers.extracted(contract, clock, script, produced.states[upto], upto)
    else:
        state = writers.state(contract, clock, script, upto)
    memory = (writers.memory(contract, clock, script, compacted, upto, now, settings.memory_ttl_seconds)
              if arm.memory else writers.Written())

    batches = [_batch("policy-registry", governance), _batch("conversation", [*history, query])]
    if arm.state:
        batches.append(_batch("state-svc", state.items))
    if arm.memory:
        batches.append(_batch("memory-svc", memory.items, memory.excluded))
    route = profile_mod.route_policy(arm.pipeline)
    snapshot = {
        "assembly_time": writers.instant(now),
        "scope": {"tenant": TENANT, "user": USER, "session": script["conversation_id"],
                  "task": script["conversation_id"]},
        "budget": {"input": 0, "reserved_output": settings.reserved_output,
                   "margin_percent": settings.margin_percent},
        "profile": profile_mod.profile(contract, route),
        "route_policy": route,
        "tokenizer": settings.tokenizer,
        "renderer": settings.renderer,
        "batches": batches,
        "conflicts": [],
    }

    ranked_history = _rank_newest(history)
    deduplicated: list[str] = []
    if arm.pipeline:
        ranked_history, deduplicated = _dedupe(ranked_history)
    carriers: dict[str, list[str]] = {}
    evidence: dict[str, list[str]] = {}
    for turn in window:
        if turn["shards"]:
            carriers.setdefault(turn["id"], []).append(writers.message_id(turn, "user"))
            evidence.setdefault(turn["id"], []).extend(turn["shards"])
    for written in (state, memory):
        bodies = {i["id"]: i["body"] for i in written.items}
        for item_id, carried in written.carries.items():
            for turn_id in carried:
                carriers.setdefault(turn_id, []).append(item_id)
                evidence.setdefault(turn_id, []).append(bodies[item_id])

    frozen = Frozen(snapshot, [*governance, *state.items, query], ranked_history, _rank_newest(memory.items),
                    carriers, evidence, deduplicated)
    if settings.renderer == "cwa-messages/v1" and settings.tokenizer == "estimate-utf8/v1":
        frozen._fast = _blocks(frozen)
    frozen.floor = frozen.charged(frozen.protected)
    frozen.full = frozen.charged(frozen.protected + frozen.history + frozen.memory)
    snapshot["budget"]["input"] = frozen.full
    return frozen


def _blocks(frozen: Frozen) -> dict:
    """Each compressible item's rendered block length in bytes, and the protected items' counts, for the arithmetic
    count: rendering the protected items with one more item adds exactly that item's block to the user message."""
    count = TOKENIZERS["estimate-utf8/v1"]

    def parts(items):
        rendered = render.render(frozen.snapshot, items)
        *system, content = rendered.counted
        return sum(count(text) for text in system), len(content.encode("utf-8"))

    system, base = parts(frozen.protected)
    return {"system": system, "base": base,
            "history": [parts(frozen.protected + [i])[1] - base for i in frozen.history],
            "memory": [parts(frozen.protected + [i])[1] - base for i in frozen.memory]}
