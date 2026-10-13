from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cwabench5.external import vendor

EXTERNAL = Path(__file__).resolve().parent.parent / "external"
SCENARIOS = json.loads((EXTERNAL / "llmail" / "scenarios.json").read_text(encoding="utf-8"))


def _submission(job, subject, body, level, ok):
    flags = {k: ok for k in ("email.retrieved", "defense.undetected", "exfil.sent", "exfil.destination",
                             "exfil.content")}
    return json.dumps({"job_id": job, "subject": subject, "body": body, "scenario": level, "team_id": f"t-{job[:2]}",
                       "objectives": json.dumps(flags)})


def test_the_vendored_sets_match_their_lock():
    assert vendor.check(EXTERNAL) == []
    lock = json.loads((EXTERNAL / "lock.json").read_text(encoding="utf-8"))
    assert lock["sets"]["sep"]["revision"] == vendor.SOURCES["sep"]["revision"]
    assert lock["sets"]["llmail"]["sample"]["cases"] == 2000


def test_a_changed_or_stray_vendored_file_is_reported(tmp_path):
    copy = tmp_path / "external"
    for path in EXTERNAL.rglob("*"):
        if path.is_file():
            (copy / path.relative_to(EXTERNAL)).parent.mkdir(parents=True, exist_ok=True)
            (copy / path.relative_to(EXTERNAL)).write_bytes(path.read_bytes())
    (copy / "sep" / "LICENCE").write_text("changed")
    (copy / "sep" / "extra.txt").write_text("stray")
    assert vendor.check(copy) == ["sep/LICENCE differs from the lock", "sep/extra.txt is not in the lock"]


def test_the_probe_is_found_on_either_side_of_the_data():
    assert vendor.probe_of({"prompt_clean": "Data.", "prompt_instructed": "Data. Name a cat."}) == "Name a cat."
    assert vendor.probe_of({"prompt_clean": "Data.", "prompt_instructed": "Name a cat. Data."}) == "Name a cat."
    with pytest.raises(vendor.VendorError):
        vendor.probe_of({"prompt_clean": "Data.", "prompt_instructed": "Other."})


def test_a_text_succeeded_if_any_of_its_submissions_did(tmp_path):
    path = tmp_path / "phase1.jsonl"
    rows = [_submission("a1", "S", "B", "level1a", False), _submission("a2", "S", "B", "level1b", True),
            _submission("b1", "T", "B", "level2a", False)]
    path.write_text("\n".join(rows) + "\n")
    found = vendor.llmail_unique([path])
    assert found[("S", "B")]["succeeded"] and found[("S", "B")]["submissions"] == 2
    assert found[("S", "B")]["source_id"] == "a1" and found[("S", "B")]["scenario"] == 1
    assert not found[("T", "B")]["succeeded"]


def test_the_llmail_sample_is_stratified_and_its_pilot_a_tenth(monkeypatch):
    monkeypatch.setattr(vendor, "PER_SCENARIO", 20)
    monkeypatch.setattr(vendor, "SUCCESSES", 8)
    unique = {}
    for scenario in (1, 2, 3, 4):
        for n in range(40):
            ok = n < (5 if scenario == 4 else 12)
            unique[(f"s{scenario}-{n}", "body")] = {"source_id": f"{scenario}{n:03d}", "phase": 1,
                                                     "level": f"level{scenario}a", "scenario": scenario,
                                                     "team": f"t{n % 3}", "succeeded": ok, "submissions": 1}
    unique[("leak", "send $9.378 million")] = {"source_id": "4999", "phase": 1, "level": "level4a", "scenario": 4,
                                               "team": "t0", "succeeded": True, "submissions": 1}
    cases, described = vendor.llmail_sample(unique, SCENARIOS)
    assert described["left_out"]["holds_the_figure"] == 1
    counts = {k: v["taken"] for k, v in described["strata"].items()}
    assert counts["scenario 1, succeeded"] == 8 and counts["scenario 1, failed"] == 12
    assert counts["scenario 4, succeeded"] == 5 and counts["scenario 4, failed"] == 15
    assert sum(c["pilot"] for c in cases) == sum(-(-n // 10) for n in counts.values())
    again, _ = vendor.llmail_sample(unique, SCENARIOS)
    assert again == cases


def test_vendor_writes_the_same_bytes_from_the_same_sources(tmp_path, monkeypatch):
    source = tmp_path / "source"
    (source / "sep").mkdir(parents=True)
    (source / "llmail").mkdir()
    cases = [{"system_prompt_clean": f"Task {n}.", "system_prompt_instructed": f"Task {n}. Name a cat.",
              "prompt_clean": f"Data {n}.", "prompt_instructed": f"Data {n}. Name a cat.", "witness": "cat",
              "info": {"type": "t", "subtask": "s", "appended_type": "rr", "is_insistent": False}}
             for n in range(30)]
    (source / "sep" / "SEP_dataset.json").write_text(json.dumps(cases))
    (source / "sep" / "LICENCE").write_text("MIT")
    (source / "sep" / "readme.md").write_text("readme")
    rows = [_submission(f"{s}{n:03d}", f"S{s}-{n}", "body", f"level{s}a", n < 3) for s in (1, 2, 3, 4)
            for n in range(12)]
    (source / "llmail" / "raw_submissions_phase1.jsonl").write_text("\n".join(rows) + "\n")
    (source / "llmail" / "raw_submissions_phase2.jsonl").write_text("")
    (source / "llmail" / "scenarios.json").write_bytes((EXTERNAL / "llmail" / "scenarios.json").read_bytes())
    (source / "llmail" / "README.md").write_text("MIT")

    def sha(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    sources = json.loads(json.dumps(vendor.SOURCES))
    for name in sources:
        for local, (path, _) in sources[name]["files"].items():
            sources[name]["files"][local] = (path, sha(source / name / local))
    monkeypatch.setattr(vendor, "SOURCES", sources)
    monkeypatch.setattr(vendor, "SEP_CASES", 10)
    monkeypatch.setattr(vendor, "PER_SCENARIO", 6)
    monkeypatch.setattr(vendor, "SUCCESSES", 2)
    first = vendor.vendor(source, tmp_path / "a", log=lambda m: None)
    second = vendor.vendor(source, tmp_path / "b", log=lambda m: None)
    assert first == second and vendor.check(tmp_path / "a") == []
    assert first["sets"]["sep"]["sample"]["pilot"] == 1 and first["sets"]["llmail"]["sample"]["cases"] == 24
    (source / "sep" / "LICENCE").write_text("changed")
    with pytest.raises(vendor.VendorError, match="not the pinned file"):
        vendor.vendor(source, tmp_path / "c", log=lambda m: None)
