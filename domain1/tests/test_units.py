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


def test_dumps_refuses_nan():
    with pytest.raises(ValueError):
        output.dumps({"x": float("nan")})
