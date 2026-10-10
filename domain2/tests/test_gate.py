from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cwabench import gitinfo

DOMAIN1 = Path(__file__).resolve().parent.parent.parent / "domain1"

# On the reference assembler ------------------------------------------------------------------------------------------

reference = pytest.mark.skipif(os.environ.get("CWA_BENCH_REFERENCE") != "1",
                               reason="set CWA_BENCH_REFERENCE=1 to run S1 on the reference assembler (needs "
                                      "`cwabench setup`)")


LADDER = '["cwa-history", "cwa-state", "cwa-memory", "cwa-pipeline", "cwa-format"]'


def _configs(tmp_path, spec, modes, suites=("S0", "S1", "S7"), extra="", arms=LADDER, summarizer="stub"):
    """A Domain 1 configuration whose adapters are the reference assembler in each buggy_adapter mode, and a
    Domain 2 configuration that assembles with them."""
    python = DOMAIN1 / ".build/python-venv/bin/python"
    commit = gitinfo.inspect(spec).commit
    tables = "".join(f"""
[adapters.{mode.replace('-', '_')}]
language = "Python"
checkout = {json.dumps(str(DOMAIN1 / "../../../assembler-python"))}
command = [{json.dumps(str(python))}, {json.dumps(str(DOMAIN1 / "tests/buggy_adapter.py"))}]
env = {{ CWA_BUGGY_MODE = {json.dumps(mode)} }}
""" for mode in modes)
    d1 = tmp_path / "d1.toml"
    d1.write_text(f"""
[contract]
path = {json.dumps(str(spec))}
commit = {json.dumps(commit)}
allow_dirty = true
[run]
results_dir = {json.dumps(str(tmp_path / "d1-results"))}
{tables}""", encoding="utf-8")
    names = [m.replace("-", "_") for m in modes]
    d2 = tmp_path / "d2.toml"
    d2.write_text(f"""
[contract]
path = {json.dumps(str(spec))}
commit = {json.dumps(commit)}
allow_dirty = true
[run]
suites = {json.dumps(list(suites))}
results_dir = {json.dumps(str(tmp_path / "results"))}
concurrency = 4
[adapters]
config = {json.dumps(str(d1))}
use = {json.dumps(names)}
payload_source = {json.dumps(names[0])}
[budgets]
input = [700]
ratios = [1.0, 0.4]
[arms]
cwa = {arms}
[baselines]
summarizer = {json.dumps(summarizer)}
[s7]
goldens = {json.dumps(str(tmp_path / "goldens.json"))}
[turns]
counts = [12]
checkpoint_every = 6
[families.vt]
seed = 5
sizes = {{ pilot = 1, recorded = 1 }}
variables = 2
distractors = 1
assignment_density = 0.5
filler_sentences = 2
reply_sentences = 2
[families.fr]
seed = 6
sizes = {{ pilot = 2, recorded = 2 }}
tasks = ["record", "compute"]
fields = 4
steps = 4
filler_sentences = 2
reply_sentences = 2
{extra}""", encoding="utf-8")
    return d2


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


@reference
def test_s1_passes_on_the_reference_and_s7_adopts_its_goldens(tmp_path, spec):
    from cwabench.validate import validate_run
    from cwabench2 import config as config_mod
    from cwabench2.runner import run
    from cwabench2.suites.s7_goldens import accept

    config = config_mod.load(_configs(tmp_path, spec, ["none"]))
    run_dir, status = run(config, build=False, log=lambda m: None)
    assert status == "pass", [f["summary"] for f in map(json.loads, (run_dir / "findings.jsonl").read_text()
                                                        .splitlines())]
    assert validate_run(run_dir) == []
    summary = _read(run_dir / "suites/S1/summary.json")
    assert summary["gate"]["passed"] == summary["gate"]["total"] == 3 * 5
    rows = [json.loads(line) for line in (run_dir / "suites/S1/turns.jsonl").read_text().splitlines()]
    assert {r["point"] for r in rows} == {"turn", "probe"} and all(r["verdict"] == "passed" for r in rows)
    probes = [r for r in rows if r["point"] == "probe"]
    assert all(r["fact"]["by_trace"] == r["fact"]["by_text"] for r in probes)
    assert any(not r["fact"]["present"] for r in probes if r["arm"] == "cwa-history" and r["tier"] == "700")
    assert summary["rows"]["timelines"] == 3 * 5 * 3
    target, count = accept(config, run_dir)
    assert count == len(probes) and target.is_file()
    again, status = run(config, build=False, log=lambda m: None)
    s7 = _read(again / "suites/S7/summary.json")
    assert status == "pass" and s7["goldens"]["drift"]["match"] == len(probes)


@reference
def test_s1_fails_an_assembler_that_truncates_protected_content(tmp_path, spec):
    from cwabench.validate import validate_run
    from cwabench2 import config as config_mod
    from cwabench2.runner import run

    config = config_mod.load(_configs(tmp_path, spec, ["none", "truncate-protected"]))
    run_dir, status = run(config, build=False, log=lambda m: None)
    assert status == "fail" and validate_run(run_dir) == []
    findings = [json.loads(line) for line in (run_dir / "findings.jsonl").read_text().splitlines()]
    caught = {(f["oracle"], f["adapter"]) for f in findings if f["suite"] == "S1"}
    assert ("expected", "truncate_protected") in caught and ("differential", None) in caught
    assert ("auditor", "truncate_protected") in caught
    assert not any(adapter == "none" for _, adapter in caught)
    summary = _read(run_dir / "suites/S1/summary.json")
    assert summary["gate"]["passed"] == 0


@reference
def test_s2_replays_its_llm_run_byte_for_byte(tmp_path, spec, monkeypatch):
    from cwabench.validate import validate_run
    from cwabench2 import config as config_mod
    from cwabench2.runner import run

    answers = []

    def chat(self, handed):  # a fake endpoint: answers each probe with the last number its payload states
        import re

        found = re.findall(r"\d[\d,]*", handed[-1]["content"] + " ".join(m["content"] for m in handed[:-1]))
        answers.append(handed)
        prompt = sum((len(m["content"].encode()) + 3) // 4 for m in handed) + 8  # the template's few tokens
        text = found[-1] if found else "none"
        if handed[0]["content"].startswith("You keep the state"):  # the extractor: the newest figure
            text = json.dumps({"figure": found[-1]} if found else {})
        elif handed[0]["content"].startswith("You keep a running summary"):  # the summarizer: every figure
            text = "Figures: " + ", ".join(found)
        return {"text": text, "id": f"r{len(answers)}", "model": self.model,
                "finish_reason": "stop", "latency_ms": 5.0,
                "usage": {"prompt_tokens": prompt, "completion_tokens": 2, "total_tokens": prompt + 2},
                "cached_tokens": None}

    monkeypatch.setattr("cwabench.producers.llm_summarizer.Client.chat", chat)
    served = {"id": "fake", "root": "weights"}
    monkeypatch.setattr("cwabench2.model.Model.server", lambda self: served if self.mode == "llm" else None)
    extra = f"""
[model]
mode = "llm"
cache = {json.dumps(str(tmp_path / "cache"))}
[s2]
tiers = ["700"]
repeats = 2
[s3]
tiers = ["700"]
repeats = 2
"""
    every = '["cwa-history", "cwa-state", "cwa-state-x", "cwa-memory", "cwa-pipeline", "cwa-format"]'
    path = _configs(tmp_path, spec, ["none"], suites=("S0", "S1", "S2", "S3", "S5"), extra=extra, arms=every,
                    summarizer="llm")
    config = config_mod.load(path)
    first, status = run(config, build=False, log=lambda m: None)
    assert status == "pass" and validate_run(first) == [] and answers
    summary = _read(first / "suites/S2/summary.json")
    arms = {entry["arm"] for entry in summary["by_arm"]}
    assert {"control-full", "control-concat", "concat", "summary", "cwa-state", "cwa-state-x", "cwa-format"} <= arms
    produced = [json.loads(line) for line in (first / "producers/calls.jsonl").read_text().splitlines()]
    assert {r["producer"] for r in produced} == {"extractor", "summarizer"} and all(r["parsed"] for r in produced)
    s3 = _read(first / "suites/S3/summary.json")
    assert {m["id"] for m in s3["metrics"]} >= {"s3.aptitude_p90", "s3.unreliability"}
    grades = [json.loads(line) for line in (first / "suites/S2/grades.jsonl").read_text().splitlines()]
    assert {g["verdict"] for g in grades if g["arm"] == "concat"} >= {"overflow"}
    assert summary["model"]["server"] == served and summary["model"]["concurrency"] == config.model["concurrency"]
    calls = len(answers)

    from dataclasses import replace

    again, status = run(replace(config, model={**config.model, "mode": "replay"}), build=False, log=lambda m: None)
    assert status == "pass" and len(answers) == calls  # replay calls nothing

    def strip(path):  # what differs between any two runs: the run's id and how long a cache read took
        return [{k: v for k, v in json.loads(line).items() if k not in ("run_id", "lookup_ms")}
                for line in path.read_text().splitlines()]

    for path in ("suites/S2/grades.jsonl", "suites/S3/grades.jsonl"):
        assert strip(again / path) == strip(first / path)
    assert _read(again / "suites/S2/summary.json")["model"]["server"] is None  # a replay reaches no server
    # What is written does not depend on how many calls are in flight
    one, status = run(replace(config, model={**config.model, "mode": "replay", "concurrency": 1}), build=False,
                      log=lambda m: None)
    assert status == "pass" and len(answers) == calls
    for path in ("suites/S2/grades.jsonl", "suites/S3/grades.jsonl", "producers/calls.jsonl"):
        assert strip(one / path) == strip(again / path)
    assert all(json.loads(line)["cache_hit"] for line in (again / "producers/calls.jsonl").read_text().splitlines())
    hits = [json.loads(line)["cache_hit"] for line in (again / "model/calls.jsonl").read_text().splitlines()]
    assert hits and all(hits)

    import shutil

    shutil.rmtree(tmp_path / "cache")
    from cwabench2.runner import ProducerError

    with pytest.raises(ProducerError, match="CacheMiss"):  # the producers need the cache before any snapshot
        run(replace(config, model={**config.model, "mode": "replay"}), build=False, log=lambda m: None)
    assert len(answers) == calls
    stub = replace(config, arms=[a for a in config.arms if a != "cwa-state-x"],
                   baseline=replace(config.baseline, summarizer="stub"), s2={**config.s2, "arms": [
                       a for a in config.s2["arms"] if a != "cwa-state-x"]}, suites=["S0", "S1", "S2"])
    missed, status = run(replace(stub, model={**stub.model, "mode": "replay"}), build=False, log=lambda m: None)
    assert status == "fail" and len(answers) == calls
    findings = [json.loads(line) for line in (missed / "findings.jsonl").read_text().splitlines()]
    assert ["replay_miss"] in [f["checks"] for f in findings]
