"""What the application writes before each snapshot is frozen (domain-2-plan.md, 4.3): the conversation's items, state
and memory. All of it is harness code doing the application's duties (R-5, R-8, R-9, R-14), never the model.

- **The clock.** Turn k's user message is said at START + (k − 1) × `turn_seconds`, its scripted reply half a period
  later, and the probe after turn t a quarter period before turn t + 1. A snapshot's assembly_time is the moment its
  query is said, so freshness, expiry and history order are all fixed by the script.
- **Conversation.** Each user and assistant message is its own interaction.history item (`turn:<tid>:user`,
  `turn:<tid>:assistant`); prior model turns carry lineage `generated` and authority `untrusted` (R-1, R-7).
- **The oracle state writer.** state.task holds every fact the user has stated so far, at its current value: one
  item per figure (VT), per booking field (FR record) or per order line (FR compute). It writes from the ground truth,
  so it is the upper bound of what an application's state machine could know (domain-2-plan.md, 15), and it holds the
  facts, not the answer: an order's lines, never its total.
- **The memory producer.** The application keeps the last `history_turns` turns verbatim and compacts older ones
  into interaction.memory: one item per compacted turn that stated a fact, its body the turn's fact sentences (an
  extractive digest, lineage `extracted`), its source the user message it came from, expiring `memory_ttl_seconds`
  after it was said. A memory whose fact a later turn replaced is revoked: the producer reports it as excluded with
  reason `revoked`, and an expired one with `expired`, and emits neither (R-9, R-14). Revocation reads the same ground
  truth as the state writer, so it is the oracle's too. Turns with no fact are compacted into nothing.

Every item writes its six policy fields out (R-3), so a trace fills no defaults.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from ..conversations.names import capitalize, number

START = datetime(2026, 10, 1, 9, 0, 0, tzinfo=timezone.utc)
TENANT, USER = "cwa-bench", "u_1"


def instant(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Clock:
    turn_seconds: int

    def user(self, turn: int) -> datetime:
        return START + timedelta(seconds=(turn - 1) * self.turn_seconds)

    def assistant(self, turn: int) -> datetime:
        return self.user(turn) + timedelta(seconds=self.turn_seconds // 2)

    def probe(self, after: int) -> datetime:
        return self.user(after) + timedelta(seconds=self.turn_seconds * 3 // 4)


@dataclass
class Written:
    """One producer's output for a snapshot: its items, its excluded rows, and the turns each item carries."""

    items: list[dict] = field(default_factory=list)
    excluded: list[dict] = field(default_factory=list)
    carries: dict[str, list[str]] = field(default_factory=dict)  # item id → turn ids whose facts it holds


def item(contract, id: str, slot: str, body: str, *, source: str, freshness: datetime, authority: str | None = None,
         trust: str = "unverified", source_version: str = "1", **fields) -> dict:
    """An item with every policy field written out from its slot's defaults, then `fields` applied."""
    defaults = contract.slot_defaults[slot]
    written = {
        "id": id, "slot": slot, "source": source, "source_version": source_version,
        "authority": authority or defaults["authority"], "trust": trust, "freshness": instant(freshness),
        "body": body,
        "token_budget": defaults["token_budget"], "variants": [], "conflict_policy": defaults["conflict_policy"],
        "lineage": defaults["lineage"], "eligibility": defaults["eligibility"],
        "injection_risk": defaults["injection_risk"],
    }
    written.update(fields)
    return written


def message_id(turn: dict, speaker: str) -> str:
    return f"turn:{turn['id']}:{speaker}"


def history(contract, clock: Clock, turns: list[dict]) -> list[dict]:
    """Every message of `turns`, as interaction.history items in the order they were said."""
    items = []
    for turn in turns:
        user_id, assistant_id = message_id(turn, "user"), message_id(turn, "assistant")
        items.append(item(contract, user_id, "interaction.history", turn["user"], source=user_id,
                          freshness=clock.user(turn["turn"])))
        items.append(item(contract, assistant_id, "interaction.history", turn["assistant"], source=assistant_id,
                          freshness=clock.assistant(turn["turn"]), authority="untrusted", lineage="generated"))
    return items


def _facts(script: dict, upto: int) -> list[tuple[str, int, str]]:
    """The facts stated by turn `upto`, at their current values: (state key, the turn that set it, its state body)."""
    truth = script["ground_truth"]
    if truth["task"] == "variables":
        facts = []
        for variable in truth["variables"]:
            known = [a for a in variable["assignments"] if a["turn"] <= upto]
            if known:
                body = f"{capitalize(variable['phrase'])} is {number(int(known[-1]['value']))}."
                facts.append((variable["variable_id"], known[-1]["turn"], body))
        return facts
    if truth["task"] == "record":
        return [(f["field"], f["turn"], f"{f['field']}: {number(int(f['value'])) if f['kind'] == 'number' else f['value']}")
                for f in truth["fields"] if f["turn"] <= upto]
    facts = []
    for n, step in enumerate((s for s in truth["steps"] if s["turn"] <= upto), 1):
        if step["op"] == "item":
            line = f"{step['quantity']} {step['item']}s at {step['unit_price']} each"
        elif step["op"] == "fee":
            line = f"a delivery fee of {step['amount']}"
        else:
            line = f"a credit of {-step['amount']}"
        facts.append((f"line-{n:02d}", step["turn"], f"Order line {n}: {line}."))
    return facts


def state(contract, clock: Clock, script: dict, upto: int) -> Written:
    """The oracle state writer's state.task items after turn `upto`."""
    written = Written()
    versions = {}
    if script["ground_truth"]["task"] == "variables":
        versions = {v["variable_id"]: sum(a["turn"] <= upto for a in v["assignments"])
                    for v in script["ground_truth"]["variables"]}
    for key, turn, body in _facts(script, upto):
        state_id = f"state:{key}"
        written.items.append(item(contract, state_id, "state.task", body, source=f"state-svc:{key}",
                                  source_version=str(versions.get(key, 1)), freshness=clock.user(turn),
                                  trust="verified", scope={"tenant": TENANT, "task": script["conversation_id"]}))
        written.carries[state_id] = [f"t{turn:03d}"]
    return written


def _replaced(script: dict, turn: int, upto: int) -> bool:
    """Whether a fact turn `turn` stated was replaced by a later turn up to `upto` (VT reassignment)."""
    truth = script["ground_truth"]
    if truth["task"] != "variables":
        return False
    for variable in truth["variables"]:
        turns = [a["turn"] for a in variable["assignments"]]
        if turn in turns:
            return any(turn < later <= upto for later in turns)
    return False


def memory(contract, clock: Clock, script: dict, compacted: list[dict], upto: int, now: datetime,
           ttl_seconds: int) -> Written:
    """The memory producer's output for the compacted turns, as seen at `now` after turn `upto`."""
    written = Written()
    for turn in compacted:
        if not turn["shards"]:
            continue
        memory_id = f"mem:{turn['id']}"
        said = clock.user(turn["turn"])
        expires = said + timedelta(seconds=ttl_seconds)
        if _replaced(script, turn["turn"], upto):
            written.excluded.append({"item_id": memory_id, "reason": "revoked", "stage": "producer"})
        elif expires <= now:
            written.excluded.append({"item_id": memory_id, "reason": "expired", "stage": "producer"})
        else:
            written.items.append(item(contract, memory_id, "interaction.memory", " ".join(turn["shards"]),
                                      source=message_id(turn, "user"), freshness=said, lineage="extracted",
                                      expires=instant(expires), scope={"tenant": TENANT, "user": USER}))
            written.carries[memory_id] = [turn["id"]]
    return written
