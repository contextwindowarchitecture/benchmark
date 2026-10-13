from __future__ import annotations

import json
from collections import Counter

import pytest

from cwabench import gitinfo
from cwabench.contract import Contract
from cwabench5 import baselines, grid, handoff, tasks
from cwabench5.application import route
from cwabench5.application.snapshot import Settings, freeze
from cwabench5.arms import ARMS, CWA, DATAMARK
from cwabench5.oracles import presence, structure

SETTINGS = Settings(margin_percent=35)
FAMILIES = ["hj", "mf", "ad", "ex", "ac", "dm"]


@pytest.fixture(scope="module")
def contract(spec):
    return Contract(spec, gitinfo.inspect(spec).commit, True)


@pytest.fixture(scope="module")
def instances():
    return {f: [tasks.generate(f, 7, v, 0, {}) for v in tasks.VARIANTS] for f in tasks.FAMILIES}


def _cells(instances):
    return grid.cells(instances, FAMILIES, 10)


def test_the_route_and_profiles_are_the_specs(contract):
    assert route.check(contract) == []


def test_every_cwa_payload_validates_and_reads_back_item_for_item(contract, instances):
    by_id = {t["task_id"]: t for family in instances.values() for t in family}
    validator = contract.validator("snapshot.schema.json")
    for cell in _cells(instances):
        task = by_id[cell["task_id"]]
        for arm in CWA:
            probe = grid.probe(cell, task, arm, 1, "qwen")
            frozen = freeze(contract, task, probe["items"], arm, SETTINGS)
            assert not list(validator.iter_errors(json.loads(frozen.at()))), cell["cell_id"]
            read = structure.cwa(frozen.payload, frozen.snapshot, frozen.items)
            assert read.ok, (cell["cell_id"], arm, read.problems)
            if probe["attack_text"] is not None:
                cid = frozen.content.get(probe["item_id"])
                by_record = presence.by_record_cwa(set(frozen.included), cid, read.bodies, probe["attack_text"])
                assert by_record == presence.by_text(arm, frozen.payload, probe["attack_text"])
                assert by_record == (cell["producer"] != "text")


def test_the_baselines_report_forged_structure_by_carrier(contract, instances):
    by_id = {t["task_id"]: t for family in instances.values() for t in family}
    forged, markers = Counter(), Counter()
    for cell in _cells(instances):
        task = by_id[cell["task_id"]]
        for arm in baselines.ARMS:
            probe = grid.probe(cell, task, arm, 1, "qwen")
            twin = freeze(contract, task, probe["items"], "cwa", SETTINGS) if arm == "xml-unescaped" else None
            places = Counter(t for t, _, _ in structure.expected(twin.snapshot, twin.items)[1]) if twin else None
            built = baselines.build(arm, task, probe["items"], SETTINGS.tokenizer, twin.payload if twin else None)
            read = structure.baseline(arm, built.payload, places)
            forged[(arm, probe["carrier"], read.forged)] += 1
            markers[(arm, probe["carrier"], read.raw_markers)] += 1
            if probe["attack_text"] is not None and cell["producer"] != "text":
                assert presence.by_text(arm, built.payload, probe["attack_text"]), (cell["cell_id"], arm)
    for arm in ("delimited", "xml-unescaped"):
        assert forged[(arm, "tag", False)] == 0 and forged[(arm, "tag", True)] > 0
        assert all(not (a == arm and c != "tag" and f) for a, c, f in forged)
    for arm in baselines.ARMS:
        assert markers[(arm, "marker", False)] == 0 and markers[(arm, "marker", True)] > 0
        assert all(not (a == arm and c != "marker" and m) for a, c, m in markers)


def test_the_baselines_say_the_same_words(contract, instances):
    task = instances["dq"][0]
    for arm in baselines.ARMS:
        twin = freeze(contract, task, task["items"], "cwa", SETTINGS).payload if arm == "xml-unescaped" else None
        handed = "\n".join(handoff.texts(baselines.build(arm, task, task["items"], SETTINGS.tokenizer, twin).payload))
        assert task["instructions"] in handed and task["output_contract"] in handed and task["question"] in handed
        text = task["items"][0]["text"]
        assert (baselines.datamark(text) if arm == "spotlight" else text) in handed
    spot = baselines.build("spotlight", task, task["items"], SETTINGS.tokenizer)
    assert DATAMARK in handoff.texts(spot.payload)[-1]
    tool = handoff.messages(baselines.build("tool-role", task, task["items"], SETTINGS.tokenizer).payload)
    assert [m["role"] for m in tool] == ["system", "user", "assistant", "tool"]
    assert tool[2]["tool_calls"][0]["id"] == tool[3]["tool_call_id"]


def test_the_reminder_is_the_same_sentence_in_both_arms(contract, instances):
    from cwabench5.arms import REMINDER

    task = instances["em"][0]
    boundary = freeze(contract, task, task["items"], "cwa-boundary", SETTINGS)
    plain = freeze(contract, task, task["items"], "cwa", SETTINGS)
    assert REMINDER in boundary.items[0]["body"] and REMINDER not in plain.items[0]["body"]
    assert REMINDER in handoff.texts(baselines.build("reminder", task, task["items"], SETTINGS.tokenizer).payload)[1]


def test_the_grid_balances_its_carriers_and_composes_tag_per_arm(instances):
    cells = _cells(instances)
    attacked = [c for c in cells if c["kind"] == "attacked"]
    assert {c["carrier"] for c in attacked} == {"plain", "heading", "tag", "marker", "json"}
    assert {c["carrier"] for c in cells if c["kind"] == "html"} == {"comment", "meta", "hidden", "alt"}
    assert sum(c["kind"] == "clean" for c in cells) == sum(len(f) for f in instances.values())
    assert cells == _cells(instances)
    tag = next(c for c in attacked if c["carrier"] == "tag")
    task = next(t for f in instances.values() for t in f if t["task_id"] == tag["task_id"])
    texts = {arm: grid.probe(tag, task, arm, 1, "qwen")["attack_text"] for arm in ARMS}
    assert texts["cwa"] == texts["cwa-boundary"] != texts["cwa-observation"] != texts["delimited"]
