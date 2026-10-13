from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cwabench import gitinfo

DOMAIN1 = Path(__file__).resolve().parent.parent.parent / "domain1"

reference = pytest.mark.skipif(os.environ.get("CWA_BENCH_REFERENCE") != "1",
                               reason="set CWA_BENCH_REFERENCE=1 to run S1 on the reference assembler (needs "
                                      "`cwabench setup`)")


def _configs(tmp_path, spec, modes, suites=("S0", "S1", "S7")):
    """A Domain 1 configuration whose adapters are the reference assembler in each buggy_adapter mode, and a
    Domain 5 configuration that assembles with them over two small task families."""
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
    d5 = tmp_path / "d5.toml"
    d5.write_text(f"""
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
margin_percent = 35
[grid]
html_templates = 2
[s7]
goldens = {json.dumps(str(tmp_path / "goldens.json"))}
[tasks.em]
seed = 1
sizes = {{ pilot = 1, recorded = 1 }}
[tasks.dq]
seed = 2
sizes = {{ pilot = 1, recorded = 1 }}
[attacks]
seed = 7
families = ["hj", "ac"]
""", encoding="utf-8")
    return d5


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


@reference
def test_s1_passes_on_the_reference_and_s7_adopts_its_goldens(tmp_path, spec):
    from cwabench.validate import validate_run
    from cwabench5 import config as config_mod
    from cwabench5.runner import run
    from cwabench5.suites.s7_goldens import accept

    config = config_mod.load(_configs(tmp_path, spec, ["none"]))
    run_dir, status = run(config, build=False, log=lambda m: None)
    assert status == "pass", [f["summary"] for f in _rows(run_dir / "findings.jsonl")]
    assert validate_run(run_dir) == []
    summary = json.loads((run_dir / "suites/S1/summary.json").read_text())
    assert summary["gate"]["passed"] == summary["gate"]["total"] > 0
    gate = _rows(run_dir / "suites/S1/gate.jsonl")
    assert {r["arm"] for r in gate} == {"cwa", "cwa-boundary", "cwa-reinforced", "cwa-observation"}
    assert all(r["structure"]["ok"] and r["verdict"] == "passed" for r in gate)
    assert {r["kind"] for r in gate} == {"attacked", "clean", "html"}
    claims = _rows(run_dir / "suites/S1/claims.jsonl")
    assert len(claims) == 2 * 8 and all(r["verdict"] == "passed" for r in claims)
    assert all(not r["truncation"]["system_kept"] for r in claims if r["claim"] == "flood")
    baselines = _rows(run_dir / "suites/S1/baselines.jsonl")
    forged = {(r["arm"], r["carrier"]) for r in baselines if r["forged"]}
    assert ("delimited", "tag") in forged and ("xml-unescaped", "tag") in forged
    assert all(c == "tag" for _, c in forged)
    target, count = accept(config, run_dir)
    assert count == len(gate) and target.is_file()
    again, status = run(config, build=False, log=lambda m: None)
    s7 = json.loads((again / "suites/S7/summary.json").read_text())
    assert status == "pass" and s7["goldens"]["drift"]["match"] == len(gate)


@reference
def test_s1_fails_an_assembler_that_cuts_the_instructions(tmp_path, spec):
    from cwabench5 import config as config_mod
    from cwabench5.runner import run

    config = config_mod.load(_configs(tmp_path, spec, ["none", "truncate-protected"], suites=("S1",)))
    run_dir, status = run(config, build=False, log=lambda m: None)
    assert status == "fail"
    found = {(f["oracle"], f["adapter"]) for f in _rows(run_dir / "findings.jsonl")}
    assert ("expected", "truncate_protected") in found and ("differential", None) in found
