"""S7's pieces: the independent renderer, the scale generator and its predictions, timelines, and the measurement
code (sampling, confidence intervals, log-log fits, peak RSS). An opt-in test runs S7 end to end on the reference
assembler with planted defects."""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import pytest

from cwabench import perf, timelines
from cwabench.canon import render, validity
from cwabench.corpora import scale

SMALL = [("compressible-v1", 10, 2000), ("floors", 20, 2000), ("surfaced-conflicts", 12, 2000),
         ("fitting-order", 20, 2000), ("slot-caps", 20, 2000), ("droppable-heavy", 20, 2000),
         ("compressible-v0", 10, 2000), ("compressible-v3", 10, 2000)]


# The renderer ----------------------------------------------------------------------------------------------------------

def test_renderer_writes_every_published_payload_and_count(contract):
    checked = 0
    for case in contract.cases:
        if case.expected_payload is None:
            continue
        rendered = render.from_trace(case.snapshot, case.expected_trace)
        assert rendered.payload == case.expected_payload, case.id
        assert rendered.count(case.snapshot["tokenizer"]) == case.expected_trace["result"]["input_tokens"], case.id
        checked += 1
    assert checked >= 40


def test_charged_count_rounds_up():
    assert render.charged(100, 0) == 100
    assert render.charged(100, 10) == 110
    assert render.charged(101, 10) == 112  # 111.1 rounds up


# The generator -----------------------------------------------------------------------------------------------------------

def test_every_shape_covers_the_plan():
    assert set(scale.SHAPES) == {"droppable-heavy", "compressible-v0", "compressible-v1", "compressible-v3", "floors",
                                 "slot-caps", "fitting-order", "surfaced-conflicts"}


@pytest.mark.parametrize("shape, n, t", SMALL)
def test_cells_are_valid_deterministic_and_ordered(contract, shape, n, t):
    cell = scale.Cell(shape, n, t)
    a, b = scale.build(contract, cell), scale.build(contract, cell)
    assert a.at(a.full) == b.at(b.full)
    assert scale.build(contract, scale.Cell(shape, n, t, seed=1)).at(a.full) != a.at(a.full)
    assert validity.is_valid(contract, a.at(a.full))
    assert 0 < a.protected < a.full
    assert len(a.all_items) == n + 2  # the instructions and the query are protected, beyond the candidates
    if shape == "floors":
        assert a.protected < a.floor < a.full
    assert a.expect(a.protected - 1) == ("refused", "protected_content_over_budget")
    assert a.expect(a.full) == ("assembled", None)
    if a.floor:
        assert a.expect(a.floor - 1) == ("refused", "slot_floor_over_budget")
        assert a.expect(a.floor) == ("assembled", None)
    else:
        assert a.expect(a.protected) == ("assembled", None)


@pytest.mark.parametrize("tokenizer", scale.TOKENIZERS)
@pytest.mark.parametrize("renderer", scale.RENDERERS)
def test_every_component_pair_builds_valid_snapshots(contract, tokenizer, renderer):
    built = scale.build(contract, scale.Cell("surfaced-conflicts", 12, 3000, tokenizer, renderer))
    assert validity.is_valid(contract, built.at(built.full))
    assert abs(built.full - 3000) / 3000 < 0.25  # bodies are sized to the target total


def test_bodies_count_the_same_under_both_tokenizers():
    import random

    from cwabench.canon.tokenizers import estimate_utf8, fixture_whitespace

    body = scale._body(random.Random(1), "abc", 57)
    assert fixture_whitespace(body) == estimate_utf8(body) == 57


def test_too_few_tokens_per_candidate_is_not_constructible(contract):
    with pytest.raises(scale.NotConstructible):
        scale.build(contract, scale.Cell("compressible-v0", 5000, 10000))


def test_the_grid_lists_every_shape_and_component(contract):
    cells = scale.cells({"candidates": [10, 100], "candidate_tokens": [10000], "components_cell": [10, 10000]})
    assert {c.shape for c in cells} == set(scale.SHAPES)
    assert {(c.tokenizer, c.renderer) for c in cells} == {(t, r) for t in scale.TOKENIZERS for r in scale.RENDERERS}


def test_cache_template_replaces_only_the_budget(contract, tmp_path):
    from cwabench.suites.s7_scale import SENTINEL, Meta

    built = scale.build(contract, scale.Cell("floors", 20, 2000))
    path = tmp_path / "cell.json"
    path.write_bytes(built.at(SENTINEL))
    meta = Meta(built.cell, path, built.full, built.protected, built.floor, [], 22, 0)
    assert meta.at(123) == built.at(123)
    assert meta.budget("threshold-1") == built.protected - 1
    assert meta.budget("ratio:0.5") == round(built.full * 0.5)
    assert meta.budget("window:8192") == 8192


# Timelines -----------------------------------------------------------------------------------------------------------

def test_timelines_account_for_every_row(contract):
    for case in contract.cases:
        trace = case.expected_trace
        document = timelines.build(contract, case.snapshot, trace, "expected", "sha256:" + "0" * 64,
                                   "20261008T000000Z-0000000")
        events = document["events"]
        assert [e["seq"] for e in events] == list(range(len(events)))
        excluded = [e for e in events if e["type"] in ("excluded", "omitted")]
        assert len(excluded) == len(trace["excluded"]), case.id
        assert len([e for e in events if e["type"] == "placed"]) == len(trace["included"]), case.id
        assert len([e for e in events if e["type"] == "compressed"]) == len(trace["compressed"]), case.id
        lanes = [timelines.LANES.index(e["stage"]) for e in events if e["stage"] != "fit"]
        assert lanes == sorted(lanes), case.id
        assert all(e["order"] == "inferred" for e in events if e["type"] == "compressed")


def test_timeline_places_compression_by_the_routes_fitting_order(contract):
    """fitting_order omits history before compressing knowledge: the omission comes first, then the compression."""
    built = scale.build(contract, scale.Cell("fitting-order", 10, 2000))
    snapshot = json.loads(built.at(built.full))
    history = next(i for i in built.all_items if i["slot"] == "interaction.history")
    knowledge = next(i for i in built.all_items if i["slot"] == "evidence.knowledge" and i["variants"])
    example = next(i for i in built.all_items if i["slot"] == "governance.examples")
    trace = {"excluded": [{"item_id": example["id"], "reason": "over_budget", "stage": "assembler",
                           "slot": example["slot"]},
                          {"item_id": history["id"], "reason": "over_budget", "stage": "assembler",
                           "slot": history["slot"]}],
             "compressed": [{"item_id": knowledge["id"], "slot": knowledge["slot"],
                             "variant_id": knowledge["variants"][0]["id"], "from": 9, "to": 3, "method": "stub-lead"}],
             "included": [], "conflicts": [], "defaults_filled": [], "refused": {"bool": False, "reason": None},
             "result": {"input_tokens": 1, "hash": "0" * 64}}
    events = timelines.build(contract, snapshot, trace, "x", "sha256:" + "0" * 64, "20261008T000000Z-0000000")["events"]
    fit = [(e["type"], e["item_id"]) for e in events if e["stage"] == "fit"]
    assert fit == [("omitted", example["id"]), ("omitted", history["id"]), ("compressed", knowledge["id"])]


# Measurement ---------------------------------------------------------------------------------------------------------

def test_loglog_recovers_known_exponents():
    points = [({"candidates": n, "tokens": t}, 0.003 * n ** 1.0 * t ** 0.5) for n in (10, 100, 1000)
              for t in (1e4, 1e5, 1e6)]
    fit = perf.loglog(points, ["candidates", "tokens"])
    assert math.isclose(fit.coefficients["candidates"], 1.0, abs_tol=1e-9)
    assert math.isclose(fit.coefficients["tokens"], 0.5, abs_tol=1e-9)
    assert math.isclose(fit.r2, 1.0)


def test_loglog_needs_variation_in_every_variable():
    points = [({"candidates": n, "tokens": 1e4}, float(n)) for n in (10, 100, 1000, 10000)]
    assert perf.loglog(points, ["candidates", "tokens"]) is None
    assert perf.loglog(points, ["candidates"]).coefficients["candidates"] == pytest.approx(1.0)


def test_confidence_interval_and_stopping_rule():
    steady = perf.Stats([100.0] * 10)
    assert steady.ci95_rate == 0 and steady.as_json(0.05)["ci_met"]
    noisy = perf.Stats([50.0, 150.0] * 5)
    assert noisy.ci95_rate > 0.05
    policy = perf.Policy(min_samples=10, max_samples=30, max_seconds=100)
    assert not policy.done(perf.Stats([100.0] * 9), 0)
    assert policy.done(steady, 0)
    assert not policy.done(noisy, 0)
    assert policy.done(perf.Stats([50.0, 150.0] * 15), 0)  # the sample cap
    assert policy.done(noisy, 101)  # the time cap
    assert not policy.done(perf.Stats([100.0, 101.0]), 101)  # but never fewer than three samples
    slow = perf.Policy(min_samples=10, min_slow=3, slow_ms=1000)
    assert slow.done(perf.Stats([5000.0, 5001.0, 5002.0]), 0)  # three samples suffice for a slow cell


def test_sample_stops_at_the_first_failure():
    values = iter([1.0, 2.0, None, 3.0])
    stats, ok = perf.sample(lambda: next(values), perf.Policy(warmup=1, min_samples=10, max_seconds=100))
    assert not ok and stats.samples == [2.0]


def _script(tmp_path: Path, body: str):
    from cwabench.adapters import Adapter
    from cwabench.config import AdapterConfig
    from cwabench.gitinfo import Checkout

    path = tmp_path / "script.py"
    path.write_text(body, encoding="utf-8")
    config = AdapterConfig("script", "Python", tmp_path, [sys.executable, str(path)])
    return Adapter(config, config.command, {}, Checkout(None, None, None, None, None), None, None, None)


def test_invoke_measured_reports_the_childs_own_peak_rss(tmp_path):
    adapter = _script(tmp_path, "import sys\nblock = bytearray(64 * 1024 * 1024)\nsys.stdout.write(sys.stdin.read())\n")
    measured = perf.invoke_measured(adapter, b"hello", 30, tmp_path)
    assert measured.invocation.exit_code == 0 and measured.invocation.stdout == b"hello"
    assert measured.rss_bytes is not None and measured.rss_bytes > 64 * 1024 * 1024


def test_invoke_measured_times_out(tmp_path):
    adapter = _script(tmp_path, "import time\ntime.sleep(30)\n")
    measured = perf.invoke_measured(adapter, b"", 0.5, tmp_path)
    assert measured.invocation.timed_out and measured.rss_bytes is None
    assert measured.invocation.wall_ms < 10000


# S7 end to end (opt-in: needs the reference assembler) ------------------------------------------------------------------

reference = pytest.mark.skipif(os.environ.get("CWA_BENCH_REFERENCE") != "1",
                               reason="set CWA_BENCH_REFERENCE=1 to run S7 on the reference assembler with planted "
                                      "defects (needs `cwabench setup`)")


@reference
def test_s7_catches_each_planted_defect(tmp_path, spec):
    from cwabench import config as config_mod
    from cwabench import gitinfo
    from cwabench.runner import run

    root = Path(__file__).resolve().parent.parent
    python = root / ".build/python-venv/bin/python"
    modes = ("none", "budget-plus-one", "truncate-protected", "slow")
    adapters = "".join(f"""
[adapters.{mode.replace('-', '_')}]
language = "Python"
checkout = {json.dumps(str(root / "../../../assembler-python"))}
command = [{json.dumps(str(python))}, {json.dumps(str(root / "tests/buggy_adapter.py"))}]
env = {{ CWA_BUGGY_MODE = {json.dumps(mode)} }}
""" for mode in modes)
    path = tmp_path / "s7.toml"
    path.write_text(f"""
[contract]
path = {json.dumps(str(spec))}
commit = {json.dumps(gitinfo.inspect(spec).commit)}
allow_dirty = true
[run]
suites = ["S7"]
adapters = {json.dumps([m.replace('-', '_') for m in modes])}
results_dir = {json.dumps(str(tmp_path / "results"))}
{adapters}
[container]
enabled = false
[s7]
timeout_s = 8
shapes = ["compressible-v1", "floors"]
candidates = [10, 100, 1000]
candidate_tokens = [10000]
budget_ratio = [1.0, 0.1]
components_cell = [10, 10000]
components_ratio = [0.1]
window_shapes = []
threshold_cells = [[10, 2000]]
sweep_cell = [10, 500]
perf = false
max_tests = 20
""", encoding="utf-8")
    run_dir, status = run(config_mod.load(path), build=False, log=lambda _: None)
    assert status == "fail"
    findings = [json.loads(line) for line in (run_dir / "suites/S7/findings.jsonl").read_text().splitlines()]
    by_adapter = {}
    for f in findings:
        by_adapter.setdefault(f["adapter"], set()).update(f["checks"])
    assert "none" not in by_adapter, by_adapter.get("none")  # the reference assembler is clean
    # One token more than the snapshot allows: it assembles one token below the threshold.
    assert {"prediction", "threshold", "A5"} <= by_adapter["budget_plus_one"]
    assert {"protected_preservation", "A4", "A6"} <= by_adapter["truncate_protected"]
    assert by_adapter["slow"] == {"timeout"}
    slow = next(f for f in findings if f["adapter"] == "slow")
    assert slow["severity"] == "warning"
    rows = [json.loads(line) for line in (run_dir / "suites/S7/results.jsonl").read_text().splitlines()]
    assert any(s["adapter"] == "slow" for r in rows for s in r["skipped"])  # dominated cells were not run
    summary = json.loads((run_dir / "suites/S7/summary.json").read_text())
    exact = {(t["adapter"], t["status"]) for t in summary["scale"]["thresholds"]}
    assert ("none", "exact") in exact and ("budget_plus_one", "inexact") in exact
    sweep = json.loads((run_dir / summary["scale"]["sweeps"][0]["path"]).read_text())
    assert sweep["adapters"]["none"]["frames"][0]["budget_input"] == sweep["full"]


def test_self_reported_time_prefers_a_reported_total():
    from cwabench.suites.s7_scale import _self_reported

    assert _self_reported({"timings": {"admission_ms": 1.0, "fitting_ms": 2.0, "total_ms": 3.5}}) == 3.5
    assert _self_reported({"timings": {"admit": 1.0, "fit": 2.0}}) == 3.0
    assert _self_reported({"timings": {}}) is None and _self_reported(None) is None


def test_curve_summarizes_each_frame():
    from cwabench.suites.s7_scale import _curve

    frames = [
        {"budget_input": 100, "outcome": "assembled", "refusal_reason": None, "charged_tokens": 98, "exact_step": False,
         "included": [{"item_id": "a", "slot": "s", "tokens": 50, "variant_id": None},
                      {"item_id": "b", "slot": "s", "tokens": 48, "variant_id": "b~v1"}], "omitted": []},
        {"budget_input": 60, "outcome": "assembled", "refusal_reason": None, "charged_tokens": 50, "exact_step": True,
         "included": [{"item_id": "a", "slot": "s", "tokens": 50, "variant_id": None}], "omitted": ["b"]},
        {"budget_input": 10, "outcome": "refused", "refusal_reason": "protected_content_over_budget",
         "charged_tokens": None, "included": [], "omitted": []},  # exact_step is set later by run_sweeps
    ]
    assert _curve(frames) == [
        {"budget_input": 100, "outcome": "assembled", "refusal_reason": None, "charged_tokens": 98, "included": 2,
         "compressed": 1, "omitted": 0, "exact_step": False},
        {"budget_input": 60, "outcome": "assembled", "refusal_reason": None, "charged_tokens": 50, "included": 1,
         "compressed": 0, "omitted": 1, "exact_step": True},
        {"budget_input": 10, "outcome": "refused", "refusal_reason": "protected_content_over_budget",
         "charged_tokens": None, "included": 0, "compressed": 0, "omitted": 0, "exact_step": False},
    ]
    document = {"$schema": "cwa-bench-d1/sweep/v1", "run_id": "20261008T000000Z-aaaaaaa", "suite": "S7",
                "cell": {"shape": "floors", "candidates": 1, "candidate_tokens": 10, "tokenizer": "fixture-whitespace/v1",
                         "renderer": "fixture-xml/v1", "seed": 1},
                "snapshot": "sha256:" + "0" * 64, "full": 100, "protected": 10, "floor": None, "step_percent": 2.0,
                "agree": True, "threshold": 60, "mispredicted": {"fake": 0}, "audit_failed": {"fake": []},
                "shedding": [], "shedding_from": "fake",
                "adapters": {"fake": {"frames": [dict(f, predicted=[f["outcome"], f["refusal_reason"]],
                                                       exact_step=f.get("exact_step", False), input_tokens=None,
                                                       audit_failed=[]) for f in frames],
                                      "curve": _curve(frames)}},
                "timelines": []}
    from cwabench import output

    output.validate(document)
