from __future__ import annotations

import json

import pytest

from cwabench import gitinfo
from cwabench.contract import Contract
from cwabench5 import attacks, pasted, tasks
from cwabench5.application.snapshot import Settings
from cwabench5.arms import ALL, PASTE_CLOSE, PASTE_OPEN, PASTED
from cwabench5.oracles import presence, structure

SETTINGS = Settings(margin_percent=35)


@pytest.fixture(scope="module")
def contract(spec):
    return Contract(spec, gitinfo.inspect(spec).commit, True)


def _probe(arm: str, carrier: str = "tag"):
    task = tasks.generate("em", 501, "plain", 0, {})
    template = next(t for t in attacks.every_template() if t.family == "hj" and t.split == "test")
    return task, attacks.compose(task, template, "middle", carrier, 7, delimiters=ALL[arm].delimiters)


def test_every_arm_sends_the_same_material_and_question():
    task, probe = _probe("pasted-naive", "plain")
    code = pasted.code(7, "cell")
    for arm in pasted.ARMS:
        body = pasted.body(arm, task, probe["items"], code)
        assert body.endswith(task["question"]) and probe["attack_text"] in body
    assert code not in pasted.body("pasted-naive", task, probe["items"], code)
    assert pasted.body("pasted-nonce", task, probe["items"], code) == pasted.body("cwa-pasted-nonce", task,
                                                                                    probe["items"], code)
    assert f"[[PASTED {code}]]" in pasted.body("pasted-nonce", task, probe["items"], code)
    assert PASTE_OPEN in pasted.body("pasted-delimited", task, probe["items"], code)


def test_a_tag_attack_forges_the_fixed_boundary_but_not_the_nonce():
    code = pasted.code(7, "cell")
    for arm, want in (("pasted-delimited", True), ("pasted-nonce", False), ("pasted-naive", None)):
        task, probe = _probe(arm)
        built = pasted.build(arm, task, probe["items"], code, "estimate-utf8/v1")
        assert pasted.forged(arm, built.payload, code) is want, arm
        assert presence.by_text(arm, built.payload, probe["attack_text"])
    task, probe = _probe("pasted-delimited")
    assert PASTE_CLOSE in probe["attack_text"]


def test_a_cwa_pasted_payload_holds_the_material_in_the_query_and_reads_back(contract):
    code = pasted.code(7, "cell")
    for arm in ("cwa-pasted", "cwa-pasted-nonce"):
        task, probe = _probe(arm)
        frozen = pasted.freeze(contract, task, probe["items"], arm, SETTINGS, code)
        read = structure.cwa(frozen.payload, frozen.snapshot, frozen.items)
        assert read.ok, read.problems
        assert {i["slot"] for i in frozen.items} == {"governance.instructions", "governance.output_contract",
                                                     "interaction.query"}
        content = json.loads(frozen.payload)["messages"][0]["content"]
        assert "<query" in content and "<evidence" not in content and "&lt;/query&gt;" in content
        assert presence.by_record_cwa(set(frozen.included), frozen.content[probe["item_id"]], read.bodies,
                                      probe["attack_text"])
        assert presence.by_text(arm, frozen.payload, probe["attack_text"])


def test_the_pasted_arms_are_their_own():
    assert set(pasted.ARMS) == set(PASTED) and not set(PASTED) & {"cwa", "naive"}
