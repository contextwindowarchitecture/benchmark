from __future__ import annotations

import json
from pathlib import Path

import pytest

from cwabench.validate import validate_run
from cwabench2 import ci
from cwabench2 import config as config_mod
from cwabench2.runner import adapters_config, run

DOMAIN1 = Path(__file__).resolve().parent.parent.parent / "domain1" / "domain1.toml"
PROFILES = """
[ci.profiles.nightly]
description = "the self-check"
suites = ["S0"]
size = "pilot"

[ci.profiles.big]
suites = ["S0"]
size = "recorded"
frames = true
"""


def test_a_profile_sets_suites_size_and_frames_and_always_replays(small_config):
    config = small_config(extra=PROFILES + '[model]\nmode = "llm"\n')
    nightly = ci.apply(config, ci.profile(config, "nightly"))
    assert (nightly.suites, nightly.size, nightly.frames, nightly.model["mode"]) == (["S0"], "pilot", False, "replay")
    big = ci.apply(config, ci.profile(config, "big"))
    assert (big.size, big.frames) == ("recorded", True)
    with pytest.raises(config_mod.ConfigError, match="no \\[ci.profiles.weekly\\].*nightly, big"):
        ci.profile(config, "weekly")
    for bad, message in (('suites = ["S9"]', "unknown suites"), ('size = "huge"', "size"),
                         ("frames = 1", "frames")):
        broken = small_config(extra=f"[ci.profiles.x]\n{bad}\n")
        with pytest.raises(config_mod.ConfigError, match=message):
            ci.profile(broken, "x")


def test_checkouts_move_the_spec_and_domain1s_adapters(small_config, tmp_path):
    config = small_config(extra=f'[adapters]\nconfig = {json.dumps(str(DOMAIN1))}\nuse = ["python", "rust"]\n')
    moved = ci.with_checkouts(config, tmp_path / "co")
    assert moved.contract_path == tmp_path / "co" / config.contract_path.name
    d1 = adapters_config(moved)
    assert list(d1.adapters) == ["python", "rust"]
    assert all(a.checkout.parent == tmp_path / "co" for a in d1.adapters.values())
    assert d1.build_dir == DOMAIN1.parent / ".build" / "ci" / "build"  # never an ordinary run's builds
    assert adapters_config(config).build_dir != d1.build_dir


def test_s7_goldens_in_the_reports_shape(tmp_path):
    assert ci.s7_goldens(tmp_path) is None
    (tmp_path / "suites" / "S7").mkdir(parents=True)
    (tmp_path / "suites" / "S7" / "summary.json").write_text(json.dumps({"goldens": {
        "path": "goldens/d2-goldens.json", "contract_commit": "c",
        "drift": {"match": 10, "spec_change": 0, "regression": 2, "new": 0, "removed": 3}}}))
    assert ci.s7_goldens(tmp_path) == {"adopted": True, "drift": {"match": 10, "regression": 2}, "removed": 3}


def test_ci_runs_reports_drift_and_publishes(small_config):
    config = small_config(extra=PROFILES)
    first, status, report = ci.run(config, "nightly", build=False, log=lambda _: None)
    assert status == "pass" and validate_run(first) == []
    assert report["verdict"] == "baseline" and report["source"] == "checkouts"
    assert report["$schema"] == "cwa-bench-d2/ci-report/v1"
    published = config.results_dir / "nightly"
    assert published.is_symlink() and published.resolve() == first.resolve()
    manifest = json.loads((first / "manifest.json").read_text())
    assert manifest["ci"] == {"profile": "nightly", "source": "checkouts", "mirrors": None}

    second, status, report = ci.run(config, "nightly", build=False, log=lambda _: None)
    assert status == "pass" and validate_run(second) == []
    assert report["previous"]["run_id"] == first.name and report["verdict"] == "unchanged"
    assert published.resolve() == second.resolve()


def test_a_fixture_is_the_run_cut_down_under_domain_2s_prefix(small_config, tmp_path):
    from cwabench2.cli import main

    config = small_config()
    run_dir, status = run(config, build=False, log=lambda _: None)
    assert status == "pass"
    out = tmp_path / "fixtures"
    assert main(["--config", str(config.path), "fixture", run_dir.name, "--out", str(out), "--rows", "2"]) == 0
    fixture = out / run_dir.name
    assert validate_run(fixture) == []
    index = json.loads((fixture / "index.json").read_text())
    assert index["$schema"] == "cwa-bench-d2/run-index/v1" and index["fixture"]["of"] == run_dir.name
    assert json.loads((out / "index.json").read_text())["$schema"] == "cwa-bench-d2/runs-index/v1"
    assert not (out / "latest").exists()  # a fixtures directory has no latest link
    rows = (fixture / "suites" / "S0" / "results.jsonl").read_text().splitlines()
    assert len(rows) == 2 < len((run_dir / "suites" / "S0" / "results.jsonl").read_text().splitlines())
