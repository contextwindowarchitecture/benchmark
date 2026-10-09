"""CC · Corrections and contradictions (domain-2-plan.md, 6.3).

VT's figures (variables.py), but a later value is a correction, not a change: the user says the earlier value was
wrong ("Correction: the Harwell account's credit limit is 4,350, not 4,200."). The correcting sentence names the stale
value too, so the wrong answer is in the context right beside the right one. Each figure is stated once, in the first
third of the conversation, and corrected later: every tracked figure `1 … corrections` times, each distractor at most
once. There are no other reassignments, so every stale value in a CC conversation is one the user retracted. A short
conversation places the corrections that fit after the statements, the tracked figures' first.

The application's duty (R-9, R-14) is the same as for a reassignment: the memory of a retracted statement is revoked,
and the oracle state writer holds the corrected value. A probe asks for a tracked figure, one already corrected when
there is one; the headline is the stale-value rate.

Parameters: `variables`, `distractors`, `corrections` (the most corrections per tracked figure), `filler_sentences`
and `reply_sentences`.
"""
from __future__ import annotations

import random

from . import names, variables
from .names import capitalize, number

GENERATOR = "cc"
VERSION = 1

INSTRUCTIONS = ("You are an assistant helping the user keep track of figures they mention during a long working "
                "session. The user sometimes corrects a figure they gave earlier; the corrected value is the one that "
                "counts.")
OUTPUT_CONTRACT = variables.OUTPUT_CONTRACT

CORRECT = ("Correction: {phrase} is {value}, not {old}.",
           "Sorry, I gave you the wrong figure earlier: {phrase} is {value}, not {old}.",
           "Scratch what I said about {phrase}; the right figure is {value}, not {old}.")


def generate(seed: int, turns: int, checkpoints: list[int], index: int, parameters: dict) -> dict:
    rng = random.Random(seed)
    tracked, distractors = int(parameters["variables"]), int(parameters["distractors"])
    most = int(parameters["corrections"])
    if tracked < 1 or most < 1:
        raise ValueError("cc needs at least one tracked figure and one correction")
    chains = variables._chains(rng, tracked, distractors)
    counts = {c["variable_id"]: rng.randint(1, most) if c["role"] == "tracked" else rng.randint(0, 1) for c in chains}
    if len(chains) > turns:
        raise ValueError(f"cc states {len(chains)} figures, one per turn, which needs at least {len(chains)} turns")

    # Statements in the first third (turn 1 always states, so the first probe has a figure), corrections after.
    early = max(len(chains), turns // 3)
    stated_at = [1, *sorted(rng.sample(range(2, early + 1), len(chains) - 1))]
    order = [chains[0], *rng.sample(chains[1:], len(chains) - 1)]
    free = sorted(set(range(2, turns + 1)) - set(stated_at))
    corrections = [c for c in chains for _ in range(counts[c["variable_id"]])]
    rng.shuffle(corrections)
    corrections.sort(key=lambda c: c["role"] != "tracked")  # a short conversation keeps the tracked figures' first
    events: dict[int, tuple[dict, str]] = {turn: (chain, "stated") for turn, chain in zip(stated_at, order)}
    # Each correction lands after its figure's statement and any earlier correction of it.
    last = {c["variable_id"]: t for t, (c, _) in events.items()}
    for chain in corrections:
        after = [t for t in free if t > last[chain["variable_id"]]]
        if not after:
            continue
        turn = rng.choice(after[:max(1, len(after) // 2)])
        free.remove(turn)
        events[turn] = (chain, "corrected")
        last[chain["variable_id"]] = turn
    values = iter(rng.sample(variables.VALUES, len(events)))

    assignments = {c["variable_id"]: [] for c in chains}
    script_turns = []
    for turn in range(1, turns + 1):
        shards = []
        if turn in events:
            chain, kind = events[turn]
            history = assignments[chain["variable_id"]]
            value = next(values)
            if kind == "stated":
                text = variables._say(variables.FIRST, chain["phrase"], value, rng)
            else:
                text = rng.choice(CORRECT).format(phrase=chain["phrase"], value=number(value),
                                                  old=number(history[-1]["value"]))
                text = capitalize(text)
            shards.append(text)
            history.append({"turn": turn, "value": value, "kind": kind})
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
        corrected = [c for c in candidates if len(known(c)) > 1]
        chain = rng.choice(corrected or candidates)
        values_so_far = known(chain)
        current = values_so_far[-1]
        others = [a["value"] for c in chains if c is not chain for a in known(c)]
        question = rng.choice(variables.QUESTIONS).format(phrase=chain["phrase"])
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
                           "values": len(values_so_far), "corrections": len(values_so_far) - 1,
                           "chains": len(chains)},
        })

    return {
        "instructions": INSTRUCTIONS,
        "output_contract": OUTPUT_CONTRACT,
        "turns": script_turns,
        "probes": probes,
        "ground_truth": {
            "task": "corrections",
            "variables": [{"variable_id": c["variable_id"], "role": c["role"], "near_miss": c["near_miss"],
                           "of": c["of"], "phrase": c["phrase"],
                           "assignments": [{"turn": a["turn"], "value": str(a["value"]), "kind": a["kind"]}
                                           for a in assignments[c["variable_id"]]]}
                          for c in chains],
        },
    }
