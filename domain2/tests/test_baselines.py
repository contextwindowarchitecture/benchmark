from __future__ import annotations

import json

import pytest

from cwabench2 import baselines
from cwabench2.application import fact
from cwabench2.application.snapshots import ARMS, Point, Settings, freeze
from cwabench2.conversations import generate
from cwabench2.suites.s0_cases import BASELINE_CASES, BASELINE_PAYLOADS, BASELINE_SCRIPT

VT = {"variables": 2, "distractors": 2, "assignment_density": 0.5, "filler_sentences": 3, "reply_sentences": 2}
POINT = Point("probe", 3, BASELINE_SCRIPT["probes"][0])
HAND = baselines.Settings(window_turns=2, margin_percent=0)


@pytest.mark.parametrize("arm, budget, outcome, tokens, kept, first, system", BASELINE_CASES,
                         ids=[f"{c[0]}@{c[1]}" for c in BASELINE_CASES])
def test_hand_computed_cases(arm, budget, outcome, tokens, kept, first, system):
    built = baselines.at(arm, BASELINE_SCRIPT, POINT, budget, HAND)
    assert (built.outcome, built.input_tokens, built.kept, built.first_kept, built.system_survived) == (
        outcome, tokens, kept, first, system)


def test_hand_computed_payloads():
    for (arm, budget), text in BASELINE_PAYLOADS.items():
        assert baselines.at(arm, BASELINE_SCRIPT, POINT, budget, HAND).payload == text.encode()


def last(script):
    probe = script["probes"][-1]
    return Point("probe", probe["after_turn"], probe)


@pytest.mark.parametrize("arm", baselines.ARMS)
def test_every_baseline_fits_its_budget_and_keeps_a_suffix(arm):
    script = generate("vt", 9, 60, 0, 10, VT)
    point = last(script)
    full = baselines.full(script, point, baselines.Settings())
    for budget in (full, full // 2, full // 5, 400):
        built = baselines.at(arm, script, point, budget, baselines.Settings())
        if built.outcome == "fits":
            assert built.charged <= budget
        else:
            assert arm == "concat" or built.history_kept == 0
        order = [m for m in built.kept if m.startswith("turn:")]
        said = sorted(order, key=lambda m: (m.split(":")[1], m.endswith("assistant")))
        assert order == said  # turns stay in the order they were said, each user message before its reply
        request = json.loads(built.payload)
        assert request["messages"][-1] == {"role": "user", "content": point.probe["question"]}
        assert built.system_survived == any(e["id"] == "system" for e in request["system"])


def test_the_fact_oracle_reads_baselines_both_ways():
    script = generate("vt", 9, 60, 0, 10, VT)
    point = last(script)
    needs = point.probe["needs"]
    for arm in baselines.ARMS:
        for budget in (8192, 2000, 600):
            built = baselines.at(arm, script, point, budget, baselines.Settings())
            by_record = fact.by_trace(set(built.kept), built.carriers, needs)
            assert by_record == fact.by_text_chat(built.payload, built.evidence, needs), (arm, budget)


def test_the_format_control_takes_the_window_selection_exactly(spec):
    from cwabench import gitinfo
    from cwabench.contract import Contract

    contract = Contract(spec, gitinfo.inspect(spec).commit, allow_dirty=True)
    script = generate("vt", 9, 60, 0, 10, VT)
    point = last(script)
    for budget in (8192, 1500, 500):
        built = baselines.at("window", script, point, budget, baselines.Settings())
        frozen = freeze(contract, script, ARMS["cwa-format"], point, Settings(), only=set(built.kept))
        expected = frozen.expect(frozen.full)
        history = {i for i in expected.included if i.startswith("turn:")}
        assert history == {m for m in built.kept if m.startswith("turn:")}


def test_the_summarizer_is_stub_only_until_p3():
    with pytest.raises(ValueError, match="P3"):
        baselines.summarize(BASELINE_SCRIPT["turns"], baselines.Settings(summarizer="llm"))
