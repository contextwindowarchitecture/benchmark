"""VT · Variable tracking (domain-2-plan.md, 6.1).

Tracked variables are figures the user assigns and later reassigns ("the Harwell account's credit limit is 4,200"),
among distractor chains with near-miss names (the same onset with another coda, or the same entity's other figure) and
filler turns. Each probe asks for one tracked variable's current value.

Parameters: `variables` (tracked chains), `distractors` (decoy chains), `assignment_density` (the share of turns that
assign a value; each such turn assigns exactly one), `filler_sentences` per turn. The turn count fixes how many
assignments there are, so a longer conversation has longer chains; each probe records its own difficulty
(`attributes`): the distance in turns from the variable's last assignment, how many values it has had, and how many
chains there are, which are what the plan's chain-length, reassignment and distance parameters measure.

Every value in a conversation is distinct, so a wrong number is classified exactly: an earlier value of the same
variable is `stale`, any value of another variable a `distractor`.
"""
from __future__ import annotations

import random

from . import names
from .names import capitalize, number

GENERATOR = "vt"
VERSION = 2  # 2: longer assistant turns; the output contract apart from the instructions

INSTRUCTIONS = ("You are an assistant helping the user keep track of figures they mention during a long working "
                "session. A figure can change; the latest value the user gave is the one that counts.")
OUTPUT_CONTRACT = "When asked for a figure, reply with the number only."

KINDS = (
    ("account", ("credit limit", "overdraft limit", "monthly allowance")),
    ("project", ("budget", "headcount cap", "contingency fund")),
    ("warehouse", ("crate count", "pallet capacity", "reorder threshold")),
    ("fleet", ("fuel allowance", "mileage cap", "vehicle count")),
)

FIRST = ("Please note that {phrase} is {value}.", "For the record, {phrase} is {value}.", "{Phrase} is {value}.",
         "Let's keep track of {phrase}: it is {value}.")
UPDATE = ("Update: {phrase} is now {value}.", "Change {phrase} to {value}.", "{Phrase} has been set to {value}.",
          "From now on, {phrase} is {value}.")
RISE = ("{Phrase} has gone up to {value}.",)
FALL = ("{Phrase} has come down to {value}.",)
QUESTIONS = ("What is {phrase} now? Reply with the number only.",
             "Quick check: what is {phrase} at the moment? Just the number, please.",
             "Remind me, what is {phrase} currently? Number only.")

VALUES = range(1000, 10000, 25)


def _say(templates: tuple[str, ...], phrase: str, value: int, rng: random.Random) -> str:
    return rng.choice(templates).format(phrase=phrase, Phrase=capitalize(phrase), value=number(value))


def _chains(rng: random.Random, tracked: int, distractors: int) -> list[dict]:
    pool, taken = names.names(rng), set()
    chains = []
    for i in range(tracked):
        name = next(n for n in pool if n not in taken)
        taken.add(name)
        kind, attributes = rng.choice(KINDS)
        attribute = rng.choice(attributes)
        chains.append({"variable_id": f"v{i + 1}", "role": "tracked", "near_miss": None, "of": None,
                       "name": name, "kind": kind, "attribute": attribute})
    for j in range(distractors):
        base = chains[j % tracked]
        if j % 2 == 0:  # a near-miss name: the same onset, another coda
            name = names.similar(base["name"], taken, rng)
            taken.add(name)
            near_miss, kind, attribute = "name", base["kind"], base["attribute"]
        else:  # the same entity's other figure
            used = {c["attribute"] for c in chains if c["name"] == base["name"]}
            choices = [a for k, attrs in KINDS if k == base["kind"] for a in attrs if a not in used]
            if not choices:  # every figure of that entity is taken: fall back to a near-miss name
                name = names.similar(base["name"], taken, rng)
                taken.add(name)
                near_miss, kind, attribute = "name", base["kind"], base["attribute"]
            else:
                name, near_miss, kind, attribute = base["name"], "attribute", base["kind"], rng.choice(choices)
        chains.append({"variable_id": f"d{j + 1}", "role": "distractor", "near_miss": near_miss,
                       "of": base["variable_id"], "name": name, "kind": kind, "attribute": attribute})
    for chain in chains:
        chain["phrase"] = f"the {chain['name']} {chain['kind']}'s {chain['attribute']}"
    return chains


def generate(seed: int, turns: int, checkpoints: list[int], index: int, parameters: dict,
             questions: tuple[str, ...] = QUESTIONS) -> dict:
    """A VT script. `questions` lets IP (persistence.py) ask without "number only", which would contradict its rule."""
    rng = random.Random(seed)
    tracked, distractors = int(parameters["variables"]), int(parameters["distractors"])
    if tracked < 1:
        raise ValueError("vt needs at least one tracked variable")
    chains = _chains(rng, tracked, distractors)
    count = min(turns, max(len(chains), round(turns * float(parameters["assignment_density"]))))
    # Turn 1 always assigns, so the first probe has a tracked variable to ask about.
    positions = [1, *sorted(rng.sample(range(2, turns + 1), count - 1))]

    # The first len(chains) assignments give every chain its first value (a tracked one first); the rest reassign,
    # tracked chains twice as often as distractors.
    order = [chains[0], *rng.sample(chains[1:], len(chains) - 1)]
    weights = [2 if c["role"] == "tracked" else 1 for c in chains]
    values = rng.sample(VALUES, count)
    events: dict[int, tuple[dict, int]] = {}
    for n, turn in enumerate(positions):
        chain = order[n] if n < len(order) else rng.choices(chains, weights)[0]
        events[turn] = (chain, values[n])

    assignments = {c["variable_id"]: [] for c in chains}
    script_turns = []
    for turn in range(1, turns + 1):
        shards = []
        if turn in events:
            chain, value = events[turn]
            history = assignments[chain["variable_id"]]
            if not history:
                templates = FIRST
            else:
                templates = rng.choice((UPDATE, RISE if value > history[-1]["value"] else FALL))
            shards.append(_say(templates, chain["phrase"], value, rng))
            history.append({"turn": turn, "value": value})
        script_turns.append({
            "turn": turn,
            "id": f"t{turn:03d}",
            "user": names.user_text(shards, int(parameters["filler_sentences"]), rng),
            "assistant": names.reply(rng, int(parameters["reply_sentences"])),
            "shards": shards,
        })

    probes = []
    for after in checkpoints:
        def known(chain):
            return [a for a in assignments[chain["variable_id"]] if a["turn"] <= after]

        candidates = [c for c in chains if c["role"] == "tracked" and known(c)]
        chain = rng.choice(candidates)
        values_so_far = known(chain)
        current = values_so_far[-1]
        others = [a["value"] for c in chains if c is not chain for a in known(c)]
        question = rng.choice(questions).format(phrase=chain["phrase"])
        figures = " ".join(f"{capitalize(c['phrase'])} is {number(known(c)[-1]['value'])}." for c in chains if known(c))
        shards = [s for t in script_turns[:after] for s in t["shards"]]
        probes.append({
            "probe_id": f"p{after:03d}",
            "after_turn": after,
            "question": question,
            "answer": {"kind": "number", "expected": str(current["value"]),
                       "stale": [str(a["value"]) for a in values_so_far[:-1]],
                       "distractors": [str(v) for v in sorted(others)]},
            "needs": [f"t{current['turn']:03d}"],
            "full": f"Here are the current figures. {figures}\n\n{question}",
            "concat": "\n".join(shards) + f"\n\n{question}",
            "attributes": {"variable": chain["variable_id"], "distance": after - current["turn"],
                           "values": len(values_so_far), "chains": len(chains)},
        })

    return {
        "instructions": INSTRUCTIONS,
        "output_contract": OUTPUT_CONTRACT,
        "turns": script_turns,
        "probes": probes,
        "ground_truth": {
            "task": "variables",
            "variables": [{"variable_id": c["variable_id"], "role": c["role"], "near_miss": c["near_miss"],
                           "of": c["of"], "phrase": c["phrase"],
                           "assignments": [{"turn": a["turn"], "value": str(a["value"])}
                                           for a in assignments[c["variable_id"]]]}
                          for c in chains],
        },
    }
