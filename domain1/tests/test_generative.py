"""P4: the validity checker, the generator, mutation, the metamorphic transforms and judge, and the minimizer, each
against what it must get right; then S5 end to end through fake adapters, so a crash, a rejected valid snapshot and
mutants are each caught, grouped into one finding per signature, minimized and drafted."""
from __future__ import annotations

import json
import os
import random
from fractions import Fraction

import pytest

from cwabench.adapters import Outcome
from cwabench.canon import digest, instants, spelling, validity
from cwabench.canon.strings import utf16_key
from cwabench.corpora import fuzz
from cwabench.corpora.fuzz import generate as gen
from cwabench.corpora.fuzz import mutate as mut
from cwabench.corpora.fuzz import pools
from cwabench.minimize import Minimizer
from cwabench.oracles import metamorphic as mr
from cwabench.oracles.auditor.model import View
from cwabench.suites.generated import normalized, pointer_shape


@pytest.fixture(scope="module")
def generated(contract):
    return [gen.generate(contract, random.Random(f"test:{i}"), f"t-{i}") for i in range(150)]


# Validity --------------------------------------------------------------------------------------------------------------

def test_every_case_is_valid_and_every_rejection_breaks_exactly_one_check(contract):
    for case in contract.cases:
        assert validity.problems(contract, case.snapshot_bytes) == [], case.id
    for rejection in contract.rejections:
        assert len(validity.broken_checks(contract, rejection.snapshot_bytes)) == 1, rejection.id


def test_instants_round_trip_in_every_style():
    for seconds in (pools.T, pools.T - Fraction(1, 10**12), pools.T + Fraction(123456789, 10**9), Fraction(0),
                    pools.T - 86400 * 400):
        for style in range(pools.STYLES):
            text = pools.instant(seconds, style)
            assert instants.parse(text) == seconds, (text, style)


# Generation and mutation ----------------------------------------------------------------------------------------------

def test_generation_is_deterministic_and_valid(contract, generated):
    again = [gen.generate(contract, random.Random(f"test:{i}"), f"t-{i}") for i in range(150)]
    assert [g.data for g in generated] == [g.data for g in again]
    for g in generated:
        assert validity.problems(contract, g.data) == [], g.case_id


def test_every_feature_intent_builds_a_valid_snapshot(contract):
    for intent in gen.FEATURES + gen.FAULT_INTENTS[::7]:
        for k in range(3):
            g = gen.Gen(contract, random.Random(f"{intent.name}:{k}"), gen.Builder(contract, "t"),
                        used={"gov:base", "q:base"})
            g.b.base()
            try:
                intent.apply(g)
            except gen.NotConstructible:
                continue
            data = json.dumps(gen._finish(g), ensure_ascii=False).encode()
            assert validity.problems(contract, data) == [], intent.name


def test_fuzz_seed_corpus_is_fixed(contract):
    first, second = fuzz.seeds(contract), fuzz.seeds(contract)
    assert len(first) == fuzz.SEEDS
    assert [s.data for s in first] == [s.data for s in second]


def test_every_mutation_operator_breaks_its_own_check_only(contract, generated):
    documents = [json.loads(g.data) for g in generated[:40]]
    for operator in mut.OPERATORS:
        made = [mut.mutate(contract, d, "p", random.Random(i), operator, "m") for i, d in enumerate(documents)]
        made = [m for m in made if m is not None]
        assert len(made) >= len(documents) // 2, operator.name
        for m in made:
            assert validity.broken_checks(contract, m.data) == [operator.check], operator.name


def test_steering_raises_the_weight_of_uncovered_targets():
    from cwabench.corpora.fuzz.steer import Steering

    steering = Steering()
    covered = gen.INTENTS["pipeline:dedupe"]
    before = steering.intent_weight(covered)
    steering.observe(covered.targets * 50)
    assert steering.intent_weight(covered) < before
    assert steering.intent_weight(gen.INTENTS["pipeline:diversity"]) == before


# Metamorphic transforms -----------------------------------------------------------------------------------------------

def _seeds(contract):
    return [s for s in fuzz.seeds(contract)[:60]]


def test_relations_promising_identical_answers_keep_the_digest(contract):
    for s in _seeds(contract):
        for instance in mr.instances(contract, s.data, None, ["MR1", "MR2", "MR4", "MR12"]):
            assert digest.snapshot_digest(json.loads(instance.base)) == digest.snapshot_digest(json.loads(instance.data))


def test_mr5_rewrites_every_timestamp_as_the_same_instant(contract):
    for s in _seeds(contract)[:20]:
        for instance in mr.instances(contract, s.data, None, ["MR5"]):
            a, b = json.loads(instance.base), json.loads(instance.data)
            assert instants.parse(a["assembly_time"]) == instants.parse(b["assembly_time"])
            for x, y in zip(View.build(contract, a).candidates, View.build(contract, b).candidates):
                for key in ("freshness", "expires"):
                    if isinstance(x.get(key), str):
                        assert instants.parse(x.get(key)) == instants.parse(y.get(key))


def test_mr3_mapping_follows_each_unnamed_candidate(contract):
    for s in _seeds(contract)[:20]:
        for instance in mr.instances(contract, s.data, None, ["MR3"]):
            base = View.build(contract, json.loads(instance.base))
            variant = View.build(contract, json.loads(instance.data))
            recorded = {json.dumps(c.item, sort_keys=True): c.recorded_id for c in variant.candidates}
            for c in base.candidates:
                if "#invalid-" in c.recorded_id:
                    assert recorded[json.dumps(c.item, sort_keys=True)] == instance.expect["mapping"][c.recorded_id]


def test_mr10_ids_reverse_utf16_order_and_disagree_with_code_points():
    ids = ["a", "b", "c", "d", "e"]
    mapping = mr._reversing_ids(ids)
    new = [mapping[i] for i in ids]
    assert sorted(new, key=utf16_key) == list(reversed(new))
    assert sorted(new) != sorted(new, key=utf16_key)  # code-point order differs: the trap


def test_number_spellings_are_json_and_the_same_double():
    for value in (0, 7, -3, 9007199254740993, 0.1, -0.0, 5e-324, 1e308, 0.30000000000000004):
        for text in spelling.number_spellings(value):
            assert spelling.JSON_NUMBER.fullmatch(text), text
            assert float(text) == float(value)


def _outcome(kind="assembled", payload=b"p", **trace):
    base = {"refused": {"bool": kind == "refused", "reason": None}, "context": {"snapshot_digest": "d"},
            "excluded": [], "included": [], "result": {"input_tokens": 10, "hash": "h"}}
    base.update(trace)
    return Outcome(kind, payload if kind == "assembled" else None, base)


def test_judge_identical_added_row_margin_and_monotone():
    same = mr.Instance("MR1", "MR1", b"a", b"b", {"compare": "identical"})
    assert mr.judge(None, same, _outcome(), _outcome())[0] == "pass"
    assert mr.judge(None, same, _outcome(), _outcome(payload=b"q"))[0] == "fail"
    assert mr.judge(None, same, _outcome(), Outcome("crashed", problem="boom"))[0] == "fail"
    assert mr.judge(None, same, Outcome("rejected"), _outcome())[0] == "skipped"

    row = {"item_id": "x", "reason": "expired", "stage": "assembler", "slot": "interaction.memory"}
    added = mr.Instance("MR6", "MR6:expired", b"a", b"b", {"compare": "added-row", "row": row})
    variant = _outcome(excluded=[row], context={"snapshot_digest": "other"})
    assert mr.judge(None, added, _outcome(), variant)[0] == "pass"
    assert mr.judge(None, added, _outcome(), _outcome())[0] == "fail"

    reduce = mr.Instance("MR14", "MR14:reduce", b"a", b"b", {"compare": "margin", "expect": "reduce", "margin": 9})
    assert mr.judge(None, reduce, _outcome(), _outcome())[0] == "fail"
    assert mr.judge(None, reduce, _outcome(), _outcome("refused"))[0] == "pass"

    monotone = mr.Instance("MR13", "MR13", b"a", b"b", {"compare": "monotone"})
    assert mr.judge(None, monotone, _outcome(), _outcome("refused"))[0] == "triage"


def test_signatures_ignore_ids_numbers_and_row_positions():
    assert normalized("'kb:1' excluded 3 times") == normalized("'mem:9' excluded 12 times")
    assert normalized("snapshot_digest 2698ff96b32be162… is not d6e5aa85cad18e3d…") == \
        normalized("snapshot_digest a58bea48b57b9d1e… is not 850b0abbc07e5c14…")
    assert pointer_shape("/defaults_filled/0/item_id") == pointer_shape("/defaults_filled/7")


# Minimizer -------------------------------------------------------------------------------------------------------------

def test_minimizer_keeps_the_failure_and_validity(contract, generated):
    source = next(g for g in generated if b"\\u0000" in g.data)

    def nul(document):
        data = json.dumps(document, ensure_ascii=False).encode()
        return validity.is_valid(contract, data) and any(
            isinstance(i, dict) and "\x00" in str(i.get("body")) for b in document["batches"] for i in b["items"])

    result = Minimizer(nul, 400).run(json.loads(source.data))
    assert nul(result.document)
    assert result.after["items"] == 1 and result.after["batches"] == 1
    assert not result.exhausted

    tight = Minimizer(nul, 3).run(json.loads(source.data))
    assert tight.exhausted and nul(tight.document)


# S5 end to end ---------------------------------------------------------------------------------------------------------

def test_s5_catches_crashes_rejected_valid_snapshots_and_minimizes_each(fake_config):
    from cwabench.runner import run
    from cwabench.validate import validate_run

    config = fake_config("reject-all", second="crash-on-nul", suites=("S5",),
                         extra="\n[s5]\nvalid = 60\nmutated = 20\nround = 30\nmax_tests = 80\n")
    run_dir, status = run(config, build=False, log=lambda _: None)
    assert status == "fail"
    assert validate_run(run_dir) == []
    findings = [json.loads(line) for line in (run_dir / "suites/S5/findings.jsonl").read_text().splitlines()]
    kinds = {(f["oracle"], tuple(f["checks"]), f["adapter"]) for f in findings}
    assert ("expected", ("valid-rejected",), "fake") in kinds
    assert ("fault", ("crashed",), "fake2") in kinds
    assert all(f["minimized"] and f["minimized"]["reproduces"] for f in findings)
    crash = next(f for f in findings if f["oracle"] == "fault")
    assert crash["minimized"]["items_after"] == 1
    assert (run_dir / crash["minimized"]["path"] / "snapshot.json").is_file()
    summary = json.loads((run_dir / "suites/S5/summary.json").read_text())
    rejected = {m["adapter"]: m["value"] for m in summary["metrics"] if m["id"] == "s5.mutants_rejected"}
    assert rejected == {"fake": 1.0, "fake2": 1.0}
    assert summary["generated"]["steering"]["rounds"][1]["covered_before"] >= 0
    assert (run_dir / "corpora/fuzz.valid/index.json").is_file()


# S4 end to end (opt-in: needs the reference assembler) ------------------------------------------------------------------

reference = pytest.mark.skipif(os.environ.get("CWA_BENCH_REFERENCE") != "1",
                               reason="set CWA_BENCH_REFERENCE=1 to run S4 on the reference assembler with a "
                                      "deliberate defect (needs `cwabench setup`)")


@reference
def test_s4_catches_a_defect_each_relation_targets_and_minimizes_it(tmp_path, spec):
    from pathlib import Path

    from cwabench import config as config_mod
    from cwabench import gitinfo
    from cwabench.runner import run

    root = Path(__file__).resolve().parent.parent
    python = root / ".build/python-venv/bin/python"
    adapters = "".join(f"""
[adapters.{mode.replace('-', '_')}]
language = "Python"
checkout = {json.dumps(str(root / "../../../assembler-python"))}
command = [{json.dumps(str(python))}, {json.dumps(str(root / "tests/buggy_adapter.py"))}]
env = {{ CWA_BUGGY_MODE = {json.dumps(mode)} }}
""" for mode in ("batch-order", "float-crash"))
    path = tmp_path / "s4.toml"
    path.write_text(f"""
[contract]
path = {json.dumps(str(spec))}
commit = {json.dumps(gitinfo.inspect(spec).commit)}
allow_dirty = true
[run]
suites = ["S4"]
adapters = ["batch_order", "float_crash"]
results_dir = {json.dumps(str(tmp_path / "results"))}
{adapters}
[container]
enabled = false
[s4]
corpus = ["fuzz.seeds"]
seeds_per_corpus = 12
relations = ["MR1", "MR4"]
reference = "batch_order"
""", encoding="utf-8")
    run_dir, status = run(config_mod.load(path), build=False, log=lambda _: None)
    assert status == "fail"
    findings = [json.loads(line) for line in (run_dir / "suites/S4/findings.jsonl").read_text().splitlines()]
    relation = {(f["adapter"], f["checks"][0]) for f in findings if f["oracle"] == "metamorphic"}
    assert ("batch_order", "MR1") in relation
    assert ("float_crash", "MR4:numbers") in relation or ("float_crash", "MR4:integer-spelling") in relation
    mr1 = next(f for f in findings if f["oracle"] == "metamorphic" and f["checks"] == ["MR1"])
    assert mr1["minimized"]["reproduces"]
    draft = run_dir / mr1["minimized"]["path"]
    assert len(json.loads((draft / "base.snapshot.json").read_text())["batches"]) == 2  # the least MR1 can permute
