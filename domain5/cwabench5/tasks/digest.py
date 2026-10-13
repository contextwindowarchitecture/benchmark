"""DG · Digest (domain-5-plan.md, 6.1): short notes about an event, and its key facts as a JSON record.

Four facts (the venue, the day, the organiser and the number expected) are each stated in one note among notes that
state none. The answer is a record of all four, graded field by field (Domain 2's record grader).

Imperative variant: a later note cancels the venue from the earlier note and names another; the record's venue is
the new one.

Parameters: `notes` ([least, most]).
"""
from __future__ import annotations

import random

from .vocab import FIRST, WEEKDAYS, site_names

GENERATOR = "dg"
VERSION = 2  # 2: no two notes alike, so exact deduplication (R-24) never drops one
CONTRACT = "record"

INSTRUCTIONS = "You are an assistant who keeps the user's notes in order and summarizes them on request."
OUTPUT_CONTRACT = ('Reply with one JSON object and nothing else, with the keys "venue", "day", "organiser" and '
                   '"attendees".')

FACTS = {
    "venue": "The gathering will be held at the {value}.",
    "day": "It takes place on {value}.",
    "organiser": "{value} is organising it.",
    "attendees": "We expect {value} people to come.",
}
CORRECTION = "Cancel the {old} booking from the earlier note; we are using the {new} instead."
FILLER = ("Remember to bring the projector.", "Ask about step-free access at the entrance.",
          "The coat rack needs replacing.", "Send the agenda round beforehand.", "Order more name badges.",
          "Check whether the side room has chairs.", "Someone should test the microphones.")
QUESTION = "Summarize the gathering in my notes: the venue, the day, the organiser and how many people are expected."


def generate(rng: random.Random, imperative: bool, parameters: dict) -> dict:
    least, most = parameters.get("notes", [4, 10])
    count = rng.randint(max(least, 5 if imperative else 4), most)
    names = site_names(rng)
    venue, replacement = f"{names[0]} hall", f"{names[1]} hall"
    facts = {"venue": venue, "day": rng.choice(WEEKDAYS), "organiser": rng.choice(FIRST),
             "attendees": rng.randint(12, 400)}
    stated = list(FACTS)
    rng.shuffle(stated)
    slots = sorted(rng.sample(range(count - (1 if imperative else 0)), len(stated)))
    fillers = iter(rng.sample(FILLER, count - len(stated) - (1 if imperative else 0)))
    notes = [next(fillers) if n not in slots else "" for n in range(count - (1 if imperative else 0))]
    notes += [""] if imperative else []
    for slot, fact in zip(slots, stated):
        notes[slot] = FACTS[fact].format(value=facts[fact])
    correction = None
    if imperative:
        correction = {"old": venue, "new": replacement}
        notes[-1] = CORRECTION.format(old=venue, new=replacement)
    items = [{"item_id": f"n{i + 1}", "title": f"Note {i + 1}", "text": text} for i, text in enumerate(notes)]
    expected = {**facts, "venue": replacement if imperative else venue}
    answer = {"kind": "record", "fields": {
        "venue": {"kind": "text", "expected": expected["venue"]},
        "day": {"kind": "text", "expected": expected["day"]},
        "organiser": {"kind": "text", "expected": expected["organiser"]},
        "attendees": {"kind": "number", "expected": str(expected["attendees"])},
    }}
    return {"items": items, "question": QUESTION, "answer": answer,
            "facts": {"stated": facts, "correction": correction}}
