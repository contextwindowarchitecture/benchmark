"""S0's planted answers (domain-2-plan.md, 5.5): replies written by hand, each with the verdict (and, for a record,
the score) the graders must give it. They cover the format variants a model produces and the edge cases of every
rule in grading/normalize.py and grading/__init__.py. Each case is (case id, answer, reply, verdict, score)."""
from __future__ import annotations

NUMBER = {"kind": "number", "expected": "4200", "stale": ["3100"], "distractors": ["4150"]}
TOTAL = {"kind": "number", "marker": "Total:", "expected": "1295", "stale": ["1210"], "distractors": []}
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
    # Numbers after a marker: only the last marked line is read, so the working may hold other numbers.
    ("total-plain", TOTAL, "Total: 1295", "correct", 1.0),
    ("total-working", TOTAL, "450 + 35 = 485\n485 + 810 = 1,295\nTotal: 1,295", "correct", 1.0),
    ("total-bold", TOTAL, "Adding the lines: 450, 35, 810.\n**Total:** 1295", "correct", 1.0),
    ("total-last-wins", TOTAL, "Total: 1210 before the last line.\nTotal: 1295", "correct", 1.0),
    ("total-stale", TOTAL, "450 + 760 = 1210\nTotal: 1210", "stale", 0.0),
    ("total-wrong", TOTAL, "Total: 1300", "wrong", 0.0),
    ("total-missing", TOTAL, "1295", "unparsed", 0.0),
    ("total-trailing-working", TOTAL, "Total: 1295 (450 + 35 + 810)", "unparsed", 0.0),
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


# The baselines against hand-computed payloads (domain-2-plan.md, 8, S0). A three-turn conversation counted by hand
# with estimate-utf8/v1 (UTF-8 bytes ÷ 4, rounded up), no margin, and a two-turn window:
#
#   system  "Keep track.\n\nReply briefly."    27 bytes → 7     query "What is Alpha?"   14 → 4
#   t1      user "Alpha is 1000."   14 → 4       assistant "Noted."   6 → 2
#   t2      user "Beta is 2000."    13 → 4       assistant "Noted."   6 → 2
#   t3      user "Alpha is 3000."   14 → 4       assistant "Noted."   6 → 2
#   summary "Summary of the earlier conversation:\nAlpha is 1000."   51 → 13  (the stub keeps t1's one sentence)
#
# The whole conversation is 7 + 3 × 6 + 4 = 29. Each case gives (arm, budget, outcome, input_tokens, kept, first_kept,
# system_survived), where kept is the payload's messages in order and first_kept the oldest prior-turn message kept.
BASELINE_SCRIPT = {
    "conversation_id": "s0-t003-00",
    "instructions": "Keep track.",
    "output_contract": "Reply briefly.",
    "turns": [
        {"turn": 1, "id": "t001", "user": "Alpha is 1000.", "assistant": "Noted.", "shards": ["Alpha is 1000."]},
        {"turn": 2, "id": "t002", "user": "Beta is 2000.", "assistant": "Noted.", "shards": ["Beta is 2000."]},
        {"turn": 3, "id": "t003", "user": "Alpha is 3000.", "assistant": "Noted.", "shards": ["Alpha is 3000."]},
    ],
    "probes": [{"probe_id": "p003", "after_turn": 3, "question": "What is Alpha?", "needs": ["t003"]}],
    "ground_truth": {"task": "variables", "variables": [
        {"variable_id": "v1", "phrase": "Alpha",
         "assignments": [{"turn": 1, "value": "1000"}, {"turn": 3, "value": "3000"}]},
        {"variable_id": "v2", "phrase": "Beta", "assignments": [{"turn": 2, "value": "2000"}]}]},
}
U1, A1, U2, A2, U3, A3, Q = ("turn:t001:user", "turn:t001:assistant", "turn:t002:user", "turn:t002:assistant",
                             "turn:t003:user", "turn:t003:assistant", "probe:p003")
BASELINE_CASES = [
    # concat sends everything: 29 fits 29, and overflows any smaller budget.
    ("concat", 29, "fits", 29, ["system", U1, A1, U2, A2, U3, A3, Q], U1, True),
    ("concat", 20, "overflow", 29, ["system", U1, A1, U2, A2, U3, A3, Q], U1, True),
    # truncate drops from the front, the system prompt first: at 20, the system (7) and t1's user (4) go, leaving 18.
    ("truncate", 29, "fits", 29, ["system", U1, A1, U2, A2, U3, A3, Q], U1, True),
    ("truncate", 20, "fits", 18, [A1, U2, A2, U3, A3, Q], A1, False),
    ("truncate", 12, "fits", 12, [A2, U3, A3, Q], A2, False),
    # truncate-pinned keeps the system prompt and the query (11): at 20 the history may have 9, at 12 only 1.
    ("truncate-pinned", 29, "fits", 29, ["system", U1, A1, U2, A2, U3, A3, Q], U1, True),
    ("truncate-pinned", 20, "fits", 19, ["system", A2, U3, A3, Q], A2, True),
    ("truncate-pinned", 12, "fits", 11, ["system", Q], None, True),
    ("truncate-pinned", 10, "overflow", 11, ["system", Q], None, True),
    # window keeps t2 and t3 (12), then truncates as pinned.
    ("window", 29, "fits", 23, ["system", U2, A2, U3, A3, Q], U2, True),
    ("window", 20, "fits", 19, ["system", A2, U3, A3, Q], A2, True),
    ("window", 12, "fits", 11, ["system", Q], None, True),
    # summary adds t1's summary (13) after the system prompt; under pressure the summary goes first, then the window's
    # oldest messages: at 29 the history may have 18, so the summary goes; at 20 it may have 9, so t2's user goes too.
    ("summary", 40, "fits", 36, ["system", "summary", U2, A2, U3, A3, Q], U2, True),
    ("summary", 29, "fits", 23, ["system", U2, A2, U3, A3, Q], U2, True),
    ("summary", 20, "fits", 19, ["system", A2, U3, A3, Q], A2, True),
    ("summary", 12, "fits", 11, ["system", Q], None, True),
]
# Two payloads written out byte for byte: JCS orders members by name, and native chat escapes nothing beyond JSON.
BASELINE_PAYLOADS = {
    ("truncate", 12): '{"messages":[{"content":"Noted.","role":"assistant"},{"content":"Alpha is 3000.","role":"user"},'
                      '{"content":"Noted.","role":"assistant"},{"content":"What is Alpha?","role":"user"}],'
                      '"system":[],"tools":[]}',
    ("summary", 40): '{"messages":[{"content":"Beta is 2000.","role":"user"},{"content":"Noted.","role":"assistant"},'
                     '{"content":"Alpha is 3000.","role":"user"},{"content":"Noted.","role":"assistant"},'
                     '{"content":"What is Alpha?","role":"user"}],"system":[{"id":"system","text":"Keep track.'
                     '\\n\\nReply briefly."},{"id":"summary","text":"Summary of the earlier conversation:'
                     '\\nAlpha is 1000."}],'
                     '"tools":[]}',
}


# IP's compliance checks (grading/compliance.py): (case id, rule, reply, complies).
COMPLIANCE_CASES = [
    ("brackets-plain", "brackets", "[4,725]", True),
    ("brackets-sentence", "brackets", "It is [4,725] now.", True),
    ("brackets-missing", "brackets", "4,725", False),
    ("brackets-no-figure", "brackets", "[see above] 4,725", False),
    ("signoff-plain", "signoff", "4,725. Kestrel desk", True),
    ("signoff-punctuated", "signoff", "4,725\n\n— Kestrel desk.", True),
    ("signoff-bold-case", "signoff", "4,725 **kestrel Desk**", True),
    ("signoff-not-last", "signoff", "Kestrel desk: 4,725", False),
    ("uppercase-plain", "uppercase", "THE LIMIT IS 4,725.", True),
    ("uppercase-one-lower", "uppercase", "THE LIMIT Is 4,725.", False),
    ("uppercase-digits-only", "uppercase", "4,725", False),
    ("any-empty", "signoff", "", False),
    ("any-reasoning", "uppercase", "<think>let me see</think>4,725 IS IT", True),
]
