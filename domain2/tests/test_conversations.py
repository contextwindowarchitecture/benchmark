from __future__ import annotations

import copy

import pytest

from cwabench2 import output
from cwabench2.conversations import check, checkpoints, generate

VT = {"variables": 3, "distractors": 2, "assignment_density": 0.5, "filler_sentences": 2, "reply_sentences": 2}
FR = {"tasks": ["record", "compute"], "fields": 6, "steps": 6, "filler_sentences": 2, "reply_sentences": 2}


def scripts():
    for turns in (10, 37, 100):
        for index in range(3):
            yield generate("vt", 5, turns, index, 10, VT)
            yield generate("fr", 6, turns, index, 10, FR)


@pytest.mark.parametrize("script", list(scripts()), ids=lambda s: s["conversation_id"])
def test_every_script_validates_and_says_its_ground_truth(script):
    output.validate(script)
    assert check.structure(script) == []
    assert check.replay(script) == []


def test_scripts_are_deterministic_and_seeded():
    assert generate("vt", 5, 50, 0, 10, VT) == generate("vt", 5, 50, 0, 10, VT)
    assert generate("vt", 5, 50, 0, 10, VT)["turns"] != generate("vt", 5, 50, 1, 10, VT)["turns"]
    assert generate("vt", 5, 50, 0, 10, VT)["turns"] != generate("vt", 6, 50, 0, 10, VT)["turns"]
    assert [generate("fr", 6, 20, i, 10, FR)["ground_truth"]["task"] for i in range(3)] == ["record", "compute",
                                                                                            "record"]


def test_checkpoints_follow_every_tenth_turn_and_the_last():
    assert checkpoints(10, 10) == [10]
    assert checkpoints(25, 10) == [10, 20, 25]
    assert checkpoints(5, 10) == [5]


def test_vt_values_are_distinct_and_probes_classify_them():
    script = generate("vt", 5, 100, 0, 10, VT)
    values = [a["value"] for v in script["ground_truth"]["variables"] for a in v["assignments"]]
    assert len(values) == len(set(values)) == 50
    last = script["probes"][-1]["answer"]
    assert last["expected"] not in last["stale"] + last["distractors"]
    assert any(p["answer"]["stale"] for p in script["probes"])  # a long conversation reassigns
    assert {v["near_miss"] for v in script["ground_truth"]["variables"]} == {None, "name", "attribute"}


def _mutated(script, change):
    script = copy.deepcopy(script)
    change(script)
    return script


def test_the_checker_catches_text_that_disagrees_with_the_ground_truth():
    vt = generate("vt", 5, 50, 0, 10, VT)
    turn = next(t for t in vt["turns"] if t["shards"])
    shard = turn["shards"][0]
    value = shard.rstrip(".").split()[-1]

    def change_value(s):
        t = s["turns"][turn["turn"] - 1]
        t["user"] = t["user"].replace(value, "9,999")
        t["shards"] = [shard.replace(value, "9,999")]

    assert any("assignments differ" in p for p in check.replay(_mutated(vt, change_value)))
    assert any("answer differs" in p for p in check.replay(_mutated(
        vt, lambda s: s["probes"][-1]["answer"].update(expected="1"))))
    assert any("needs" in p for p in check.replay(_mutated(vt, lambda s: s["probes"][0].update(needs=["t001"]))))
    assert any("digit outside the shards" in p for p in check.structure(_mutated(
        vt, lambda s: s["turns"][0].update(user=s["turns"][0]["user"] + " Room 12 is free."))))
    assert any("not in the user text" in p for p in check.structure(_mutated(
        vt, lambda s: s["turns"][turn["turn"] - 1].update(shards=["Something else."]))))

    record = generate("fr", 6, 20, 0, 10, FR)
    assert record["ground_truth"]["task"] == "record"
    field = record["ground_truth"]["fields"][0]
    assert any(field["field"] in p for p in check.replay(_mutated(
        record, lambda s: s["turns"][-1].update(user=s["turns"][-1]["user"] + " " + (
            f"{int(field['value']):,}" if field["kind"] == "number" else field["value"]) + "."))))

    compute = generate("fr", 6, 20, 1, 10, FR)
    assert any("total differs" in p or "order lines differ" in p for p in check.replay(_mutated(
        compute, lambda s: s["ground_truth"].update(total="1"))))
    assert any("answer differs" in p for p in check.replay(_mutated(
        compute, lambda s: s["probes"][0]["answer"]["stale"].append("3"))))


def test_parameters_that_cannot_fit_are_refused():
    with pytest.raises(ValueError, match="at least 6 turns"):
        generate("fr", 6, 5, 0, 10, FR)
    with pytest.raises(ValueError, match="tasks"):
        generate("fr", 6, 20, 0, 10, {**FR, "tasks": ["essay"]})
    with pytest.raises(ValueError, match="tracked"):
        generate("vt", 5, 20, 0, 10, {**VT, "variables": 0})
