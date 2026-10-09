from __future__ import annotations

import pytest

from cwabench2.grading import grade
from cwabench2.grading.normalize import json_object, numbers, strip_reasoning, text_key
from cwabench2.suites.s0_cases import CASES


@pytest.mark.parametrize("case_id, answer, reply, verdict, score", CASES, ids=[c[0] for c in CASES])
def test_planted_cases(case_id, answer, reply, verdict, score):
    got = grade(answer, reply)
    assert (got.verdict, round(got.score, 9)) == (verdict, round(score, 9)), got.detail


def test_numbers_read_groupings_and_refuse_glued_ones():
    assert numbers("4,200 and 4 200 and 4_200 and 4200.0") == ["4200"]
    assert numbers("12,345,678") == ["12345678"]
    assert numbers("1,2,3") == ["1", "2", "3"]  # not grouped in threes: three numbers
    assert numbers("A3B, 4.2k, v1.2") == []
    assert numbers("0.50 and -0") == ["0.5", "0"]


def test_reasoning_text_keys_and_json():
    assert strip_reasoning("<THINK>x</THINK> y") == "y"
    assert strip_reasoning("<think>never closed") == ""
    assert text_key("  The ‘Harwell’  Hall!") == "'harwell' hall"
    assert json_object('```\n{"a": 1}\n``` and {"b": 2}') == ({"a": 1}, None)
    assert json_object("[1]")[0] is None


def test_record_lists_extra_keys_and_field_verdicts():
    answer = {"kind": "record", "fields": {"a": {"kind": "number", "expected": "1"},
                                           "b": {"kind": "text", "expected": None}}}
    got = grade(answer, '{"a": 1, "c": 3}')
    assert got.verdict == "wrong" and got.fields == {"a": "correct", "b": "missing"}
    assert "extra keys: c" in got.detail and got.as_json()["fields"] == {"a": "correct", "b": "missing"}
