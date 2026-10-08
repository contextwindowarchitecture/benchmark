"""The real container, end to end. Slow and needs Podman, so it runs only with CWA_BENCH_CONTAINER=1."""
from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import pytest

from cwabench import config as config_mod
from cwabench import container, corpora
from cwabench.contract import Contract

pytestmark = pytest.mark.skipif(os.environ.get("CWA_BENCH_CONTAINER") != "1",
                                reason="set CWA_BENCH_CONTAINER=1 to build and run the container")


def test_isolated_profile_runs_every_adapter_without_network():
    config = config_mod.load(Path(__file__).resolve().parent.parent / "domain1.toml")
    contract = Contract(config.contract_path, config.contract_commit, config.allow_dirty)
    corpus = corpora.load(contract, ["conformance"])[:2]
    image = container.build_image(config, lambda _: None)
    answers, _, versions = container.run_profile(config, image, container.PROFILES["linux-isolated"], corpus,
                                                 list(config.adapters), {}, lambda _: None)
    assert set(versions) >= {"go", "rust", "node", "python", "strace"}
    assert Counter(a.adapter for a in answers) == {name: 2 * len(corpus) for name in config.adapters}
    assert all(a.outcome.kind in ("assembled", "refused") for a in answers)
    traced = [container.parse_strace(a.strace.read_text()) for a in answers if a.strace]
    assert traced and all(r["calls"] > 0 and r["network"] == [] for r in traced)
