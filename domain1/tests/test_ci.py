"""P7's pieces: CI profiles, the upstream mirrors (against local git repositories), the drift report's verdicts, the
container variants' image identity, and `cwabench ci` end to end through the fake adapter."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from cwabench import ci, container
from cwabench import config as config_mod
from cwabench.config import ConfigError
from cwabench.validate import validate_run

PROFILES = """
[ci.profiles.nightly]
suites = ["S1"]
s2_matrix = ["amd64"]
summarizer = "replay"
"""


# Profiles -------------------------------------------------------------------------------------------------------------

def test_a_profile_sets_suites_matrix_and_summarizer(fake_config):
    config = fake_config("oracle", extra=PROFILES)
    chosen = ci.profile(config, "nightly")
    applied = ci.apply(config, chosen)
    assert applied.suites == ["S1"] and applied.section("s2")["matrix"] == ["amd64"]
    assert applied.section("summarizer")["mode"] == "replay"
    with pytest.raises(ConfigError, match="no \\[ci.profiles.weekly\\]"):
        ci.profile(config, "weekly")


def test_checkouts_move_every_repository_and_the_build_directory(fake_config, tmp_path):
    config = fake_config("oracle", second="oracle")
    moved = ci.with_checkouts(config, tmp_path / "co")
    assert moved.contract_path == tmp_path / "co" / config.contract_path.name
    assert {a.checkout for a in moved.all_adapters.values()} == {tmp_path / "co" / "tests"}
    assert moved.build_dir == config.root / ".build/ci/build" != config.build_dir
    assert moved.expand("{build}/x") == str(moved.build_dir / "x")


# Mirrors --------------------------------------------------------------------------------------------------------------

# Throwaway repositories: none of the user's git configuration (signing, commit hooks) applies to them.
ISOLATED = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", "/")}


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True,
                          env=ISOLATED).stdout.strip()


def repository(path: Path, files: dict[str, str]) -> str:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for name, text in files.items():
        (path / name).write_text(text)
    git(path, "add", ".")
    git(path, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "one")
    return git(path, "rev-parse", "HEAD")


def commit(path: Path, name: str, text: str) -> str:
    (path / name).write_text(text)
    git(path, "add", ".")
    git(path, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", name)
    return git(path, "rev-parse", "HEAD")


def test_fetch_mirrors_upstream_without_touching_the_checkouts(tmp_path):
    upstream = tmp_path / "upstream"
    pinned = repository(upstream / "spec", {"SPEC.md": "v1"})
    commit(upstream / "spec", "SPEC.md", "v2")  # the pin stays at v1
    first = repository(upstream / "assembler-x", {"a.txt": "1"})
    local = tmp_path / "local"
    for name in ("spec", "assembler-x"):
        subprocess.run(["git", "clone", "-q", str(upstream / name), str(local / name)], check=True, env=ISOLATED)
    path = tmp_path / "domain1.toml"
    path.write_text(f"""
[contract]
path = "{local / 'spec'}"
commit = "{pinned}"
[run]
suites = ["S1"]
results_dir = "{tmp_path / 'results'}"
[adapters.x]
checkout = "{local / 'assembler-x'}"
command = ["true"]
[ci.prepare]
x = [["touch", "prepared"]]
""")
    config = config_mod.load(path)
    directory, mirrors = ci.fetch(config, lambda _: None)
    assert mirrors["contract"]["commit"] == pinned
    assert mirrors["x"]["commit"] == first and mirrors["x"]["ref"] == "origin/main"
    assert (directory / "assembler-x" / "prepared").is_file()

    second = commit(upstream / "assembler-x", "a.txt", "2")
    _, mirrors = ci.fetch(config, lambda _: None)
    assert mirrors["x"]["commit"] == second
    assert git(local / "assembler-x", "rev-parse", "HEAD") == first  # the checkout itself is never moved
    assert not (local / "assembler-x" / "prepared").exists()


# Container variants ---------------------------------------------------------------------------------------------------

def test_variants_change_the_image_identity(fake_config):
    config = fake_config("oracle", extra="""
[container.variants.amd64]
platform = "linux/amd64"
[container.variants."python-3.11"]
images = { python = "docker.io/library/python:3.11-slim-trixie" }
[container.variants.bad]
images = { cobol = "x" }
""")
    amd64, py = container.variant(config, "amd64"), container.variant(config, "python-3.11")
    assert (amd64.cell, py.cell) == ("platform:linux/amd64", "toolchain:python-3.11")
    digests = {container.context_digest(config, [], v) for v in (None, amd64, py)}
    assert len(digests) == 3
    with pytest.raises(container.ContainerError, match="cobol"):
        container.variant(config, "bad")
    with pytest.raises(container.ContainerError, match="no \\[container.variants.missing\\]"):
        container.variant(config, "missing")


# The drift report -----------------------------------------------------------------------------------------------------

def manifest(run_id: str, go: str = "a" * 40, contract: str = "c" * 40) -> dict:
    return {"run_id": run_id, "status": "pass", "started_at": "2026-10-08T00:00:00.000Z",
            "contract": {"commit": contract}, "harness": {"source_digest": "h"}, "config": {"sha256": "s"},
            "adapters": {"go": {"available": True, "commit": go, "dirty": False, "toolchain": "go1.27"}},
            "ci": {"profile": "nightly", "source": "mirrors", "mirrors": None}}


def summary(status="pass", value=1.0, metric_status="pass", timing=10.0) -> dict:
    return {"status": status, "suites": [{"id": "S1", "status": status}],
            "metrics": [{"id": "s1.pass_rate", "label": "Cases passed", "suite": "S1", "adapter": "go", "unit": "rate",
                         "value": value, "status": metric_status},
                        {"id": "s7.startup_ms", "label": "Startup", "suite": "S7", "adapter": "go", "unit": "ms",
                         "value": timing, "status": "info"}]}


def finding(fid: str, severity="error") -> dict:
    return {"finding_id": fid, "suite": "S1", "adapter": "go", "severity": severity, "summary": fid}


def previous(tmp_path: Path, findings=(), goldens_regressions=0) -> Path:
    path = tmp_path / "20261007T000000Z-aaaaaaa"
    (path / "suites" / "S12").mkdir(parents=True)
    (path / "manifest.json").write_text(json.dumps(manifest(path.name)))
    (path / "summary.json").write_text(json.dumps(summary()))
    (path / "findings.jsonl").write_text("".join(json.dumps(f) + "\n" for f in findings))
    return path


def current(tmp_path: Path, regressions: int = 0) -> Path:
    path = tmp_path / "now"
    (path / "suites" / "S12").mkdir(parents=True)
    drift = [{"adapter": "go", "drift": "match", "count": 10}]
    if regressions:
        drift.append({"adapter": "go", "drift": "regression", "count": regressions})
    (path / "suites" / "S12" / "summary.json").write_text(json.dumps(
        {"goldens": {"adopted": {"from_run": "x"}, "drift": drift, "removed": []}}))
    return path


def test_verdicts(tmp_path):
    before = previous(tmp_path, [finding("f1")])
    now = current(tmp_path)
    assert ci.compare(now, manifest("n"), summary(), [], None)["verdict"] == "baseline"
    same = ci.compare(now, manifest("n"), summary(timing=12.5), [finding("f1")], before)
    assert same["verdict"] == "unchanged" and same["findings"]["persisting"] == 1  # a timing alone is no change
    assert [m["id"] for m in same["metrics"]] == ["s7.startup_ms"]

    bumped = ci.compare(now, manifest("n", go="b" * 40), summary(), [], before)
    assert bumped["verdict"] == "changed"
    assert bumped["bumps"] == [{"what": "adapter:go", "from": "a" * 40, "to": "b" * 40}]
    assert [f["finding_id"] for f in bumped["findings"]["resolved"]] == ["f1"]

    assert ci.compare(now, manifest("n"), summary(), [finding("f1"), finding("f2")], before)["verdict"] == "regressed"
    warned = ci.compare(now, manifest("n"), summary(), [finding("f1"), finding("w", "warning")], before)
    assert warned["verdict"] == "changed"
    worse = ci.compare(now, manifest("n"), summary("fail", 0.5, "fail"), [finding("f1")], before)
    assert worse["verdict"] == "regressed" and worse["suites"] == [{"id": "S1", "status": "fail", "previous": "pass"}]
    golden = ci.compare(current(tmp_path / "g", regressions=3), manifest("n"), summary(), [finding("f1")], before)
    assert golden["verdict"] == "regressed" and golden["goldens"]["drift"] == {"match": 10, "regression": 3}


def test_previous_run_is_the_newest_earlier_run_of_the_same_profile(tmp_path):
    for run_id, profile in (("20261001T000000Z-aaaaaaa", "nightly"), ("20261002T000000Z-aaaaaaa", "weekly"),
                            ("20261003T000000Z-aaaaaaa", "nightly"), ("20261005T000000Z-aaaaaaa", "nightly")):
        (tmp_path / run_id).mkdir()
        (tmp_path / run_id / "manifest.json").write_text(json.dumps(
            {"run_id": run_id, "status": "pass", "ci": {"profile": profile}}))
    assert ci.previous_run(tmp_path, "nightly", "20261004T000000Z-aaaaaaa").name == "20261003T000000Z-aaaaaaa"
    assert ci.previous_run(tmp_path, "weekly", "20261001T000000Z-aaaaaaa") is None


# End to end -----------------------------------------------------------------------------------------------------------

def test_ci_runs_reports_drift_and_publishes(fake_config):
    config = fake_config("oracle", extra=PROFILES)
    first, status, report = ci.run(config, "nightly", build=False, log=lambda _: None)
    assert status == "pass" and report["verdict"] == "baseline" and report["source"] == "checkouts"
    results = config.results_dir
    assert (results / "nightly").resolve() == first.resolve() == (results / "latest").resolve()
    assert validate_run(first) == []

    second, status, report = ci.run(config, "nightly", build=False, log=lambda _: None)
    assert report["verdict"] == "unchanged" and report["previous"]["run_id"] == first.name
    assert (results / "nightly").resolve() == second.resolve()

    broken = fake_config("flip-payload", extra=PROFILES)  # another config file: a bump, and new findings
    third, status, report = ci.run(broken, "nightly", build=False, log=lambda _: None)
    assert status == "fail" and report["verdict"] == "regressed"
    assert {b["what"] for b in report["bumps"]} == {"config"}
    assert report["findings"]["new"] and report["suites"] == [{"id": "S1", "status": "fail", "previous": "pass"}]
    assert validate_run(third) == []
    index = json.loads((results / "index.json").read_text())
    assert {r["run_id"]: r["ci_profile"] for r in index["runs"]} == {
        first.name: "nightly", second.name: "nightly", third.name: "nightly"}
