"""FR · Facts revealed over turns (domain-2-plan.md, 6.2).

A fully specified task is sharded into facts revealed one per turn, among filler turns, and asked at every probe with
what has been revealed so far. Two tasks, alternating by conversation index unless `tasks` names one:

- `record`: a booking record whose fields (text and numbers) are revealed one by one. The probe asks for the record as
  a JSON object with every key, null for what is not known yet, and is graded field by field;
- `compute`: an order whose lines (a quantity at a unit price, a fee, a credit) are revealed one by one. The probe asks
  for the total so far, graded as a number; an earlier running total is a `stale` answer.

Parameters: `fields` (record) or `steps` (compute), the number of facts; `filler_sentences` per turn. The first fact
is revealed at turn 1 and the others at seeded turns, so a longer conversation spaces them further apart.

Each probe's `full` text is the FULL control (everything known at the probe, stated once as a brief) and `concat` the
CONCAT control (the opening and the shards revealed so far, in order, as one message).
"""
from __future__ import annotations

import random

from . import names
from .names import capitalize, number

GENERATOR = "fr"
VERSION = 1

INSTRUCTIONS = {
    "record": ("You are an assistant helping the user put together an event booking from details they give over a long "
               "working session. When asked for the booking record, reply with a single JSON object and nothing else."),
    "compute": ("You are an assistant helping the user put together a supply order from lines they give over a long "
                "working session. When asked for the total, reply with the number only."),
}

EVENTS = ("workshop", "retreat", "summit", "offsite")
HOSTS = ("Alba", "Corin", "Dara", "Emrys", "Fiona", "Galen", "Iris", "Lorcan")  # not names.PEOPLE, which filler uses
CATERING = ("buffet", "boxed lunches", "plated dinner", "finger food")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday")
CITY_ENDINGS = ("port", "haven", "field", "bridge")
ITEMS = ("lamp", "crate", "cable reel", "toolkit", "helmet", "lantern", "tarp", "ladder")

# Record fields in the order the record lists them: kind and reveal templates ({event}, {value}). The three number
# fields draw from disjoint ranges, so every value is written in exactly one turn.
FIELDS = {
    "venue": ("text", ("The {event} will be held at {value}.", "Let's book {value} for the {event}.")),
    "host": ("text", ("{value} has agreed to host the {event}.", "The host for the {event} is {value}.")),
    "city": ("text", ("The {event} takes place in {value}.",)),
    "day": ("text", ("We settled on {value} for the {event}.", "The {event} is on a {value}.")),
    "catering": ("text", ("For catering at the {event}, we'll go with {value}.",)),
    "capacity": ("number", ("Plan for {value} attendees at the {event}.", "The {event} should seat {value} people.")),
    "budget": ("number", ("The budget for the {event} is {value}.",)),
    "rooms": ("number", ("The {event} needs {value} breakout rooms.",)),
}


def _field_values(rng: random.Random, pool: list[str]) -> dict[str, str | int]:
    return {
        "venue": f"{pool[0]} Hall",
        "host": f"{rng.choice(HOSTS)} {pool[1]}",
        "city": rng.choice(names.ONSETS) + rng.choice(CITY_ENDINGS),
        "day": rng.choice(WEEKDAYS),
        "catering": rng.choice(CATERING),
        "capacity": rng.randrange(20, 400, 5),
        "budget": rng.randrange(1000, 50000, 250),
        "rooms": rng.randrange(2, 13),
    }


def _positions(rng: random.Random, turns: int, count: int) -> list[int]:
    if count > turns:
        raise ValueError(f"fr reveals {count} facts, one per turn, which needs at least {count} turns")
    return [1, *sorted(rng.sample(range(2, turns + 1), count - 1))]


def _turns(rng: random.Random, turns: int, shards_at: dict[int, str], opening: str, filler: int) -> list[dict]:
    script_turns = []
    for turn in range(1, turns + 1):
        shards = [shards_at[turn]] if turn in shards_at else []
        text = names.user_text(shards, filler, rng)
        script_turns.append({"turn": turn, "id": f"t{turn:03d}", "user": f"{opening} {text}" if turn == 1 else text,
                             "assistant": names.reply(rng), "shards": shards})
    return script_turns


def _record(rng: random.Random, turns: int, checkpoints: list[int], parameters: dict) -> dict:
    pool = names.names(rng)
    event = f"{pool[0]} {rng.choice(EVENTS)}"
    values = _field_values(rng, pool[1:])
    chosen = sorted(rng.sample(list(FIELDS), int(parameters["fields"])), key=list(FIELDS).index)
    reveal_order = rng.sample(chosen, len(chosen))
    positions = _positions(rng, turns, len(chosen))
    revealed = {}  # field → turn
    shards_at = {}
    for field, turn in zip(reveal_order, positions):
        kind, templates = FIELDS[field]
        value = number(values[field]) if kind == "number" else values[field]
        shards_at[turn] = capitalize(rng.choice(templates).format(event=event, value=value))
        revealed[field] = turn
    opening = f"I'm organising the {event} and need help keeping the booking straight."
    script_turns = _turns(rng, turns, shards_at, opening, int(parameters["filler_sentences"]))

    keys = ", ".join(chosen)
    question = (f"Fill in the booking record for the {event} as a JSON object with exactly these keys: {keys}. "
                f"Use null for anything I haven't told you yet. Reply with the JSON only.")
    probes = []
    for after in checkpoints:
        known = [f for f in chosen if revealed[f] <= after]
        expected = {f: (str(values[f]) if f in known else None) for f in chosen}
        brief = "; ".join(f"{f}: {number(values[f]) if FIELDS[f][0] == 'number' else values[f]}" for f in known)
        shards = [t["shards"][0] for t in script_turns[:after] if t["shards"]]
        probes.append({
            "probe_id": f"p{after:03d}",
            "after_turn": after,
            "question": question,
            "answer": {"kind": "record",
                       "fields": {f: {"kind": FIELDS[f][0], "expected": expected[f]} for f in chosen}},
            "needs": [f"t{revealed[f]:03d}" for f in sorted(known, key=revealed.get)],
            "full": f"Here is everything about the {event} so far: {brief or 'nothing yet'}.\n\n{question}",
            "concat": "\n".join([opening, *shards]) + f"\n\n{question}",
            "attributes": {"known": len(known), "fields": len(chosen),
                           "distance": after - max((revealed[f] for f in known), default=0)},
        })
    return {
        "instructions": INSTRUCTIONS["record"],
        "turns": script_turns,
        "probes": probes,
        "ground_truth": {
            "task": "record",
            "subject": event,
            "fields": [{"field": f, "kind": FIELDS[f][0], "value": str(values[f]), "turn": revealed[f]}
                       for f in chosen],
        },
    }


def _compute(rng: random.Random, turns: int, checkpoints: list[int], parameters: dict) -> dict:
    depot = f"{names.names(rng)[0]} depot"
    count = int(parameters["steps"])
    positions = _positions(rng, turns, count)
    items = rng.sample(ITEMS, len(ITEMS))
    steps, shards_at, total = [], {}, 0
    for n, turn in enumerate(positions):
        # The first line is an item, so a total exists from turn 1; a credit never exceeds the total before it.
        op = "item" if n == 0 else rng.choices(("item", "fee", "credit"), (3, 1, 1))[0]
        if op == "credit" and total < 20:
            op = "item"
        if op == "item":
            item, quantity, price = items[n % len(items)], rng.randrange(2, 13), rng.randrange(5, 96)
            amount = quantity * price
            text = f"Add {quantity} {item}s at {price} each to the order."
            step = {"op": op, "item": item, "quantity": quantity, "unit_price": price}
        elif op == "fee":
            amount = rng.randrange(15, 61, 5)
            text = f"There's a flat delivery fee of {amount} on the order."
            step = {"op": op}
        else:
            amount = -min(rng.randrange(10, 81, 5), total - 5)
            text = f"Take {-amount} off the order for the returned stock."
            step = {"op": op}
        total += amount
        shards_at[turn] = text
        steps.append({**step, "turn": turn, "amount": amount, "total_after": total})
    opening = f"I'm putting together a supply order for the {depot}."
    script_turns = _turns(rng, turns, shards_at, opening, int(parameters["filler_sentences"]))

    question = "What does the order come to so far? Reply with the total as a number only."
    probes = []
    for after in checkpoints:
        known = [s for s in steps if s["turn"] <= after]
        lines = "; ".join(shards_at[s["turn"]].rstrip(".") for s in known)
        probes.append({
            "probe_id": f"p{after:03d}",
            "after_turn": after,
            "question": question,
            "answer": {"kind": "number", "expected": str(known[-1]["total_after"]),
                       "stale": [str(s["total_after"]) for s in known[:-1]
                                 if s["total_after"] != known[-1]["total_after"]],
                       "distractors": []},
            "needs": [f"t{s['turn']:03d}" for s in known],
            "full": f"Here is the supply order for the {depot} so far: {lines}.\n\n{question}",
            "concat": "\n".join([opening, *(shards_at[s["turn"]] for s in known)]) + f"\n\n{question}",
            "attributes": {"known": len(known), "steps": count, "distance": after - known[-1]["turn"]},
        })
    return {
        "instructions": INSTRUCTIONS["compute"],
        "turns": script_turns,
        "probes": probes,
        "ground_truth": {"task": "compute", "subject": depot, "steps": steps, "total": str(total)},
    }


TASKS = {"record": _record, "compute": _compute}


def generate(seed: int, turns: int, checkpoints: list[int], index: int, parameters: dict) -> dict:
    tasks = list(parameters.get("tasks", list(TASKS)))
    unknown = [t for t in tasks if t not in TASKS]
    if unknown or not tasks:
        raise ValueError(f"fr tasks must be some of {', '.join(TASKS)}; got {tasks}")
    return TASKS[tasks[index % len(tasks)]](random.Random(seed), turns, checkpoints, parameters)
