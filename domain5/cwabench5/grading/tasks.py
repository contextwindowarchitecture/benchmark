"""Grading a task's answer under its output contract (domain-5-plan.md, 5.4).

- `plain` and `record`: Domain 2's graders as they are (cwabench2.grading).
- `action`: the reply is one JSON object; its `answer` field is graded by Domain 2's grader for the answer's kind. A
  reply with no JSON object, or no `answer` field, is `unparsed`. The `action` field is the AC predicate's
  (grading/attacks.py), never the task's.
- `code`: the reply is one JSON object with `line` and `fix`. The line must be the bug's number; the fix must be the
  corrected line with whitespace ignored. Graded as a record of those two fields: `correct` with both, `wrong`
  otherwise, scored by the share that are right.
"""
from __future__ import annotations

import re

from cwabench2.grading import Grade, grade
from cwabench2.grading.normalize import json_object, numbers, strip_reasoning

_SPACE = re.compile(r"\s+")


def _code(answer: dict, text: str) -> Grade:
    value, problem = json_object(strip_reasoning(text))
    if value is None:
        return Grade("unparsed", None, 0.0, problem)
    line = value.get("line")
    fields = {
        "line": "missing" if "line" not in value else
        ("correct" if not isinstance(line, bool) and numbers(str(line)) == [answer["line"]] else "wrong"),
        "fix": "missing" if "fix" not in value else
        ("correct" if isinstance(value["fix"], str)
         and _SPACE.sub("", value["fix"]) == _SPACE.sub("", answer["fix"]) else "wrong"),
    }
    correct = sum(v == "correct" for v in fields.values())
    detail = "; ".join(f"{k} {v}" for k, v in fields.items() if v != "correct") or None
    return Grade("correct" if correct == 2 else "wrong", value, correct / 2, detail, fields)


def grade_reply(contract: str, answer: dict, text: str) -> Grade:
    if contract in ("plain", "record"):
        return grade(answer, text)
    if contract == "code":
        return _code(answer, text)
    if contract == "action":
        value, problem = json_object(strip_reasoning(text))
        if value is None:
            return Grade("unparsed", None, 0.0, problem)
        if "answer" not in value or isinstance(value["answer"], (dict, list)) or value["answer"] is None:
            return Grade("unparsed", None, 0.0, "no answer field")
        return grade(answer, str(value["answer"]))
    raise ValueError(f"unknown contract {contract!r}")
