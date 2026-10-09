"""Conventional payload assembly, the baselines CWA is compared with (domain-2-plan.md, 3 and 7).

Each baseline is harness code that builds what a conventional application sends, from the same script, at the same
point and budget as the CWA arms, and records what it dropped:

| Arm | Payload |
| --- | --- |
| `concat` | the system prompt and every prior turn; overflows when it does not fit |
| `truncate` | the same, oldest messages dropped first until it fits, the system prompt first among them |
| `truncate-pinned` | the system prompt pinned, the oldest turn messages dropped until it fits |
| `window` | the last `window_turns` turns, then as truncate-pinned |
| `summary` | the system prompt, a rolling summary of the turns before the window, then the window; the summary, the oldest content, goes first, then the oldest window messages |

**The payload** has the shape `cwa-messages/v1` gives a request, so S2 hands every arm to the model the same way:
the JCS serialization of `{"system": [{"id", "text"}…], "tools": [], "messages": [{"role", "content"}…]}`. The system
prompt is the instructions, a blank line and the output contract, as one entry (`system`); the rolling summary is a
second entry (`summary`). Prior turns are native `user` and `assistant` messages, and the probe's question (or the
turn queried) is the last `user` message, which no baseline drops.

**The count** is the declared tokenizer's count of every system text and every message content, summed, times
(100 + margin) / 100 rounded up: the rule `cwa-messages/v1` counts by, with no wrapper to count. Since a baseline's
texts are counted separately, the count of a selection is the sum of its parts, and the fit search is arithmetic.

**What it dropped.** Every baseline drops a prefix of its droppable messages, so `first_kept` names the oldest kept
message and determines the rest; `system_survived` says whether the system prompt is in the payload.

The rolling summary is a producer's output, made before the payload is built. At P2 it is the `stub` summarizer:
Domain 1's extractive compressor (cwabench.producers.compressors) applied to each summarized user turn, in order.
It selects sentences, so it adds no fact, and it may or may not keep a turn's fact sentence. The cached LLM
summarizer, the strongest conventional control, arrives with the model client at P3.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from cwabench.canon import jcs
from cwabench.canon.render import charged
from cwabench.canon.tokenizers import TOKENIZERS
from cwabench.producers.compressors import extractive

from ..application.writers import message_id

ARMS = ("concat", "truncate", "truncate-pinned", "window", "summary")


@dataclass(frozen=True)
class Settings:
    window_turns: int = 10
    summarizer: str = "stub"
    extractive_ratio: float = 0.4
    margin_percent: int = 15
    tokenizer: str = "estimate-utf8/v1"


@dataclass(frozen=True)
class Message:
    id: str
    role: str  # system, user or assistant
    text: str


@dataclass
class Built:
    """One baseline's payload at one point and budget, and what it kept."""

    arm: str
    outcome: str  # "fits", or "overflow" when even the messages it never drops exceed the budget
    payload: bytes
    input_tokens: int
    charged: int
    kept: list[str]  # message ids, in payload order
    history_total: int
    history_kept: int
    first_kept: str | None  # the oldest prior-turn message kept, or None
    system_survived: bool
    summary: dict | None  # {"turns", "kept", "tokens"} for the summary arm
    carriers: dict[str, list[str]] = field(default_factory=dict)  # turn id → message ids carrying its facts
    evidence: dict[str, list[str]] = field(default_factory=dict)  # turn id → the texts carrying it

    @property
    def payload_hash(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


def system_prompt(script: dict) -> str:
    return f"{script['instructions']}\n\n{script['output_contract']}"


def summarize(turns: list[dict], settings: Settings) -> tuple[str, dict[str, bool]]:
    """The rolling summary of `turns`, and, per fact-carrying turn, whether it kept every fact sentence."""
    if settings.summarizer != "stub":
        raise ValueError(f"the {settings.summarizer!r} summarizer arrives with the model client (P3)")
    parts, kept = [], {}
    for turn in turns:
        digest = extractive(turn["user"], settings.extractive_ratio, settings.tokenizer)
        parts.append(digest)
        if turn["shards"]:
            kept[turn["id"]] = all(shard in digest for shard in turn["shards"])
    return " ".join(parts), kept


def payload(messages: list[Message]) -> bytes:
    system = [{"id": m.id, "text": m.text} for m in messages if m.role == "system"]
    chat = [{"role": m.role, "content": m.text} for m in messages if m.role != "system"]
    return jcs.serialize_bytes({"system": system, "tools": [], "messages": chat})


def build(arm: str, script: dict, history_turns: list[dict], query: Message, budget: int,
          settings: Settings) -> Built:
    """`arm`'s payload with `history_turns` as the prior turns and `query` as the live one."""
    count = TOKENIZERS[settings.tokenizer]
    system = Message("system", "system", system_prompt(script))
    window = history_turns
    summary_message, summary_kept = None, {}
    if arm in ("window", "summary"):
        window = history_turns[-settings.window_turns:] if settings.window_turns > 0 else []
    if arm == "summary":
        older = history_turns[:len(history_turns) - len(window)]
        if older:
            text, summary_kept = summarize(older, settings)
            summary_message = Message("summary", "system", f"Summary of the earlier conversation:\n{text}")
    history = [m for turn in window for m in (Message(message_id(turn, "user"), "user", turn["user"]),
                                              Message(message_id(turn, "assistant"), "assistant", turn["assistant"]))]

    # The droppable messages, oldest first, and the ones the arm never drops.
    if arm == "truncate":
        droppable, pinned = [system, *history], []
    elif arm == "summary":
        droppable, pinned = [*([summary_message] if summary_message else []), *history], [system]
    else:
        droppable, pinned = history, [system]
    counts = [count(m.text) for m in droppable]
    base = sum(count(m.text) for m in [*pinned, query])
    margin = settings.margin_percent

    def fits(dropped: int) -> bool:
        return charged(base + sum(counts[dropped:]), margin) <= budget

    if arm == "concat":
        dropped = 0
    else:
        low, high = 0, len(droppable)  # the fewest drops that fit; dropping more never costs more
        while low < high:
            middle = (low + high) // 2
            low, high = (low, middle) if fits(middle) else (middle + 1, high)
        dropped = low
    outcome = "fits" if fits(dropped) else "overflow"
    kept_droppable = droppable[dropped:]
    messages = [*pinned, *kept_droppable, query]
    tokens = sum(count(m.text) for m in messages)
    kept_history = [m for m in messages if m.role in ("user", "assistant") and m is not query]

    carriers: dict[str, list[str]] = {}
    evidence: dict[str, list[str]] = {}
    for turn in window:
        if turn["shards"]:
            carriers[turn["id"]] = [message_id(turn, "user")]
            evidence[turn["id"]] = list(turn["shards"])
    for turn_id, whole in summary_kept.items():
        if whole:
            carriers.setdefault(turn_id, []).append("summary")
        older = next(t for t in history_turns if t["id"] == turn_id)
        evidence.setdefault(turn_id, []).extend(older["shards"])
    summary = None
    if arm == "summary":
        summary = {"turns": len(history_turns) - len(window), "kept": any(m.id == "summary" for m in messages),
                   "tokens": count(summary_message.text) if summary_message else 0}
    return Built(arm, outcome, payload(messages), tokens, charged(tokens, margin), [m.id for m in messages],
                 len(history), len(kept_history), kept_history[0].id if kept_history else None,
                 any(m.id == "system" for m in messages), summary, carriers, evidence)


def at(arm: str, script: dict, point, budget: int, settings: Settings) -> Built:
    """`arm`'s payload at a point (application/snapshots.py): a probe's question after turn t, or user turn k."""
    turns = script["turns"]
    if point.kind == "probe":
        query = Message(f"probe:{point.probe['probe_id']}", "user", point.probe["question"])
        return build(arm, script, turns[:point.turn], query, budget, settings)
    current = turns[point.turn - 1]
    return build(arm, script, turns[:point.turn - 1], Message(message_id(current, "user"), "user", current["user"]),
                 budget, settings)


def full(script: dict, point, settings: Settings) -> int:
    """The charged count of the whole conversation at a point in native chat: what the baselines' ratio tiers are of."""
    return at("concat", script, point, 1, settings).charged
