from __future__ import annotations

import json

import pytest

from cwabench import gitinfo
from cwabench.contract import Contract
from cwabench5 import tasks
from cwabench5.application import claims
from cwabench5.application.snapshot import Settings, freeze
from cwabench5.oracles.tokens import Tokenizer

SETTINGS = Settings(margin_percent=35)


@pytest.fixture(scope="module")
def contract(spec):
    return Contract(spec, gitinfo.inspect(spec).commit, True)


@pytest.fixture(scope="module")
def instances():
    return {f: [tasks.generate(f, 7, v, 0, {}) for v in tasks.VARIANTS] for f in tasks.FAMILIES}


@pytest.mark.parametrize("name", [*claims.CLAIMS, "flood"])
def test_every_claim_is_a_valid_snapshot_with_one_added_item(contract, instances, name):
    task = instances["sl"][0]
    frozen = freeze(contract, task, task["items"], "cwa", SETTINGS)
    made = claims.flood(contract, frozen, claims.INSTRUCTION, SETTINGS.tokenizer) if name == "flood" else \
        claims.claim(contract, frozen, name)
    snapshot = json.loads(made.data)
    assert not list(contract.validator("snapshot.schema.json").iter_errors(snapshot))
    ids = [i["id"] for b in snapshot["batches"] for i in b["items"]]
    assert len(ids) == len(frozen.items) + 1 and made.item_id in ids
    assert snapshot["budget"]["input"] == frozen.full


def test_a_front_truncation_drops_the_system_prompt_under_a_flood(contract, instances):
    task = instances["dq"][0]
    frozen = freeze(contract, task, task["items"], "cwa", SETTINGS)
    made = claims.flood(contract, frozen, claims.INSTRUCTION, SETTINGS.tokenizer)
    system = f"{task['instructions']}\n\n{task['output_contract']}"
    assert not claims.truncated(system, made.body, frozen.full, SETTINGS.tokenizer)


def test_the_tokenizer_check_reads_nothing_without_a_cache(small_config):
    from dataclasses import replace

    config = replace(small_config(), tokenize="replay")
    assert Tokenizer(config).marker_ids("qwen") is None
