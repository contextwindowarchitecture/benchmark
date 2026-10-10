"""The real container, end to end, once in each engine that runs here (Podman, Docker). Slow, so it runs only with
CWA_BENCH_CONTAINER=1."""
from __future__ import annotations

import os
import shutil
import subprocess
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from cwabench import config as config_mod
from cwabench import container, corpora
from cwabench.contract import Contract

pytestmark = pytest.mark.skipif(os.environ.get("CWA_BENCH_CONTAINER") != "1",
                                reason="set CWA_BENCH_CONTAINER=1 to build and run the container")


def _runs(engine: str) -> bool:
    path = shutil.which(engine)
    return path is not None and subprocess.run([path, "info"], capture_output=True, timeout=60).returncode == 0


@pytest.mark.parametrize("engine", container.ENGINES)
def test_isolated_profile_runs_every_adapter_without_network(engine):
    if not _runs(engine):
        pytest.skip(f"{engine} is not installed here, or cannot run a container")
    config = config_mod.load(Path(__file__).resolve().parent.parent / "domain1.toml")
    config = replace(config, settings={**config.settings, "container": {**config.section("container"),
                                                                         "engine": engine}})
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
