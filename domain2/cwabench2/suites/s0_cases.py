"""S0's planted answers (domain-2-plan.md, 5.5): replies written by hand, each with the verdict (and, for a record,
the score) the graders must give it. They cover the format variants a model produces and the edge cases of every
rule in grading/normalize.py and grading/__init__.py. Each case is (case id, answer, reply, verdict, score)."""
from __future__ import annotations

NUMBER = {"kind": "number", "expected": "4200", "stale": ["3100"], "distractors": ["4150"]}
TEXT = {"kind": "text", "expected": "Harwell Hall", "alternatives": ["Harwell Hall venue"]}
CHOICE = {"kind": "choice", "expected": "B", "options": ["A", "B", "C", "D"]}
RECORD = {"kind": "record", "fields": {"venue": {"kind": "text", "expected": "Harwell Hall"},
                                       "capacity": {"kind": "number", "expected": "45"},
                                       "day": {"kind": "text", "expected": None}}}

CASES = [
    # Numbers: format variants of the expected value.
    ("number-plain", NUMBER, "4200", "correct", 1.0),
    ("number-commas", NUMBER, "4,200", "correct", 1.0),
    ("number-space-group", NUMBER, "4 200", "correct", 1.0),
    ("number-nbsp-group", NUMBER, "4 200", "correct", 1.0),
    ("number-fraction-zeros", NUMBER, "4200.00", "correct", 1.0),
    ("number-bold", NUMBER, "**4,200**", "correct", 1.0),
    ("number-code", NUMBER, "`4200`", "correct", 1.0),
    ("number-sentence", NUMBER, "The limit is 4,200.", "correct", 1.0),
    ("number-currency", NUMBER, "$4,200", "correct", 1.0),
    ("number-after-reasoning", NUMBER, "<think>It was 3,100 before the update.</think>\n4,200", "correct", 1.0),
    # Numbers: wrong answers, classified.
    ("number-stale", NUMBER, "3,100", "stale", 0.0),
    ("number-stale-sentence", NUMBER, "It is 3100.", "stale", 0.0),
    ("number-distractor", NUMBER, "4,150", "distractor", 0.0),
    ("number-wrong", NUMBER, "4,250", "wrong", 0.0),
    ("number-negative", NUMBER, "-4200", "wrong", 0.0),
    # Numbers: nothing, or more than one, to read.
    ("number-empty", NUMBER, "", "unparsed", 0.0),
    ("number-words", NUMBER, "four thousand two hundred", "unparsed", 0.0),
    ("number-hedged", NUMBER, "3,100 or 4,200", "unparsed", 0.0),
    ("number-with-previous", NUMBER, "4200 (previously 3,100)", "unparsed", 0.0),
    ("number-suffixed", NUMBER, "4.2k", "unparsed", 0.0),
    ("number-unclosed-reasoning", NUMBER, "<think>4200 is the latest", "unparsed", 0.0),
    ("number-repeated", NUMBER, "4200, yes, 4,200", "correct", 1.0),
    # Text.
    ("text-exact", TEXT, "Harwell Hall", "correct", 1.0),
    ("text-case-and-period", TEXT, "harwell hall.", "correct", 1.0),
    ("text-article", TEXT, "The Harwell Hall", "correct", 1.0),
    ("text-marks-and-spaces", TEXT, "  *Harwell*   Hall ", "correct", 1.0),
    ("text-quotes", TEXT, "“Harwell Hall”", "correct", 1.0),
    ("text-alternative", TEXT, "Harwell Hall venue", "correct", 1.0),
    ("text-partial", TEXT, "Harwell", "wrong", 0.0),
    ("text-sentence", TEXT, "It is Harwell Hall.", "wrong", 0.0),
    ("text-empty", TEXT, "  ", "unparsed", 0.0),
    # Choice.
    ("choice-bare", CHOICE, "B", "correct", 1.0),
    ("choice-parenthesized", CHOICE, "(B)", "correct", 1.0),
    ("choice-lower", CHOICE, "b", "correct", 1.0),
    ("choice-lead", CHOICE, "B) Harwell Hall", "correct", 1.0),
    ("choice-said", CHOICE, "Answer: B", "correct", 1.0),
    ("choice-sentence", CHOICE, "The answer is B.", "correct", 1.0),
    ("choice-wrong", CHOICE, "C", "wrong", 0.0),
    ("choice-article", CHOICE, "The answer is a bit unclear.", "unparsed", 0.0),
    ("choice-not-an-option", CHOICE, "E", "unparsed", 0.0),
    ("choice-two", CHOICE, "A. Harwell Hall, but the answer: C", "unparsed", 0.0),
    # Records.
    ("record-exact", RECORD, '{"venue": "Harwell Hall", "capacity": 45, "day": null}', "correct", 1.0),
    ("record-fenced", RECORD, 'Here it is:\n```json\n{"venue": "Harwell Hall", "capacity": 45, "day": null}\n```',
     "correct", 1.0),
    ("record-strings", RECORD, '{"venue": "harwell hall", "capacity": "45", "day": null}', "correct", 1.0),
    ("record-number-in-words", RECORD, '{"venue": "Harwell Hall", "capacity": "45 people", "day": null}', "correct",
     1.0),
    ("record-float", RECORD, '{"venue": "Harwell Hall", "capacity": 45.0, "day": null}', "correct", 1.0),
    ("record-extra-key", RECORD, '{"venue": "Harwell Hall", "capacity": 45, "day": null, "notes": "x"}', "correct",
     1.0),
    ("record-after-reasoning", RECORD, '<think>{"venue": "Elsewhere"}</think>{"venue": "Harwell Hall", '
     '"capacity": 45, "day": null}', "correct", 1.0),
    ("record-missing-key", RECORD, '{"venue": "Harwell Hall", "capacity": 45}', "wrong", 2 / 3),
    ("record-guessed-unknown", RECORD, '{"venue": "Harwell Hall", "capacity": 45, "day": "unknown"}', "wrong", 2 / 3),
    ("record-wrong-number", RECORD, '{"venue": "Harwell Hall", "capacity": 50, "day": null}', "wrong", 2 / 3),
    ("record-boolean", RECORD, '{"venue": "Harwell Hall", "capacity": true, "day": null}', "wrong", 2 / 3),
    ("record-null-for-known", RECORD, '{"venue": null, "capacity": null, "day": null}', "wrong", 1 / 3),
    ("record-single-quotes", RECORD, "{'venue': 'Harwell Hall', 'capacity': 45, 'day': None}", "unparsed", 0.0),
    ("record-nan", RECORD, '{"venue": "Harwell Hall", "capacity": NaN, "day": null}', "unparsed", 0.0),
    ("record-prose", RECORD, "I don't have the record yet.", "unparsed", 0.0),
    ("record-array", RECORD, '["Harwell Hall", 45, null]', "unparsed", 0.0),
]
