"""Model-based producers (domain-2-plan.md, 7 and 4.3): run before any snapshot is frozen, each call cached.

Two producers rewrite the conversation with the model, turn by turn, as a real application would as the turns arrive:

- **The extractor** (the `cwa-state-x` arm's state writer). After each user turn it reads the state so far and the new
  message, and returns the updated state as a JSON object of facts and their current values. A reply that is not a
  JSON object keeps the state as it was and is counted. The state after turn k is written as state.task items, one per
  key. This is the realistic counterpart of the oracle state writer (section 15: oracle-state circularity).
- **The rolling summarizer** (the `summary` baseline with `[baselines].summarizer = "llm"`). As each turn leaves the
  window it updates the summary with that turn's user and assistant messages; the summary after turn k covers turns 1
  to k. It is the study's strongest conventional control (RECAP/SNOWBALL).

Every call goes through the model (model/), so it is cached and replayable exactly as S2's are, with sample 0 and the
model's parameters. Calls of one conversation run in order, since each depends on the one before; conversations run
in parallel. Each call is recorded as a `producer-row`.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from cwabench.canon import jcs

from ..grading.normalize import json_object, strip_reasoning
from ..model import CacheMiss, EndpointError, Model

EXTRACT_SYSTEM = ("You maintain the state of a task from a conversation with a user. The state is a JSON object that "
                  "maps each fact the user has given to its current value. Reply with the updated JSON object only.")
EXTRACT_USER = ("Current state:\n{state}\n\nNew message from the user:\n{user}\n\nUpdate the state: add each new fact, "
                "change any fact the user changed or corrected, and keep the rest. Use short, stable keys. Reply with "
                "the JSON object only.")
SUMMARY_SYSTEM = ("You keep a running summary of a long conversation for an assistant that will not see the older "
                  "turns. Keep every name, figure and value the user gave, with its latest value, and drop small talk. "
                  "Reply with the summary only.")
SUMMARY_USER = ("Summary so far:\n{summary}\n\nNext turn of the conversation:\nUser: {user}\nAssistant: {assistant}"
                "\n\nWrite the updated summary.")
NOTHING = "(nothing yet)"


@dataclass
class Produced:
    """One conversation's producer outputs, by the turn they cover."""

    states: dict[int, dict] = field(default_factory=dict)  # after turn k (0 … T): the extracted state
    summaries: dict[int, str] = field(default_factory=dict)  # after turn k (0 … T − window): the rolling summary


def payload(system: str, user: str) -> bytes:
    return jcs.serialize_bytes({"system": [{"id": "system", "text": system}], "tools": [],
                                "messages": [{"role": "user", "content": user}]})


def _row(run_id: str, script: dict, producer: str, turn: int, reply, parsed: bool, text: str) -> dict:
    from .. import output

    return {"$schema": output.schema_name("producer-row"), "run_id": run_id, "conversation": script["conversation_id"],
            "producer": producer, "turn": turn, "key": reply.key, "request_sha256": reply.request_sha256,
            "cache_hit": reply.cache_hit, "lookup_ms": reply.lookup_ms, "parsed": parsed, "output": text[:4000],
            "provenance": reply.provenance}


def _extract(model: Model, run_id: str, script: dict) -> tuple[dict[int, dict], list[dict]]:
    states, rows, state = {0: {}}, [], {}
    for turn in script["turns"]:
        current = json.dumps(state, ensure_ascii=False, sort_keys=True) if state else "{}"
        reply = model.ask(payload(EXTRACT_SYSTEM, EXTRACT_USER.format(state=current, user=turn["user"])), 0)
        value, _ = json_object(strip_reasoning(reply.text))
        if value is not None:
            state = value
        states[turn["turn"]] = state
        rows.append(_row(run_id, script, "extractor", turn["turn"], reply, value is not None, reply.text))
    return states, rows


def _summarize(model: Model, run_id: str, script: dict, upto: int) -> tuple[dict[int, str], list[dict]]:
    summaries, rows, summary = {0: ""}, [], ""
    for turn in script["turns"][:upto]:
        reply = model.ask(payload(SUMMARY_SYSTEM, SUMMARY_USER.format(summary=summary or NOTHING, user=turn["user"],
                                                                        assistant=turn["assistant"])), 0)
        text = strip_reasoning(reply.text)
        if text:
            summary = text
        summaries[turn["turn"]] = summary
        rows.append(_row(run_id, script, "summarizer", turn["turn"], reply, bool(text), reply.text))
    return summaries, rows


def produce(model: Model, run_id: str, scripts: list[dict], extract: bool, summarize: bool, window: int,
            concurrency: int, log) -> tuple[dict[str, Produced], list[dict], list[str]]:
    """Every conversation's producer outputs, the call rows, and the errors (a replay miss, an endpoint failure)."""
    produced: dict[str, Produced] = {}
    rows: list[dict] = []
    errors: list[str] = []

    def one(script):
        out, mine = Produced(), []
        try:
            if extract:
                out.states, found = _extract(model, run_id, script)
                mine += found
            if summarize:
                out.summaries, found = _summarize(model, run_id, script, max(0, script["turn_count"] - window))
                mine += found
        except (CacheMiss, EndpointError) as error:
            return script, out, mine, f"{script['conversation_id']}: {type(error).__name__}: {error}"
        return script, out, mine, None

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for script, out, mine, error in pool.map(one, scripts):
            produced[script["conversation_id"]] = out
            rows += mine
            if error:
                errors.append(error)
            log(f"producers: {script['conversation_id']} ({len(mine)} calls)")
    return produced, rows, errors
