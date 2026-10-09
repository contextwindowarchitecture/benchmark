"""The values each turn states, from the ground truth, and whether a text carries them.

A model-based producer (application/producers.py) rewrites what the user said: the extractor into state, the rolling
summarizer into a summary. Neither keeps the fact sentences, so whether its output carries a turn's facts is judged by
the values: a figure as a number, compared by value; a booking field's text by its normalized key. The fact-in-payload
oracle then treats that output as a carrier of the turn, with its own text as the evidence (application/fact.py).
"""
from __future__ import annotations

from ..grading.normalize import numbers, text_key


def turn_values(script: dict) -> dict[str, list[tuple[str, str]]]:
    """turn id → [(kind, value)]: what each fact-carrying turn states, as `number` or `text`."""
    truth = script["ground_truth"]
    found: dict[str, list[tuple[str, str]]] = {}
    if truth["task"] in ("variables", "corrections"):
        for variable in truth["variables"]:
            for a in variable["assignments"]:
                found.setdefault(f"t{a['turn']:03d}", []).append(("number", a["value"]))
    elif truth["task"] == "record":
        for field in truth["fields"]:
            found.setdefault(f"t{field['turn']:03d}", []).append((field["kind"], field["value"]))
    else:
        for step in truth["steps"]:
            found.setdefault(f"t{step['turn']:03d}", []).append(("number", str(abs(step["amount"]))))
    return found


def carries(text: str, values: list[tuple[str, str]]) -> bool:
    """Whether `text` states every value."""
    if not values:
        return False
    read = set(numbers(text))
    key = text_key(text)
    for kind, value in values:
        if kind == "number":
            if numbers(value)[0] not in read:
                return False
        elif text_key(value) not in key:
            return False
    return True
