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


def test_dumps_refuses_nan():
    with pytest.raises(ValueError):
        output.dumps({"x": float("nan")})
