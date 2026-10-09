"""Deterministic graders (domain-2-plan.md, 5.2): no judge model, nothing gates on one.

`grade(answer, text)` grades a reply against a probe's `answer` (conversations/__init__.py). Every grade has a
verdict, one of VERDICTS:

- `correct`;
- `stale`: an earlier value of what was asked (a reassigned variable, an earlier running total);
- `distractor`: the value of something else in the conversation;
- `wrong`: any other answer;
- `unparsed`: no answer could be read, or more than one (two different numbers, two letters, no JSON object).
  A parse failure is its own outcome, never a wrong answer.

Kinds of answer:

- `number`: exactly one distinct number in the reply, compared by value; `stale` and `distractors` list the values
  that classify a wrong one;
- `text`: the reply's text key equals the expected one's or an alternative's (normalize.text_key);
- `choice`: exactly one of the options' letters, read from a bare letter ("B", "(B)", "B."), a lead ("B) …",
  "B. …") or "answer: B" / "the answer is B" (a capital letter there, so "the answer is a …" reads as no letter);
  another option is `wrong`;
- `record`: a JSON object (normalize.json_object) with every expected key; each field is a number or a text, or must
  be null when its expected value is null. The record is `correct` when every field is, `wrong` otherwise, and its
  score is the share of fields that are correct. A missing key is a wrong field; keys not asked for are ignored and
  listed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .normalize import json_object, numbers, strip_reasoning, text_key

VERDICTS = ("correct", "stale", "distractor", "wrong", "unparsed")
FIELD_VERDICTS = ("correct", "wrong", "missing")


@dataclass(frozen=True)
class Grade:
    verdict: str
    answer: object  # what was read from the reply: a canonical number, a text key, a letter, an object, or None
    score: float  # 1.0 correct, 0.0 otherwise, or a record's share of correct fields
    detail: str | None = None
    fields: dict[str, str] = field(default_factory=dict)  # a record's verdict per field

    def as_json(self) -> dict:
        return {"verdict": self.verdict, "answer": self.answer, "score": self.score, "detail": self.detail,
                "fields": self.fields or None}


def _classified(value: str, answer: dict, expected: str) -> str:
    if value == expected:
        return "correct"
    if value in answer.get("stale", []):
        return "stale"
    if value in answer.get("distractors", []):
        return "distractor"
    return "wrong"


def grade_number(answer: dict, text: str) -> Grade:
    found = numbers(strip_reasoning(text))
    if not found:
        return Grade("unparsed", None, 0.0, "no number")
    if len(found) > 1:
        return Grade("unparsed", None, 0.0, f"several numbers: {', '.join(found[:5])}")
    expected = numbers(answer["expected"])[0]
    verdict = _classified(found[0], {k: [numbers(v)[0] for v in answer.get(k, [])] for k in ("stale", "distractors")},
                          expected)
    return Grade(verdict, found[0], 1.0 if verdict == "correct" else 0.0)


def grade_text(answer: dict, text: str) -> Grade:
    key = text_key(strip_reasoning(text))
    if not key:
        return Grade("unparsed", None, 0.0, "empty reply")
    accepted = [text_key(answer["expected"]), *(text_key(a) for a in answer.get("alternatives", []))]
    if key in accepted:
        return Grade("correct", key, 1.0)
    keys = {k: [text_key(v) for v in answer.get(k, [])] for k in ("stale", "distractors")}
    verdict = _classified(key, keys, accepted[0])
    return Grade(verdict, key, 0.0)


_BARE = re.compile(r"^\W*([A-Za-z])\W*$")
_LEAD = re.compile(r"^\W*([A-Za-z])[).:]\s")
_SAID = re.compile(r"\b(?:[Aa]nswer|ANSWER)(?:\s+is)?\s*[:\-]?\s*\(?([A-Z])\)?(?!\w)")  # a capital: not "is a …"


def grade_choice(answer: dict, text: str) -> Grade:
    reply = strip_reasoning(text).strip()
    options = [o.upper() for o in answer["options"]]
    letters = []
    for pattern in (_BARE, _LEAD, _SAID):
        letters += [m.upper() for m in pattern.findall(reply)]
    letters = sorted({letter for letter in letters if letter in options})
    if not letters:
        return Grade("unparsed", None, 0.0, "no option letter")
    if len(letters) > 1:
        return Grade("unparsed", None, 0.0, f"several option letters: {', '.join(letters)}")
    verdict = "correct" if letters[0] == answer["expected"].upper() else "wrong"
    return Grade(verdict, letters[0], 1.0 if verdict == "correct" else 0.0)


def _field(spec: dict, present: bool, value) -> str:
    if not present:
        return "missing"
    expected = spec["expected"]
    if expected is None:
        return "correct" if value is None else "wrong"
    if value is None or isinstance(value, (bool, dict, list)):
        return "wrong"
    text = str(value)
    if spec["kind"] == "number":
        found = numbers(text)
        return "correct" if found == numbers(expected) else "wrong"
    return "correct" if text_key(text) == text_key(expected) else "wrong"


def grade_record(answer: dict, text: str) -> Grade:
    value, problem = json_object(strip_reasoning(text))
    if value is None:
        return Grade("unparsed", None, 0.0, problem)
    verdicts = {name: _field(spec, name in value, value.get(name)) for name, spec in answer["fields"].items()}
    correct = sum(v == "correct" for v in verdicts.values())
    extra = sorted(set(value) - set(answer["fields"]))
    wrong = [f"{name} {verdict}" for name, verdict in verdicts.items() if verdict != "correct"]
    detail = "; ".join([*wrong, *([f"extra keys: {', '.join(extra)}"] if extra else [])]) or None
    score = correct / len(verdicts) if verdicts else 1.0
    return Grade("correct" if correct == len(verdicts) else "wrong", value, score, detail, verdicts)


GRADERS = {"number": grade_number, "text": grade_text, "choice": grade_choice, "record": grade_record}


def grade(answer: dict, text: str) -> Grade:
    return GRADERS[answer["kind"]](answer, text)
