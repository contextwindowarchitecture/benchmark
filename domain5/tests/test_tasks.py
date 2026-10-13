from __future__ import annotations

import copy

import pytest

from cwabench5 import output, tasks
from cwabench5.tasks import check

FAMILIES = tuple(tasks.FAMILIES)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("variant", tasks.VARIANTS)
def test_instances_validate_and_say_what_their_ground_truth_says(family, variant):
    for index in range(60):
        task = tasks.generate(family, 11, variant, index, {})
        output.validate(task)
        assert check.structure(task) == [], task["task_id"]
        assert check.replay(task) == [], task["task_id"]


@pytest.mark.parametrize("family", FAMILIES)
def test_an_instance_is_its_seed_and_index_alone(family):
    first = tasks.generate(family, 11, "plain", 3, {})
    assert output.dumps(first) == output.dumps(tasks.generate(family, 11, "plain", 3, {}))
    assert first["task_id"] == f"{family}-p03"
    assert output.dumps(first) != output.dumps(tasks.generate(family, 12, "plain", 3, {}))
    assert tasks.generate(family, 11, "imperative", 3, {})["task_id"] == f"{family}-i03"


@pytest.mark.parametrize("family", FAMILIES)
def test_the_reference_code_is_in_the_instructions_only(family):
    task = tasks.generate(family, 11, "plain", 0, {})
    assert task["reference_code"] in task["instructions"]
    assert all(task["reference_code"] not in i["text"] for i in task["items"])


def test_an_imperative_instance_answers_from_its_benign_instruction():
    email = tasks.generate("em", 11, "imperative", 0, {})
    assert email["answer"]["stale"] and email["facts"]["correction"]["amount"] == int(email["answer"]["expected"])
    table = tasks.generate("tq", 11, "imperative", 0, {})
    assert table["answer"]["stale"] and "entered twice" in table["items"][0]["text"]
    digest = tasks.generate("dg", 11, "imperative", 0, {})
    assert digest["answer"]["fields"]["venue"]["expected"] == digest["facts"]["correction"]["new"]
    selection = tasks.generate("sl", 11, "imperative", 0, {})
    assert selection["items"][-1]["title"] == "Price update"


@pytest.mark.parametrize("family", ("em", "dq", "tq", "sl"))
def test_the_check_catches_a_wrong_answer(family):
    task = copy.deepcopy(tasks.generate(family, 11, "plain", 1, {}))
    task["answer"]["expected"] = "Nowhere at all"
    assert check.replay(task)


def test_the_code_check_runs_the_file():
    task = copy.deepcopy(tasks.generate("cq", 11, "plain", 1, {}))
    assert check.replay(task) == []
    task["answer"]["line"] = str(int(task["answer"]["line"]) - 1)
    assert any("does not pass" in p for p in check.replay(task))
    task = copy.deepcopy(tasks.generate("cq", 11, "plain", 1, {}))
    task["answer"]["fix"] = "pass"
    assert check.replay(task)


def test_a_plain_instance_with_a_trigger_word_fails_the_structure_check():
    task = copy.deepcopy(tasks.generate("dq", 11, "plain", 0, {}))
    task["items"][0]["text"] += " Please ignore the gift shop."
    assert any("trigger" in p for p in check.structure(task))
