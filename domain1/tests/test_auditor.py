"""The auditor: no false positives on the spec's expected outputs, and it catches what it claims to."""
from __future__ import annotations

import copy
import json
from collections import Counter

import pytest

from cwabench.oracles.auditor import CHECKS, audit
from cwabench.oracles.mutants import Mutator


def _case(contract, case_id):
    return next(c for c in contract.cases if c.id == case_id)


def test_no_false_positives(contract):
    for case in contract.cases:
        result = audit(contract, case.snapshot_bytes, case.expected_payload, case.expected_trace)
        assert result.status == "pass", (case.id, {i: result.checks[i].violations for i in result.failed})


def test_kill_rate_and_every_check_earns_its_place(contract):
    total = killed = 0
    kills = Counter()
    for case in contract.cases:
        snapshot = json.loads(case.snapshot_bytes)
        for mutant in Mutator(contract, snapshot).mutants(case.expected_trace, case.expected_payload):
            result = audit(contract, case.snapshot_bytes, mutant.payload, mutant.trace)
            total += 1
            killed += result.status == "fail"
            kills.update(result.failed)
    assert killed / total >= 0.95, (killed, total)
    assert all(kills[check] > 0 for check in CHECKS), {c: kills[c] for c in CHECKS}


def test_conflict_decisions_are_computed_not_trusted(contract):
    case = _case(contract, "conflict-fact")
    trace = copy.deepcopy(case.expected_trace)
    record = trace["conflicts"][0]
    loser = next(i for i in record["items"] if i != record["winner"])
    record["winner"], swapped = loser, record["winner"]
    for row in trace["excluded"]:
        if row["item_id"] == loser:
            row["item_id"] = swapped
    result = audit(contract, case.snapshot_bytes, case.expected_payload, trace)
    assert "A12" in result.failed


def test_refusal_reason_follows_contract_order(contract):
    case = _case(contract, "required-slot-missing")
    trace = copy.deepcopy(case.expected_trace)
    trace["refused"]["reason"] = "evidence_required"
    trace["recovery"] = {"action": "request_context"}
    assert "A11" in audit(contract, case.snapshot_bytes, None, trace).failed


def test_a_dropped_admission_row_is_caught_on_refusal(contract):
    """On a refused trace, admitted items have no row, so dropping an admission row must not look like admission."""
    from cwabench.oracles.auditor.checks import ADMISSION_CONDITIONS

    tried = 0
    for case in contract.cases:
        if case.expected_outcome != "refused":
            continue
        for i, row in enumerate(case.expected_trace["excluded"]):
            definite = row["reason"] in ADMISSION_CONDITIONS or row["reason"].startswith("missing_field:")
            if row["stage"] == "assembler" and definite:
                trace = copy.deepcopy(case.expected_trace)
                del trace["excluded"][i]
                tried += 1
                assert "A10" in audit(contract, case.snapshot_bytes, None, trace).failed, (case.id, row)
    assert tried > 0


def test_an_auditor_crash_is_a_failure_not_a_pass(contract, monkeypatch):
    case = contract.cases[0]
    from cwabench.oracles.auditor import checks

    monkeypatch.setattr(checks.Audit, "a16", lambda self: 1 / 0)
    result = audit(contract, case.snapshot_bytes, case.expected_payload, case.expected_trace)
    assert result.checks["A16"].status == "fail" and "auditor error" in result.checks["A16"].violations[0]
