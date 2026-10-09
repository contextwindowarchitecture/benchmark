from __future__ import annotations

import pytest

from cwabench2.conversations import check, generate
from cwabench2.grading.compliance import complies
from cwabench2.suites.s0_cases import COMPLIANCE_CASES

CC = {"variables": 3, "distractors": 2, "corrections": 2, "filler_sentences": 2, "reply_sentences": 2}
IP = {"variables": 2, "distractors": 1, "assignment_density": 0.5, "filler_sentences": 2, "reply_sentences": 2,
      "rules": ["brackets", "signoff", "uppercase"]}


@pytest.mark.parametrize("turns", [10, 37, 100])
def test_corrections_retract_with_the_stale_value_beside_the_new(turns):
    for index in range(3):
        script = generate("cc", 11, turns, index, 10, CC)
        assert check.structure(script) == [] and check.replay(script) == []
        for variable in script["ground_truth"]["variables"]:
            kinds = [a["kind"] for a in variable["assignments"]]
            assert kinds[0] == "stated" and set(kinds[1:]) <= {"corrected"}
    script = generate("cc", 11, 100, 0, 10, CC)
    corrected = [p for p in script["probes"] if p["attributes"]["corrections"]]
    assert corrected and all(p["answer"]["stale"] for p in corrected)
    sentence = next(s for t in script["turns"] for s in t["shards"] if ", not " in s)
    assert len([w for w in sentence.split() if any(c.isdigit() for c in w)]) == 2


def test_the_checker_catches_a_correction_that_retracts_the_wrong_value():
    script = generate("cc", 11, 50, 0, 10, CC)
    turn = next(t for t in script["turns"] if t["shards"] and ", not " in t["shards"][0])
    shard = turn["shards"][0]
    old = shard.rstrip(".").split("not ")[-1]
    broken = shard.replace(f"not {old}", "not 9,999")
    turn["user"], turn["shards"] = turn["user"].replace(shard, broken), [broken]
    assert any("retracts" in p for p in check.replay(script))


def test_ip_cycles_its_rules_and_asks_without_number_only():
    rules = [generate("ip", 3, 20, i, 10, IP)["rule"]["id"] for i in range(3)]
    assert rules == ["brackets", "signoff", "uppercase"]
    script = generate("ip", 3, 20, 1, 10, IP)
    assert "Kestrel desk" in script["instructions"] and check.replay(script) == []
    assert not any("number only" in p["question"].lower() or "just the number" in p["question"].lower()
                   for p in script["probes"])


@pytest.mark.parametrize("case_id, rule, reply, expected", COMPLIANCE_CASES, ids=[c[0] for c in COMPLIANCE_CASES])
def test_compliance(case_id, rule, reply, expected):
    assert complies(rule, reply) is expected
