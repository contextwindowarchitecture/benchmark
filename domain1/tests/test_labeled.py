"""The labeled generators: deterministic, schema-valid, complete labels, and (opt-in) labels that the reference
assembler reproduces and that reject real answers with a decision changed."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from cwabench.corpora import LABELED, load
from cwabench.oracles.auditor.model import View

NAMES = [f"labeled.{n}" for n in LABELED]
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def corpus(contract):
    return {name: load(contract, [name]) for name in NAMES}


def test_generators_are_deterministic(contract, corpus):
    again = {name: load(contract, [name]) for name in NAMES}
    for name in NAMES:
        assert [s.data for s in corpus[name]] == [s.data for s in again[name]], name
        assert [s.label for s in corpus[name]] == [s.label for s in again[name]], name


def test_case_ids_are_unique(corpus):
    ids = [(s.corpus, s.case_id) for name in NAMES for s in corpus[name]]
    assert len(ids) == len(set(ids))


def test_every_snapshot_is_schema_valid(contract, corpus):
    validator = contract.validator("snapshot.schema.json")
    for name in NAMES:
        for snapshot in corpus[name]:
            document = json.loads(snapshot.data)
            errors = list(validator.iter_errors(document))
            assert not errors, (snapshot.case_id, errors[0].message[:200])


def test_every_candidate_has_a_fate(contract, corpus):
    for name in NAMES:
        for snapshot in corpus[name]:
            view = View.build(contract, json.loads(snapshot.data))
            assert set(view.by_id) == set(snapshot.label["fates"]), snapshot.case_id
            for item_id, fates in snapshot.label["fates"].items():
                assert len(fates if isinstance(fates, list) else [fates]) == len(view.by_id[item_id]), snapshot.case_id


def test_refusal_labels_cover_every_condition_and_combination(corpus):
    refusals = {s.label["refusal"] for s in corpus["labeled.refusal"]}
    assert refusals == {"required_slot_missing", "protected_slot_unplaced", "conflict_unresolved",
                        "protected_content_over_budget", "slot_floor_over_budget", "evidence_required"}
    assert len(corpus["labeled.refusal"]) == 63


def test_degradation_flips_exactly_below_min_included(corpus):
    for snapshot in corpus["labeled.degradation"]:
        if snapshot.case_id.startswith("no-min-included"):
            continue
        k = int(snapshot.case_id.rsplit("-k", 1)[1])
        assert (snapshot.label["outcome"] == "refused") == (4 - k < 3), snapshot.case_id


reference = pytest.mark.skipif(os.environ.get("CWA_BENCH_REFERENCE") != "1",
                               reason="set CWA_BENCH_REFERENCE=1 to run the labeled corpora through the reference "
                                      "assembler (needs `cwabench setup`)")


@reference
def test_reference_assembler_reproduces_every_label_and_labels_reject_changed_decisions(contract, corpus):
    """Section 11 of the plan: a generator is trusted only once the reference assembler reproduces its labels.
    Then every decision-changing mutation of those real answers must be rejected by the label oracle."""
    from concurrent.futures import ThreadPoolExecutor

    from cwabench import adapters, config
    from cwabench.adapters import Outcome
    from cwabench.oracles.label import judge
    from cwabench.oracles.mutants import Mutator

    cfg = config.load(ROOT / "domain1.toml")
    python = adapters.setup(cfg, cfg.adapters["python"], build=False)
    snapshots = [s for name in NAMES for s in corpus[name]]

    def answer(snapshot):
        return snapshot, adapters.classify(adapters.invoke(python, snapshot.data, 60, ROOT))

    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(answer, snapshots))
    wrong = [s.case_id for s, o in answers if any(c["status"] == "fail" for c in judge(s.label, o))]
    assert wrong == []

    decision_operators = {"drop_excluded_row", "duplicate_excluded_row", "change_reason", "retarget_duplicate_or_superseded",
                          "ghost_excluded_row", "drop_included_row", "move_included_to_excluded", "revive_droppable",
                          "drop_compressed_row", "compressed_variant", "refused_flip", "refusal_reason", "recovery",
                          "conflict_record"}
    total = rejected = 0
    survivors = []
    for snapshot, outcome in answers[::3]:
        mutator = Mutator(contract, json.loads(snapshot.data))
        for mutant in mutator.mutants(outcome.trace, outcome.payload):
            if mutant.operator not in decision_operators:
                continue
            match = re.match(r"^excluded/(\d+)", mutant.position)
            if match:
                index = int(match.group(1))
                if index < len(outcome.trace["excluded"]) and outcome.trace["excluded"][index]["stage"] == "producer":
                    continue  # producer rows are the producer's report, not a decision; the auditor (A2) checks them
            refused = bool(mutant.trace.get("refused", {}).get("bool"))
            changed = Outcome("refused" if refused else "assembled", payload=mutant.payload, trace=mutant.trace)
            total += 1
            if any(c["status"] == "fail" for c in judge(snapshot.label, changed)):
                rejected += 1
            else:
                survivors.append((snapshot.case_id, mutant.operator, mutant.position))
    print(f"\nlabel oracle rejected {rejected}/{total} decision mutations; survivors {survivors[:20]}")
    assert total > 500
    assert rejected == total, (rejected, total, survivors[:10])
