"""`cwabench fixture`: a run trimmed to a sample that still validates, with the blobs its files name and no others."""
from __future__ import annotations

import json
import re

from cwabench import fixture
from cwabench.runner import run
from cwabench.validate import validate_run


def test_sample_keeps_the_ends_and_spreads_the_rest():
    assert fixture.sample(list(range(10)), 4) == [0, 3, 6, 9]
    assert fixture.sample(list(range(3)), 5) == [0, 1, 2]
    assert fixture.sample(list(range(10)), 1) == [0]
    assert fixture.sample([], 3) == [] and fixture.sample([1, 2], 0) == []


def test_fixture_is_a_valid_trimmed_run(fake_config, tmp_path):
    run_dir, status = run(fake_config("flip-payload"), build=False, log=lambda _: None)  # a run with findings
    assert status == "fail"
    first = json.loads((run_dir / "findings.jsonl").read_text(encoding="utf-8").splitlines()[0])["finding_id"]
    link = {"url": "https://github.com/contextwindowarchitecture/assembler-python/issues/1", "state": "open"}
    target, problems = fixture.write_fixture(run_dir, tmp_path / "fixtures", rows=3, upstream={first: link})
    assert problems == [] and target == tmp_path / "fixtures" / run_dir.name

    index = json.loads((target / "index.json").read_text(encoding="utf-8"))
    files = {f["path"]: f for f in index["files"]}
    assert index["fixture"]["of"] == run_dir.name and index["fixture"]["rows"] == 3
    rows = (target / "suites/S1/results.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(rows) == 3 == files["suites/S1/results.jsonl"]["rows"]
    findings = [json.loads(line) for line in (target / "findings.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(findings) == files["findings.jsonl"]["rows"] >= 1  # findings are never cut
    assert findings[0]["upstream"] == link

    named = set()
    for entry in index["files"]:
        if entry["path"] != index["blobs"]["index"]:
            named |= set(re.findall(r"sha256:[0-9a-f]{64}",
                                    (target / entry["path"]).read_text(encoding="utf-8", errors="replace")))
    blobs = {json.loads(line)["digest"]
             for line in (target / index["blobs"]["index"]).read_text(encoding="utf-8").splitlines()}
    assert blobs == named and index["blobs"]["count"] == len(blobs) == files[index["blobs"]["index"]]["rows"]
    assert validate_run(target) == []

    runs = json.loads((tmp_path / "fixtures" / "index.json").read_text(encoding="utf-8"))
    assert [r["run_id"] for r in runs["runs"]] == [run_dir.name]
    assert not (tmp_path / "fixtures" / "latest").exists()
