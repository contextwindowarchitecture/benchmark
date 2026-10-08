"""S1 through the fake adapter: for each fault, exactly the cases it breaks fail, for the right reason, and the run
directory is still valid. A judge that passes everything would fail these tests."""
from __future__ import annotations

import json

import pytest

from cwabench.runner import run
from cwabench.validate import validate_run

from conftest import read_rows


def _expected_failures(contract, mode: str) -> set[str]:
    """The ids S1 must fail for each fault, computed from the corpus independently of the harness."""
    cases, rejections = contract.cases, contract.rejections
    if mode in ("oracle", "volatile"):
        return set()
    if mode == "flip-payload":
        return {c.id for c in cases if c.expected_payload}
    if mode == "drop-excluded":
        return {c.id for c in cases if c.expected_trace.get("excluded")}
    if mode == "swap-included":
        return {c.id for c in cases
                if len(c.expected_trace.get("included", [])) >= 2
                and c.expected_trace["included"][0] != c.expected_trace["included"][1]}
    if mode == "int-bool":
        return {c.id for c in cases}
    if mode == "accept-rejections":
        return {c.id for c in rejections}
    if mode in ("crash", "garbage", "unsupported-required", "hang"):
        return {c.id for c in cases + rejections}
    if mode == "unsupported-optional":
        return {c.id for c in cases + rejections if c.components()["renderer"] != "cwa-message-blocks/v1"}
    raise AssertionError(mode)


@pytest.mark.parametrize("mode", [
    "oracle", "volatile", "flip-payload", "drop-excluded", "swap-included", "int-bool", "accept-rejections",
    "crash", "garbage", "unsupported-optional", "unsupported-required",
])
def test_s1_judges_each_fault(fake_config, contract, mode):
    run_dir, status = run(fake_config(mode), build=False, log=lambda _: None)
    rows = read_rows(run_dir)
    assert len(rows) == len(contract.cases) + len(contract.rejections)

    failed = {r["case_id"] for r in rows if r["verdict"] == "failed"}
    assert failed == _expected_failures(contract, mode)
    assert status == ("pass" if not failed else "fail")
    assert validate_run(run_dir) == []

    findings = [json.loads(line) for line in (run_dir / "findings.jsonl").read_text(encoding="utf-8").splitlines()]
    assert {f["case_id"] for f in findings if f["oracle"] == "expected"} == failed
    assert all(r["finding"] for r in rows if r["verdict"] == "failed")
    audited = {f["case_id"] for f in findings if f["oracle"] == "auditor"}
    if mode in ("oracle", "volatile"):
        assert findings == []
    elif mode in ("flip-payload", "swap-included"):
        assert audited == failed  # the auditor catches these with no expected output at all
    elif mode == "drop-excluded":
        assert audited <= failed and len(audited) >= 0.9 * len(failed), (len(audited), len(failed))

    report = json.loads((run_dir / "suites/S1/reports/fake.conformance-report.json").read_text(encoding="utf-8"))
    assert {e["id"] for e in report["cases"] + report["rejections"] if e["outcome"] == "failed"} == failed


def test_s1_failure_details(fake_config):
    run_dir, _ = run(fake_config("drop-excluded"), build=False, log=lambda _: None)
    row = next(r for r in read_rows(run_dir) if r["verdict"] == "failed")
    assert [c["id"] for c in row["checks"] if c["status"] == "fail" and c["oracle"] == "expected"] == ["trace"]
    assert row["differences"][0]["pointer"].startswith("/excluded/")

    run_dir, _ = run(fake_config("flip-payload"), build=False, log=lambda _: None)
    row = next(r for r in read_rows(run_dir) if r["verdict"] == "failed")
    assert [c["id"] for c in row["checks"] if c["status"] == "fail" and c["oracle"] == "expected"] == ["payload"]
    assert "A4" in row["audit"]["failed"]  # the auditor sees the hash no longer matches
    assert "differs from byte" in row["detail"]

    run_dir, _ = run(fake_config("int-bool"), build=False, log=lambda _: None)
    row = next(r for r in read_rows(run_dir) if r["case_kind"] == "case")
    assert row["outcome"] == "invalid_output"


def test_s1_skips_only_optional_components_the_case_uses(fake_config, contract):
    run_dir, _ = run(fake_config("unsupported-optional"), build=False, log=lambda _: None)
    skipped = {r["case_id"] for r in read_rows(run_dir) if r["verdict"] == "skipped"}
    uses_blocks = {c.id for c in contract.cases + contract.rejections
                   if c.components()["renderer"] == "cwa-message-blocks/v1"}
    assert skipped == uses_blocks and skipped


def test_s1_times_out(fake_config, contract):
    run_dir, status = run(fake_config("hang", timeout_s=0.5, concurrency=32), build=False, log=lambda _: None)
    rows = read_rows(run_dir)
    assert status == "fail"
    assert {r["outcome"] for r in rows} == {"timeout"}


def test_differential_names_exactly_the_cases_adapters_disagree_on(fake_config, contract):
    run_dir, status = run(fake_config("oracle", second="flip-payload"), build=False, log=lambda _: None)
    summary = json.loads((run_dir / "suites/S1/summary.json").read_text(encoding="utf-8"))
    disagreeing = {d["case_id"] for d in summary["differential"]["disagreements"]}
    assert disagreeing == {c.id for c in contract.cases if c.expected_payload}
    assert all(d["first_stage"] == "payload" for d in summary["differential"]["disagreements"])
    assert summary["differential"]["cases"] == len(contract.cases) + len(contract.rejections)
    assert status == "fail"


def test_differential_agrees_on_identical_adapters(fake_config, contract):
    run_dir, status = run(fake_config("oracle", second="volatile"), build=False, log=lambda _: None)
    summary = json.loads((run_dir / "suites/S1/summary.json").read_text(encoding="utf-8"))
    assert summary["differential"]["agreeing"] == summary["differential"]["cases"]
    assert status == "pass"
