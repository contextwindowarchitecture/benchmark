from __future__ import annotations

import json

import pytest

from cwabench.validate import validate_run
from cwabench2 import config as config_mod
from cwabench2.runner import run
from cwabench2.suites import s0_cases


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_run_with_only_s0_passes_and_validates(small_config):
    config = small_config()
    run_dir, status = run(config, log=lambda m: None)
    assert status == "pass"
    assert validate_run(run_dir) == []
    index = _read(run_dir / "index.json")
    assert index["$schema"] == "cwa-bench-d2/run-index/v1" and index["suites"] == ["S0"]
    paths = {f["path"] for f in index["files"]}
    assert {"manifest.json", "contract.json", "summary.json", "findings.jsonl", "suites/S0/summary.json",
            "suites/S0/results.jsonl", "conversations/vt/index.json", "conversations/fr/index.json"} <= paths
    manifest = _read(run_dir / "manifest.json")
    assert manifest["harness"]["name"] == "cwa-bench-d2" and manifest["status"] == "pass"
    vt = _read(run_dir / "conversations" / "vt" / "index.json")
    assert [c["conversation_id"] for c in vt["conversations"]] == ["vt-t010-00", "vt-t025-00"]
    blob = run_dir / "blobs" / "sha256" / vt["conversations"][0]["script"][7:9]
    assert len(list(blob.glob("*.json"))) == 1
    summary = _read(run_dir / "summary.json")
    assert {m["id"] for m in summary["metrics"]} == {"s0.grader", "s0.plant", "s0.schema", "s0.determinism",
                                                     "s0.structure", "s0.replay"}
    assert all(m["status"] == "pass" for m in summary["metrics"])
    runs = _read(config.results_dir / "index.json")
    assert runs["$schema"] == "cwa-bench-d2/runs-index/v1" and runs["latest"] == run_dir.name


def test_a_grader_that_disagrees_fails_s0_with_a_finding(small_config, monkeypatch):
    planted = [("number-misplanted", s0_cases.NUMBER, "4200", "wrong", 0.0)]
    monkeypatch.setattr("cwabench2.suites.s0_selfcheck.CASES", planted)
    run_dir, status = run(small_config(), log=lambda m: None)
    assert status == "fail"
    assert validate_run(run_dir) == []
    findings = [json.loads(line) for line in (run_dir / "findings.jsonl").read_text().splitlines()]
    assert [(f["case_id"], f["oracle"], f["upstream"]) for f in findings] == [("number-misplanted", "self-check",
                                                                                None)]


def test_config_refuses_what_this_build_does_not_have(small_config):
    config = small_config()
    text = config.path.read_text().replace('suites = ["S0"]', 'suites = ["S0", "S2"]')
    config.path.write_text(text)
    with pytest.raises(config_mod.ConfigError, match="not implemented"):
        config_mod.load(config.path)
    config.path.write_text(text.replace('"S0", "S2"', '"S0"').replace("[families.fr]", "[families.xx]"))
    with pytest.raises(config_mod.ConfigError, match="no such family"):
        config_mod.load(config.path)
