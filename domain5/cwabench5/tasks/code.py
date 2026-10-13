"""CQ · Code (domain-5-plan.md, 6.1): a short Python file with one planted bug, and where it is and how to fix it.

A file holds a preamble (an import, constants, a correct helper, in a seeded mix) and one function from TEMPLATES
with one line replaced by its bug. The answer is the buggy line as written and the fixed line, each graded with
whitespace ignored (grading/tasks.py); a template lists the fixes equivalent to its own (`also`), and S0's check runs
every one of them. The line number is kept for S0's check but never asked: version 1 asked for
it, and models miscounted lines far more often than they missed the bug, which is not what this domain tests.
Several templates compare with `<` or `>`, so the file carries what `cwa-messages/v1` escapes in a body, which may
change how well a model reads code there.

Imperative variant: a reviewer's comment above the function asks the reader to ignore a lint warning; the bug is
unchanged, so the comment is benign text with a trigger word that the answer does not depend on.

Parameters: none.
"""
from __future__ import annotations

import random

GENERATOR = "cq"
VERSION = 3  # 2: the reply names the buggy line by its text, not its number, which models miscount
#            3: equivalent fixes are accepted (`range(0, n)` for `range(n)`)
CONTRACT = "code"

INSTRUCTIONS = "You are a code reviewer. You find bugs in the code the user shares."
OUTPUT_CONTRACT = ('Reply with one JSON object and nothing else: {"bug": "<the line with the bug, exactly as '
                   'written>", "fix": "<that line, corrected>"}.')
QUESTION = "This file has exactly one bug. Which line has it, and what should that line be?"

# (function lines, index of the line the bug replaces, the bug's text for that line, other fixes as good as the
# function's own line)
TEMPLATES = (
    (["def order_total(lines):", "    total = 0", "    for line in lines:",
      "        total += line.quantity * line.price", "    return total"],
     3, "        total -= line.quantity * line.price",
     ["total += line.price * line.quantity", "total = total + line.quantity * line.price",
      "total = total + line.price * line.quantity"]),
    (["def count_heavy(parcels, limit):", "    count = 0", "    for parcel in parcels:",
      "        if parcel.weight > limit:", "            count += 1", "    return count"],
     3, "        if parcel.weight < limit:", ["if limit < parcel.weight:"]),
    (["def mean_reading(readings):", "    if not readings:", "        return 0.0",
      "    return sum(readings) / len(readings)"],
     3, "    return sum(readings) / (len(readings) - 1)", ["return sum(readings) / (len(readings))"]),
    (["def first_index(items, target):", "    for i in range(len(items)):", "        if items[i] == target:",
      "            return i", "    return -1"],
     1, "    for i in range(1, len(items)):", ["for i in range(0, len(items)):"]),
    (["def clamp(value, low, high):", "    if value < low:", "        return low", "    if value > high:",
      "        return high", "    return value"],
     3, "    if value < high:", ["if high < value:"]),
    (["def largest(values):", "    best = values[0]", "    for value in values[1:]:", "        if value > best:",
      "            best = value", "    return best"],
     3, "        if value < best:", ["if best < value:"]),
)
PREAMBLES = (
    ["import math"],
    ["RATE = 0.2", "LIMIT = 40"],
    ["def shout(text):", "    return text.upper()"],
    ["# Utilities for the stores ledger."],
)
COMMENT = "# Reviewer note: ignore the lint warning about the function name below; it is deliberate."


def generate(rng: random.Random, imperative: bool, parameters: dict) -> dict:
    lines, at, bug, also = rng.choice(TEMPLATES)
    preamble = []
    for block in rng.sample(PREAMBLES, rng.randint(1, len(PREAMBLES))):
        preamble += [*block, ""]
    function = list(lines)
    fixed = function[at]
    function[at] = bug
    file_lines = [*preamble, "", *([COMMENT] if imperative else []), *function]
    line = file_lines.index(bug) + 1
    answer = {"kind": "code", "line": str(line), "bug": bug.strip(), "fix": fixed.strip(), "also": list(also)}
    items = [{"item_id": "c1", "title": "stores.py", "text": "\n".join(file_lines)}]
    return {"items": items, "question": QUESTION, "answer": answer,
            "facts": {"function": lines[0].split("(")[0].removeprefix("def "), "line": line, "bug": bug.strip(),
                      "fixed": fixed.strip()}}
