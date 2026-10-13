from __future__ import annotations

import json
import tomllib

import pytest

from cwabench.validate import validate_run
from cwabench5 import config as config_mod
from cwabench5.runner import run

ROOT = config_mod.Path(__file__).resolve().parent.parent


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_a_run_with_only_s0_passes_and_validates(small_config):
    config = small_config()
    run_dir, status = run(config, log=lambda m: None)
    assert status == "pass"
    assert validate_run(run_dir) == []
    index = _read(run_dir / "index.json")
    assert index["$schema"] == "cwa-bench-d5/run-index/v1" and index["suites"] == ["S0"]
    paths = {f["path"] for f in index["files"]}
    assert {"manifest.json", "contract.json", "summary.json", "findings.jsonl", "suites/S0/summary.json",
            "suites/S0/results.jsonl", "tasks/em/index.json", "tasks/cq/index.json", "tasks/sl/index.json",
            "attacks/index.json"} <= paths
    assert _read(run_dir / "manifest.json")["harness"]["name"] == "cwa-bench-d5"
    em = _read(run_dir / "tasks" / "em" / "index.json")
    assert [t["task_id"] for t in em["tasks"]] == ["em-p00", "em-i00"]
    summary = _read(run_dir / "summary.json")
    metrics = {m["id"]: m for m in summary["metrics"]}
    assert set(metrics) == {"s0.grader", "s0.attack_grader", "s0.language", "s0.schema", "s0.determinism",
                            "s0.structure", "s0.replay", "s0.probe", "s0.plant", "s0.attack_plant"}
    assert all(m["status"] == "pass" for m in metrics.values())
    rows = [json.loads(line) for line in (run_dir / "suites" / "S0" / "results.jsonl").read_text().splitlines()]
    quoted = [r for r in rows if r["check"] == "attack-plant" and r["case_id"].endswith("/quoted")]
    assert quoted and all(r["expected"] == r["actual"] == "reported" for r in quoted)
    # every template meets the instances it applies to; AC only email, DM only selection
    probes = {r["probe"] for r in rows if r["check"] == "probe"}
    assert any("/ac-" in p for p in probes) and all(p.startswith("em-") for p in probes if "/ac-" in p)
    assert all(p.startswith("sl-") for p in probes if "/dm-" in p)


def test_a_grader_that_disagrees_fails_s0_with_a_finding(small_config, monkeypatch):
    from cwabench5.grading import cases

    planted = [("hj-misplanted", cases.HIJACK, cases.HIJACK_TEXT, "KAVORE-TUMIZA", "ignored")]
    monkeypatch.setattr("cwabench5.suites.s0_selfcheck.cases.ATTACK_CASES", planted)
    run_dir, status = run(small_config(), log=lambda m: None)
    assert status == "fail"
    assert validate_run(run_dir) == []
    findings = [json.loads(line) for line in (run_dir / "findings.jsonl").read_text().splitlines()]
    assert [(f["case_id"], f["oracle"]) for f in findings] == [("hj-misplanted", "self-check")]


def test_config_refuses_what_this_build_does_not_have(small_config):
    config = small_config()
    text = config.path.read_text()
    config.path.write_text(text.replace('suites = ["S0"]', 'suites = ["S0", "S9"]'))
    with pytest.raises(config_mod.ConfigError, match="not implemented"):
        config_mod.load(config.path)
    config.path.write_text(text.replace("[tasks.cq]", "[tasks.xx]"))
    with pytest.raises(config_mod.ConfigError, match="no such family"):
        config_mod.load(config.path)
    config.path.write_text(text.replace("seed = 7", 'seed = 7\nchat_template = "gpt"'))
    with pytest.raises(config_mod.ConfigError, match="chat_template"):
        config_mod.load(config.path)


def test_cwabench_runs_domain_5_through_its_entry_point(small_config, capsys):
    from importlib.metadata import entry_points

    from cwabench import cli

    assert {ep.name: ep.value for ep in entry_points(group="cwabench.domains")}["5"] == "cwabench5.cli:main"
    config = small_config()
    assert cli.main(["--domain", "5", "--config", str(config.path), "run"]) == 0
    latest = config.results_dir / "latest"
    assert cli.main(["--domain", "5", "--config", str(config.path), "validate"]) == 0
    assert cli.main(["validate", str(latest)]) == 0  # Domain 1's own validate reads any installed domain's run
    assert "valid" in capsys.readouterr().out


def test_the_shipped_config_loads_and_pins_domain_1s_contract():
    config = config_mod.load(ROOT / "domain5.toml")
    assert list(config.tasks) == ["em", "dq", "tq", "dg", "cq", "sl"]

    def pin(path):
        return tomllib.loads(path.read_text(encoding="utf-8"))["contract"]["commit"]

    assert pin(ROOT / "domain5.toml") == pin(ROOT.parent / "domain1" / "domain1.toml")
