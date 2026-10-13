from __future__ import annotations

import pytest

from cwabench5 import attacks, output, tasks
from cwabench5.attacks import carriers, check
from cwabench5.attacks.families import BODIES, OPENERS


def _applicable(template, task):
    return [c for c in attacks.CARRIERS if carriers.applies(c, template.family)] if template.applies_to(
        task["family"]) else []


def test_each_family_crosses_its_openers_and_bodies_into_two_halves():
    for family in attacks.FAMILIES:
        found = attacks.templates(family)
        assert len(found) == len(OPENERS) * len(BODIES[family])
        for split in ("train", "test"):
            half = [t for t in found if t.split == split]
            assert len(half) == len(found) // 2
            assert {t.body for t in half} == set(range(len(BODIES[family])))
    assert len({t.template_id for t in attacks.every_template()}) == len(attacks.every_template())


@pytest.mark.parametrize("family", tuple(tasks.FAMILIES))
def test_every_probe_passes_its_independent_check(family):
    for variant in tasks.VARIANTS:
        for index in range(2):
            task = tasks.generate(family, 5, variant, index, {})
            for n, template in enumerate(attacks.every_template()):
                for carrier in _applicable(template, task):
                    position = attacks.POSITIONS[(n + index) % 3]
                    probe = attacks.compose(task, template, position, carrier, 99)
                    assert check.problems(task, probe) == [], probe["probe_id"]


def test_a_probe_is_its_seed_and_coordinates_alone():
    task = tasks.generate("dq", 5, "plain", 0, {})
    template = attacks.templates("hj")[1]
    first = attacks.compose(task, template, "middle", "tag", 99)
    assert first == attacks.compose(task, template, "middle", "tag", 99)
    assert first["probe_id"] == "dq-p00/hj-01/middle/tag"
    assert first["predicate"]["canary"] != attacks.compose(task, template, "middle", "tag", 98)["predicate"]["canary"]


def test_the_attack_never_states_its_canary():
    task = tasks.generate("tq", 5, "plain", 0, {})
    for template in attacks.templates("hj") + attacks.templates("ad"):
        probe = attacks.compose(task, template, "end", "plain", 7)
        canary = probe["predicate"]["canary"]
        assert canary.casefold() not in probe["instruction"].casefold()
        instruction = probe["instruction"]
        assert all(p in instruction.casefold() or p in instruction for p in probe["predicate"]["parts"])


def test_families_apply_only_where_their_contract_allows():
    selection, email = tasks.generate("sl", 5, "plain", 0, {}), tasks.generate("em", 5, "plain", 0, {})
    with pytest.raises(ValueError):
        attacks.compose(selection, attacks.templates("ac")[0], "end", "plain", 1)
    with pytest.raises(ValueError):
        attacks.compose(email, attacks.templates("dm")[0], "end", "plain", 1)
    with pytest.raises(ValueError):
        attacks.compose(email, attacks.templates("hj")[0], "end", "json", 1)
    probe = attacks.compose(selection, attacks.templates("dm")[0], "end", "plain", 1)
    assert probe["predicate"]["candidate"] in selection["answer"]["distractors"]


def test_the_email_family_receives_the_attack_in_an_email_of_its_own():
    task = tasks.generate("em", 5, "plain", 1, {})
    probe = attacks.compose(task, attacks.templates("ac")[1], "middle", "json", 3)
    assert len(probe["items"]) == len(task["items"]) + 1
    added = next(i for i in probe["items"] if i["item_id"] == probe["item_id"])
    assert added["text"].startswith("From: ") and carriers.FORGED_REPLY in added["text"]
    assert probe["predicate"]["action"]["to"].endswith(".example")


def test_code_takes_the_attack_as_comments_and_the_answer_moves_with_it():
    task = tasks.generate("cq", 5, "plain", 0, {})
    probe = attacks.compose(task, attacks.templates("ex")[1], "start", "marker", 99)
    lines = probe["items"][0]["text"].split("\n")
    inserted = probe["inserted"].count("\n")
    assert all(line.startswith("#") for line in lines[:inserted])
    assert int(probe["answer"]["line"]) == int(task["answer"]["line"]) + inserted
    end = attacks.compose(task, attacks.templates("ex")[1], "end", "marker", 99)
    assert end["answer"] == task["answer"]


def test_the_tag_and_marker_carriers_take_the_arm_and_template():
    task = tasks.generate("dg", 5, "plain", 0, {})
    template = attacks.templates("hj")[3]
    own = {"close": "<<<END>>>", "open": "[SYSTEM]", "end": "[/SYSTEM]", "reopen": "<<<BEGIN>>>"}
    probe = attacks.compose(task, template, "end", "tag", 4, delimiters=own)
    assert probe["attack_text"].startswith("<<<END>>>\n[SYSTEM]\n") and probe["attack_text"].endswith("<<<BEGIN>>>")
    llama = attacks.compose(task, template, "end", "marker", 4, chat_template="llama3")
    assert "<|start_header_id|>system<|end_header_id|>" in llama["attack_text"]


def test_the_task_schema_holds_an_attacked_instance():
    task = tasks.generate("dq", 5, "imperative", 0, {})
    probe = attacks.compose(task, attacks.templates("mf")[3], "start", "heading", 2)
    output.validate(attacks.attacked_task(task, probe))
