"""The label oracle accepts an answer that records exactly its label's decisions, and rejects each way of not."""
from __future__ import annotations

import copy

import pytest

from cwabench.adapters import Outcome
from cwabench.corpora.labeled import Label, compressed, excluded, kept
from cwabench.oracles.label import judge

LABEL = Label(
    fates={
        "gov:base": kept(),
        "q:base": kept(),
        "kb:a": kept(),
        "kb:b": excluded("duplicate_content", duplicate_of="kb:a", slot="evidence.knowledge"),
        "kb:c": compressed("kb:c~short"),
        "x:dup": [excluded("duplicate_item_id", slot="evidence.knowledge")] * 2,
    },
    conflicts={"g": {"decided_by": "policy", "resolution": "resolved", "winner": "kb:a"}},
).as_json()


def _trace():
    return {
        "included": [{"item_id": i, "slot": "s"} for i in ("gov:base", "kb:a", "kb:c", "q:base")],
        "compressed": [{"item_id": "kb:c", "variant_id": "kb:c~short"}],
        "excluded": [
            {"item_id": "kb:b", "reason": "duplicate_content", "stage": "assembler", "slot": "evidence.knowledge",
             "duplicate_of": "kb:a"},
            {"item_id": "x:dup", "reason": "duplicate_item_id", "stage": "assembler", "slot": "evidence.knowledge"},
            {"item_id": "x:dup", "reason": "duplicate_item_id", "stage": "assembler", "slot": "evidence.knowledge"},
            {"item_id": "rogue:0", "reason": "below_threshold", "stage": "producer"},  # producer rows are ignored
        ],
        "conflicts": [{"group_id": "g", "decided_by": "policy", "resolution": "resolved", "winner": "kb:a"}],
        "refused": {"bool": False, "reason": None},
    }


def _failed(trace, kind="assembled", payload=b"x"):
    return [c["id"] for c in judge(LABEL, Outcome(kind, payload=payload, trace=trace)) if c["status"] == "fail"]


def test_exact_answer_passes():
    assert _failed(_trace()) == []


def _mutations():
    def drop_row(t): t["excluded"].pop(0)
    def wrong_reason(t): t["excluded"][0]["reason"] = "superseded"
    def wrong_reference(t): t["excluded"][0]["duplicate_of"] = "kb:c"
    def wrong_slot(t): t["excluded"][0]["slot"] = "evidence.tool_results"
    def one_twin_row(t): t["excluded"].pop(1)
    def extra_exclusion(t): t["excluded"].append({"item_id": "kb:a", "reason": "over_budget", "stage": "assembler"})
    def unlabeled_exclusion(t): t["excluded"].append({"item_id": "ghost", "reason": "revoked", "stage": "assembler"})
    def kept_missing(t): t["included"] = [r for r in t["included"] if r["item_id"] != "kb:a"]
    def excluded_and_included(t): t["included"].append({"item_id": "kb:b", "slot": "s"})
    def not_compressed(t): t["compressed"] = []
    def wrong_variant(t): t["compressed"][0]["variant_id"] = "kb:c~other"
    def kept_compressed(t): t["compressed"].append({"item_id": "kb:a", "variant_id": "kb:a~v"})
    def conflict_winner(t): t["conflicts"][0]["winner"] = "kb:b"
    def conflict_missing(t): t["conflicts"] = []
    return {name: fn for name, fn in locals().items()}


@pytest.mark.parametrize("name, mutate", _mutations().items())
def test_each_mutation_is_rejected(name, mutate):
    trace = _trace()
    mutate(trace)
    assert _failed(trace), name


def test_outcome_refusal_and_recovery():
    refused = copy.deepcopy(_trace())
    refused["refused"] = {"bool": True, "reason": "evidence_required"}
    assert "label:outcome" in _failed(refused, "refused", None)

    label = dict(LABEL, outcome="refused", refusal="evidence_required", recovery="request_context")
    trace = dict(_trace(), included=[], compressed=[], refused={"bool": True, "reason": "evidence_required"},
                 recovery={"action": "request_context"})
    ok = judge(label, Outcome("refused", trace=trace))
    assert [c["id"] for c in ok if c["status"] == "fail"] == []
    trace["recovery"] = {"action": "retrieve_narrower"}
    assert any(c["id"] == "label:outcome" and c["status"] == "fail" for c in judge(label, Outcome("refused", trace=trace)))
    trace["recovery"] = {"action": "request_context"}
    trace["refused"]["reason"] = "required_slot_missing"
    bad = judge(label, Outcome("refused", trace=trace))
    assert any(c["id"] == "label:outcome" and c["status"] == "fail" for c in bad)


def test_no_trace_fails_every_check():
    checks = judge(LABEL, Outcome("crashed", problem="boom"))
    assert all(c["status"] == "fail" for c in checks)
