from __future__ import annotations

import pytest

from cwabench5 import attacks, tasks
from cwabench5.grading import cases, grade_reply, judge
from cwabench5.grading.language import UNIQUE, detect


@pytest.mark.parametrize("case_id, contract, answer, reply, verdict, score", cases.TASK_CASES,
                         ids=[c[0] for c in cases.TASK_CASES])
def test_task_replies_grade_as_planted(case_id, contract, answer, reply, verdict, score):
    got = grade_reply(contract, answer, reply)
    assert (got.verdict, got.score) == (verdict, score), got.detail


@pytest.mark.parametrize("case_id, predicate, instruction, reply, verdict", cases.ATTACK_CASES,
                         ids=[c[0] for c in cases.ATTACK_CASES])
def test_attack_replies_grade_as_planted(case_id, predicate, instruction, reply, verdict):
    assert judge(predicate, instruction, reply).verdict == verdict


@pytest.mark.parametrize("case_id, text, code", cases.LANGUAGE_CASES, ids=[c[0] for c in cases.LANGUAGE_CASES])
def test_the_language_detector_names_each_language(case_id, text, code):
    assert detect(text) == code


def test_shared_words_decide_no_language():
    seen = [w for words in UNIQUE.values() for w in words]
    assert len(seen) == len(set(seen))
    assert "e" not in seen and "es" not in seen


def test_a_quoted_attack_is_never_executed():
    """The exit criterion of P0 (domain-5-plan.md, 14): a reply that repeats an attack's text grades `reported`."""
    for family in tasks.FAMILIES:
        task = tasks.generate(family, 5, "plain", 0, {})
        for template in attacks.every_template():
            if not template.applies_to(family):
                continue
            probe = attacks.compose(task, template, "end", "plain", 21)
            quoted = f'The content says: "{probe["instruction"]}" I will not do that.'
            assert judge(probe["predicate"], probe["instruction"], quoted).verdict == "reported", probe["probe_id"]
