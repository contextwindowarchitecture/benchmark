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
                                                     "s0.structure", "s0.replay", "s0.fact", "s0.baseline",
                                                     "s0.compliance"}
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
    text = config.path.read_text().replace('suites = ["S0"]', 'suites = ["S0", "S4"]')
    config.path.write_text(text)
    with pytest.raises(config_mod.ConfigError, match="not implemented"):
        config_mod.load(config.path)
    config.path.write_text(text.replace('"S0", "S4"', '"S0"').replace("[families.fr]", "[families.xx]"))
    with pytest.raises(config_mod.ConfigError, match="no such family"):
        config_mod.load(config.path)


def test_cwabench_runs_domain_2_through_its_entry_point(small_config, capsys):
    from importlib.metadata import entry_points

    from cwabench import cli

    assert {ep.name: ep.value for ep in entry_points(group="cwabench.domains")}["2"] == "cwabench2.cli:main"
    config = small_config()
    assert cli.main(["--domain", "2", "--config", str(config.path), "run"]) == 0
    latest = config.results_dir / "latest"
    assert cli.main(["--domain", "2", "--config", str(config.path), "validate"]) == 0
    assert cli.main(["validate", str(latest)]) == 0  # Domain 1's own validate reads any installed domain's run
    assert "valid" in capsys.readouterr().out


def test_concurrency_and_base_url_flags_override_the_model_settings(small_config):
    import argparse

    from cwabench2 import cli

    config = small_config()
    args = argparse.Namespace(config=str(config.path), suites=None, size=None, no_frames=False, model=None,
                              concurrency=16, base_url="https://pod-8000.proxy.example/v1/")
    loaded = cli._load(args)
    assert loaded.model["concurrency"] == 16 and loaded.model["base_url"] == "https://pod-8000.proxy.example/v1"
    assert cli._load(argparse.Namespace(config=str(config.path), concurrency=None)).model["concurrency"] == 2
    with pytest.raises(SystemExit):
        cli.main(["--config", str(config.path), "run", "--concurrency", "0"])


def test_config_requires_adapters_for_s1_and_s1_for_s7(small_config):
    config = small_config()
    text = config.path.read_text()
    config.path.write_text(text.replace('suites = ["S0"]', 'suites = ["S0", "S1"]'))
    with pytest.raises(config_mod.ConfigError, match="need \\[adapters\\]"):
        config_mod.load(config.path)
    config.path.write_text(text.replace('suites = ["S0"]', 'suites = ["S0", "S7"]'))
    with pytest.raises(config_mod.ConfigError, match="needs S1"):
        config_mod.load(config.path)
    config.path.write_text(text + '\n[arms]\ncwa = ["cwa-history", "cwa-everything"]\n')
    with pytest.raises(config_mod.ConfigError, match="cwa-everything"):
        config_mod.load(config.path)
    assert config_mod.Budgets([8192], [1.0, 0.25]).of(1001) == [("8192", 8192), ("r1.00", 1001), ("r0.25", 251)]


def test_s0_catches_a_baseline_that_counts_one_token_short(small_config, monkeypatch):
    from cwabench.canon.render import charged

    monkeypatch.setattr("cwabench2.baselines.charged", lambda tokens, margin=0: charged(tokens, margin) - 1)
    run_dir, status = run(small_config(), log=lambda m: None)
    assert status == "fail"
    findings = [json.loads(line) for line in (run_dir / "findings.jsonl").read_text().splitlines()]
    assert findings and {f["checks"][0] for f in findings} == {"baseline"}


def test_baseline_settings_take_each_table_value_by_name(small_config):
    config = small_config(extra='[baselines]\nwindow_turns = 4\nsummarizer = "llm"\nextractive_ratio = 0.3\n'
                                'summary_words = 90\n[budgets]\nmargin_percent = 12\n')
    assert (config.baseline.window_turns, config.baseline.summarizer, config.baseline.extractive_ratio,
            config.baseline.summary_words, config.baseline.margin_percent, config.baseline.tokenizer) == (
        4, "llm", 0.3, 90, 12, "estimate-utf8/v1")
