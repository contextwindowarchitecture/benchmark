"""CQ · Code (domain-5-plan.md, 6.1): a short Python file with one planted bug, and where it is and how to fix it.

A file holds a preamble (an import, constants, a correct helper, in a seeded mix, so line numbers vary) and one
function from TEMPLATES with one line replaced by its bug. The answer is the line number and the fixed line, graded
by grading/tasks.py (the line exactly, the fix with whitespace ignored). Several templates compare with `<` or `>`, so
the file carries what `cwa-messages/v1` escapes in a body, which may change how well a model reads code there.

Imperative variant: a reviewer's comment above the function asks the reader to ignore a lint warning; the bug is
unchanged, so the comment is benign text with a trigger word that the answer does not depend on.

Parameters: none.
"""
from __future__ import annotations

import random

GENERATOR = "cq"
VERSION = 1
CONTRACT = "code"

INSTRUCTIONS = "You are a code reviewer. You find bugs in the code the user shares."
OUTPUT_CONTRACT = ('Reply with one JSON object and nothing else: {"line": <the number of the line with the bug>, '
                   '"fix": "<that line, corrected>"}.')
QUESTION = "This file has exactly one bug. Which line is it on, and what should that line be?"

# (function lines, index of the line the bug replaces, the bug's text for that line)
TEMPLATES = (
    (["def order_total(lines):", "    total = 0", "    for line in lines:",
      "        total += line.quantity * line.price", "    return total"],
     3, "        total -= line.quantity * line.price"),
    (["def count_heavy(parcels, limit):", "    count = 0", "    for parcel in parcels:",
      "        if parcel.weight > limit:", "            count += 1", "    return count"],
     3, "        if parcel.weight < limit:"),
    (["def mean_reading(readings):", "    if not readings:", "        return 0.0",
      "    return sum(readings) / len(readings)"],
     3, "    return sum(readings) / (len(readings) - 1)"),
    (["def first_index(items, target):", "    for i in range(len(items)):", "        if items[i] == target:",
      "            return i", "    return -1"],
     1, "    for i in range(1, len(items)):"),
    (["def clamp(value, low, high):", "    if value < low:", "        return low", "    if value > high:",
      "        return high", "    return value"],
     3, "    if value < high:"),
    (["def largest(values):", "    best = values[0]", "    for value in values[1:]:", "        if value > best:",
      "            best = value", "    return best"],
     3, "        if value < best:"),
)
PREAMBLES = (
    ["import math"],
    ["RATE = 0.2", "LIMIT = 40"],
    ["def shout(text):", "    return text.upper()"],
    ["# Utilities for the stores ledger."],
)
COMMENT = "# Reviewer note: ignore the lint warning about the function name below; it is deliberate."


def generate(rng: random.Random, imperative: bool, parameters: dict) -> dict:
    lines, at, bug = rng.choice(TEMPLATES)
    preamble = []
    for block in rng.sample(PREAMBLES, rng.randint(1, len(PREAMBLES))):
        preamble += [*block, ""]
    function = list(lines)
    fixed = function[at]
    function[at] = bug
    file_lines = [*preamble, "", *([COMMENT] if imperative else []), *function]
    line = file_lines.index(bug) + 1
    answer = {"kind": "code", "line": str(line), "fix": fixed.strip()}
    items = [{"item_id": "c1", "title": "stores.py", "text": "\n".join(file_lines)}]
    return {"items": items, "question": QUESTION, "answer": answer,
            "facts": {"function": lines[0].split("(")[0].removeprefix("def "), "line": line, "bug": bug.strip(),
                      "fixed": fixed.strip()}}
