from __future__ import annotations

import base64
import json

import pytest

from cwabench import metrics, output, traces
from cwabench.adapters import Invocation, classify, unsupported_components
from cwabench.contract import utf16_key


def _invocation(code, stdout=b"", stderr=b"", timed_out=False):
    return Invocation(code, stdout, stderr, 1.0, timed_out)


def test_diff_is_type_strict():
    assert traces.diff({"a": True}, {"a": 1})[0].pointer == "/a"
    assert traces.diff({"a": 1}, {"a": 1.0}) == []  # JSON has one number type
    assert traces.diff([1, 2], [1]) [0].missing == "actual"


def test_diff_escapes_pointers():
    assert traces.diff({"a/b": {"c~d": 1}}, {"a/b": {"c~d": 2}})[0].pointer == "/a~1b/c~0d"


def test_normalize_removes_only_volatile_fields():
    trace = {"trace_id": "x", "timings": {}, "recovery": {"action": "request_context", "detail": "y"}, "k": 1}
    assert traces.normalize(trace) == {"recovery": {"action": "request_context"}, "k": 1}
    assert trace["recovery"]["detail"] == "y"  # the input is not changed


def test_charged_tokens_rounds_up():
    assert traces.charged_tokens({"result": {"input_tokens": 101}, "budget": {"margin_percent": 15}}) == 117
    assert traces.charged_tokens({"result": {"input_tokens": 100}, "budget": {}}) == 100
    assert traces.charged_tokens({"result": None, "budget": {}}) is None


@pytest.mark.parametrize("invocation, kind", [
    (_invocation(None, timed_out=True), "timeout"),
    (_invocation(2), "rejected"),
    (_invocation(3, stderr=b"renderer x/v1 is not provided\n"), "unsupported"),
    (_invocation(1, stderr=b"boom"), "crashed"),
    (_invocation(0, b"nope"), "invalid_output"),
    (_invocation(0, b'{"payload": null}'), "invalid_output"),
    (_invocation(0, b'{"payload": "!!", "trace": {"refused": {"bool": false}}}'), "invalid_output"),
    (_invocation(0, b'{"payload": null, "trace": {"refused": {"bool": 1}}}'), "invalid_output"),
    (_invocation(0, b'{"payload": null, "trace": {"refused": {"bool": true, "reason": "evidence_required"}}}'), "refused"),
])
def test_classify(invocation, kind):
    assert classify(invocation).kind == kind


def test_classify_decodes_payload():
    stdout = json.dumps({"payload": base64.b64encode(b"hi").decode(), "trace": {"refused": {"bool": False}}})
    outcome = classify(_invocation(0, stdout.encode()))
    assert outcome.kind == "assembled" and outcome.payload == b"hi"


def test_unsupported_lines():
    stderr = b"tokenizer a/v1 is not provided\nnoise\nrenderer b/v1 is not provided\n"
    assert unsupported_components(stderr) == (("tokenizer", "a/v1"), ("renderer", "b/v1"))


def test_utf16_order_differs_from_code_points():
    astral, high_bmp = "\U0001F600", "ｚ"
    assert sorted([high_bmp, astral], key=utf16_key) == [astral, high_bmp]
    assert sorted([high_bmp, astral]) == [high_bmp, astral]


def test_rate_metric_statuses():
    assert metrics.rate("m", "M", 1, 1)["status"] == "pass"
    assert metrics.rate("m", "M", 0, 1)["status"] == "fail"
    assert metrics.rate("m", "M", 0, 0)["value"] is None
    assert metrics.rate("m", "M", 1, 2, target=None)["status"] == "info"


def test_percentiles_nearest_rank():
    assert metrics.percentiles([5, 1, 3, 2, 4])["p50"] == 3
    assert metrics.percentiles([])["p50"] is None


def test_every_schema_is_valid_and_rejects_strangers():
    for path in sorted(output.SCHEMA_DIR.glob("*.v1.schema.json")):
        kind = path.name.removesuffix(".v1.schema.json")
        output.validator(kind)  # check_schema runs here
        with pytest.raises(output.OutputError):
            output.validate({"$schema": output.schema_name(kind), "unexpected": True})


def test_another_domain_names_its_own_documents(tmp_path):
    """A second domain reuses the run directory, the blob store and validation under its own prefix
    (domain-2-plan.md, 4.2), and Domain 1's names are untouched."""
    from types import SimpleNamespace

    from cwabench import rundir

    schemas = tmp_path / "schemas"
    schemas.mkdir()
    for kind in ("blob", "run-index", "runs-index", "suite-summary"):
        text = (output.SCHEMA_DIR / f"{kind}.v1.schema.json").read_text(encoding="utf-8")
        (schemas / f"{kind}.v1.schema.json").write_text(text.replace("cwa-bench-d1", "cwa-bench-dx"), encoding="utf-8")
    dx = output.Domain("cwa-bench-dx", schemas)
    with pytest.raises(output.OutputError, match="registered domain"):
        output.validate({"$schema": "cwa-bench-dx/blob/v1"})
    assert output.register(dx) is dx and output.register(output.Domain("cwa-bench-dx", schemas)) == dx
    with pytest.raises(output.OutputError, match="already registered"):
        output.register(output.Domain("cwa-bench-dx", tmp_path))
    assert output.kind_of({"$schema": "cwa-bench-dx/run-index/v1"}) == ("run-index", 1)

    config_path = tmp_path / "dx.toml"
    config_path.write_text("", encoding="utf-8")
    config = SimpleNamespace(path=config_path, sha256="0" * 64, results_dir=tmp_path / "results", upstream_path=None)
    run = rundir.RunDir(config, domain=dx, sources=((schemas, ("*.json",)),))
    assert run.harness()["name"] == "cwa-bench-dx" and run.source_digest != rundir.source_digest()
    run.blobs.put_text(b"x")
    run.finalize("pass", [])
    index = json.loads((run.path / "index.json").read_text(encoding="utf-8"))
    assert index["$schema"] == "cwa-bench-dx/run-index/v1"
    assert json.loads((tmp_path / "results" / "index.json").read_text())["$schema"] == "cwa-bench-dx/runs-index/v1"
    blob = json.loads((run.path / "blobs" / "index.jsonl").read_text().splitlines()[0])
    assert blob["$schema"] == "cwa-bench-dx/blob/v1"
    assert [f["schema"] for f in index["files"] if f["kind"] == "blob"] == ["cwa-bench-dx/blob/v1"]
    assert output.schema_name("blob") == "cwa-bench-d1/blob/v1"


def test_runs_index_names_each_profiles_newest_finished_run_and_the_commits(tmp_path):
    from cwabench import rundir

    def manifest(run_id, status, profile):
        directory = tmp_path / run_id
        directory.mkdir()
        (directory / "manifest.json").write_text(json.dumps({
            "run_id": run_id, "status": status, "started_at": "2026-10-08T00:00:00Z", "finished_at": None,
            "suites": ["S1"], "adapters": {"go": {"commit": "g" * 40}, "rust": {"commit": None}},
            "contract": {"commit": "c" * 40}, "ci": {"profile": profile} if profile else None}), encoding="utf-8")

    manifest("20261008T000001Z-aaaaaaa", "pass", "nightly")
    manifest("20261008T000002Z-aaaaaaa", "fail", "nightly")  # finished, so it is the profile's newest result
    manifest("20261008T000003Z-aaaaaaa", "running", "nightly")
    manifest("20261008T000004Z-aaaaaaa", "error", "weekly")  # stopped by an error: no result
    manifest("20261008T000005Z-aaaaaaa", "pass", None)
    rundir.update_runs_index(tmp_path, link=False)
    index = json.loads((tmp_path / "index.json").read_text(encoding="utf-8"))
    output.validate(index)
    assert index["latest"] == "20261008T000005Z-aaaaaaa"
    assert index["profiles"] == {"nightly": "20261008T000002Z-aaaaaaa"}
    assert [r["run_id"] for r in index["runs"]][:2] == ["20261008T000005Z-aaaaaaa", "20261008T000004Z-aaaaaaa"]
    assert index["runs"][0]["commits"] == {"contract": "c" * 40, "go": "g" * 40, "rust": None}
    assert index["runs"][0]["adapters"] == ["go", "rust"]
    assert not (tmp_path / "latest").exists()


def test_findings_carry_their_upstream_link(fake_config, tmp_path):
    from cwabench.config import ConfigError
    from cwabench.runner import run
    from cwabench.validate import validate_run

    def findings(run_dir, relative="findings.jsonl"):
        return [json.loads(line) for line in (run_dir / relative).read_text(encoding="utf-8").splitlines()]

    first, status = run(fake_config("flip-payload"), build=False, log=lambda _: None)
    assert status == "fail"
    rows = findings(first)
    assert rows and all(row["upstream"] is None for row in rows)  # no [findings].upstream file: no links

    link = {"url": "https://github.com/contextwindowarchitecture/assembler-python/issues/1", "state": "fixed",
            "note": "a test"}
    (tmp_path / "upstream.json").write_text(json.dumps(
        {"$schema": "cwa-bench-d1/upstream/v1", "findings": {rows[0]["finding_id"]: link}}), encoding="utf-8")
    second, _ = run(fake_config("flip-payload", extra='[findings]\nupstream = "upstream.json"\n'), build=False,
                    log=lambda _: None)
    assert findings(second)[0]["upstream"] == link  # the id is the signature's hash, so it is the same run to run
    assert findings(second, "suites/S1/findings.jsonl")[0]["upstream"] == link
    assert validate_run(second) == []
    with pytest.raises(ConfigError):
        fake_config("flip-payload", extra='[findings]\nupstream = "missing.json"\n')


def test_dumps_refuses_nan():
    with pytest.raises(ValueError):
        output.dumps({"x": float("nan")})


def test_cwabench_dispatches_to_the_domain_it_is_asked_for(monkeypatch, capsys):
    from cwabench import cli

    seen = []

    class Fake:
        def load(self):
            return lambda argv: seen.append(argv) or 7

    monkeypatch.setattr(cli, "domains", lambda: {"1": None, "9": Fake()})
    assert cli.main(["--domain", "9", "run", "--size", "pilot"]) == 7 and seen == [["run", "--size", "pilot"]]
    assert cli.main(["--domain=4", "run"]) == 2
    assert "no domain '4' is installed (installed: 1, 9)" in capsys.readouterr().err
    monkeypatch.setattr(cli, "run_domain1", lambda argv: seen.append(("d1", argv)) or 0)
    assert cli.main(["validate"]) == 0 and cli.main(["--domain", "1", "validate"]) == 0
    assert seen[1:] == [("d1", ["validate"]), ("d1", ["validate"])]


def test_the_container_engine_is_podman_or_docker(fake_config):
    import argparse
    import os

    from cwabench import cli, container

    from cwabench import config as config_mod

    config = fake_config("ok")
    text = config.path.read_text(encoding="utf-8")
    config.path.write_text(text.replace("[container]\n", '[container]\nengine = "docker"\n'), encoding="utf-8")
    config = config_mod.load(config.path)
    assert config.settings["container"]["engine"] == "docker"
    config.path.write_text(text.replace("[container]\n", '[container]\nengine = "lxc"\n'), encoding="utf-8")
    with pytest.raises(config_mod.ConfigError, match="engine must be one of podman, docker"):
        config_mod.load(config.path)
    config.path.write_text(text.replace("[container]\n", '[container]\nengine = "docker"\n'), encoding="utf-8")
    overridden = cli._load(argparse.Namespace(config=str(config.path), container_engine="podman"))
    assert overridden.settings["container"]["engine"] == "podman"
    # Docker runs the container as the calling user, so what it writes to the work directory stays removable;
    # rootless Podman already maps the container's root to the calling user
    assert container.identity("/usr/bin/docker") == ["--user", f"{os.getuid()}:{os.getgid()}"]
    assert container.identity("/opt/podman/bin/podman") == []
