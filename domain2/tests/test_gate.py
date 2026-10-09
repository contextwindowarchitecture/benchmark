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


def _configs(tmp_path, spec, modes):
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
suites = ["S0", "S1", "S7"]
results_dir = {json.dumps(str(tmp_path / "results"))}
concurrency = 4
[adapters]
config = {json.dumps(str(d1))}
use = {json.dumps(names)}
payload_source = {json.dumps(names[0])}
[budgets]
input = [700]
ratios = [1.0, 0.4]
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
""", encoding="utf-8")
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
    assert summary["gate"]["passed"] == summary["gate"]["total"] == 3 * 4
    rows = [json.loads(line) for line in (run_dir / "suites/S1/turns.jsonl").read_text().splitlines()]
    assert {r["point"] for r in rows} == {"turn", "probe"} and all(r["verdict"] == "passed" for r in rows)
    probes = [r for r in rows if r["point"] == "probe"]
    assert all(r["fact"]["by_trace"] == r["fact"]["by_text"] for r in probes)
    assert any(not r["fact"]["present"] for r in probes if r["arm"] == "cwa-history" and r["tier"] == "700")
    assert summary["rows"]["timelines"] == 3 * 4 * 3
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
