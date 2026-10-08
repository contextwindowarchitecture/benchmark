from __future__ import annotations

import pytest

from cwabench import output
from cwabench.contract import Contract, ContractError


def test_corpus_loads(contract):
    assert len(contract.cases) >= 61 and len(contract.rejections) >= 25
    for case in contract.cases:
        assert case.expected_trace is not None, case.id
        assert case.expected_outcome in ("assembled", "refused")
    assert all(c.expected_outcome == "rejected" for c in contract.rejections)


def test_expected_traces_validate_with_formats(contract):
    validator = contract.validator("trace.schema.json")
    for case in contract.cases:
        assert not list(validator.iter_errors(case.expected_trace)), case.id


def test_every_recorded_reason_is_a_contract_code(contract):
    for case in contract.cases:
        for row in case.expected_trace.get("excluded", []):
            assert contract.reason_template(row["reason"]), (case.id, row["reason"])


def test_wrong_commit_is_refused(spec):
    with pytest.raises(ContractError, match="pins"):
        Contract(spec, "0" * 40, allow_dirty=True)


def test_contract_document_validates(contract):
    output.validate(contract.as_document("20261006T000000Z-abcdef0"))
