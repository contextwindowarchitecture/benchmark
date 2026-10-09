from __future__ import annotations

import pytest

from cwabench import gitinfo
from cwabench.canon import render, validity
from cwabench.contract import Contract
from cwabench2.application import profile
from cwabench2.application.snapshots import ARMS, Point, Settings, freeze, points
from cwabench2.conversations import generate

VT = {"variables": 2, "distractors": 2, "assignment_density": 0.5, "filler_sentences": 2, "reply_sentences": 2}
FR = {"tasks": ["record", "compute"], "fields": 4, "steps": 4, "filler_sentences": 2, "reply_sentences": 2}


@pytest.fixture(scope="module")
def contract(spec) -> Contract:
    return Contract(spec, gitinfo.inspect(spec).commit, allow_dirty=True)


def last_probe(script):
    probe = script["probes"][-1]
    return Point("probe", probe["after_turn"], probe)


def test_the_arms_profile_follows_the_spec_and_validates(contract):
    assert profile.check(contract) == []
    assert profile.profile(contract, profile.route_policy(False))["placement"] == profile.spec_placement(contract)


@pytest.mark.parametrize("arm", list(ARMS))
def test_every_arm_freezes_valid_snapshots_and_predicts_monotone_shedding(contract, arm):
    script = generate("vt", 3, 40, 0, 10, VT)
    frozen = freeze(contract, script, ARMS[arm], last_probe(script), Settings())
    assert validity.problems(contract, frozen.at(frozen.full)) == []
    whole = frozen.expect(frozen.full)
    assert whole.outcome == "assembled" and len(whole.included) == len(frozen.protected + frozen.history +
                                                                       frozen.memory)
    assert frozen.expect(frozen.floor - 1).refusal_reason == "protected_content_over_budget"
    assert frozen.expect(frozen.floor).outcome == "assembled"
    kept = [len(frozen.expect(b).included) for b in range(frozen.floor, frozen.full + 1, 97)]
    assert kept == sorted(kept)  # a larger budget never keeps less
    middle = frozen.expect((frozen.floor + frozen.full) // 2)
    newest = [i["id"] for i in frozen.history if i["id"] in middle.included]
    assert newest == [i["id"] for i in frozen.history[:len(newest)]]  # history is kept newest first
    assert middle.input_tokens == render.render(frozen.snapshot, [
        i for i in frozen.protected + frozen.history + frozen.memory if i["id"] in middle.included]).count(
        "estimate-utf8/v1")


def test_the_ladder_adds_state_then_memory_with_revocation(contract):
    script = generate("vt", 3, 60, 0, 10, VT)
    point = last_probe(script)
    history, state, memory = (freeze(contract, script, ARMS[a], point, Settings())
                              for a in ("cwa-history", "cwa-state", "cwa-memory"))
    assert not any(i["slot"] == "state.task" for i in history.protected)
    assert sorted(i["id"] for i in state.protected if i["slot"] == "state.task") == ["state:d1", "state:d2",
                                                                                      "state:v1", "state:v2"]
    assert len(memory.history) == 2 * 10  # the history window, user and assistant messages
    batch = next(b for b in memory.snapshot["batches"] if b["producer"]["id"] == "memory-svc")
    reasons = {row["reason"] for row in batch["excluded"]}
    assert reasons <= {"revoked"} and len(batch["items"]) + len(batch["excluded"]) == sum(
        1 for t in script["turns"][:point.turn - 10] if t["shards"])
    need = point.probe["needs"][0]
    assert {c.split(":")[0] for c in state.carriers[need]} <= {"turn", "state"}


def test_memory_expires_and_record_facts_are_never_revoked(contract):
    script = generate("fr", 4, 60, 0, 10, FR)
    assert script["ground_truth"]["task"] == "record"
    point = last_probe(script)
    kept = freeze(contract, script, ARMS["cwa-memory"], point, Settings())
    assert kept.memory and all(i["source"].startswith("turn:") and "expires" in i for i in kept.memory)
    short = freeze(contract, script, ARMS["cwa-memory"], point, Settings(memory_ttl_seconds=600))
    batch = next(b for b in short.snapshot["batches"] if b["producer"]["id"] == "memory-svc")
    assert batch["excluded"] and {row["reason"] for row in batch["excluded"]} == {"expired"}
    assert validity.problems(contract, short.at(short.full)) == []


def test_points_put_each_probe_after_its_turn():
    script = generate("vt", 3, 20, 0, 10, VT)
    ids = [p.id for p in points(script, frames=True)]
    assert ids.index("p010") == ids.index("t010") + 1 and ids[-1] == "p020" and len(ids) == 22
    assert [p.id for p in points(script, frames=False)] == ["p010", "p020"]
