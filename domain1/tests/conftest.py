from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from cwabench import config as config_mod
from cwabench import gitinfo
from cwabench.contract import Contract

ROOT = Path(__file__).resolve().parent.parent
SPEC = Path(os.environ.get("CWA_SPEC", ROOT / "../../../contextwindowarchitecture")).resolve()
FAKE = Path(__file__).resolve().parent / "fake_adapter.py"


@pytest.fixture(scope="session")
def spec() -> Path:
    if not (SPEC / "SPEC.md").is_file():
        pytest.skip(f"no specification checkout at {SPEC} (set CWA_SPEC)")
    return SPEC


@pytest.fixture(scope="session")
def contract(spec: Path) -> Contract:
    return Contract(spec, gitinfo.inspect(spec).commit, allow_dirty=True)


@pytest.fixture
def fake_config(tmp_path: Path, spec: Path):
    """A config whose only adapter is the fake one, in the given mode."""

    def make(mode: str, timeout_s: float = 20, concurrency: int = 8, second: str | None = None,
             suites=("S1",), extra: str = "") -> config_mod.Config:
        """One fake adapter in `mode`; with `second`, another fake adapter in that mode, for differential tests.
        `extra` is appended to the config, for suite settings."""
        commit = gitinfo.inspect(spec).commit
        modes = {"fake": mode} if second is None else {"fake": mode, "fake2": second}
        tables = "".join(f"""
[adapters.{name}]
language = "Python"
checkout = {json.dumps(str(FAKE.parent))}
command = [{json.dumps(sys.executable)}, {json.dumps(str(FAKE))}]
env = {{ CWA_FAKE_CORPUS = {json.dumps(str(spec / "conformance"))}, CWA_FAKE_MODE = {json.dumps(m)} }}
""" for name, m in modes.items())
        text = f"""
[contract]
path = {json.dumps(str(spec))}
commit = {json.dumps(commit)}
allow_dirty = true

[run]
suites = {json.dumps(list(suites))}
adapters = {json.dumps(list(modes))}
timeout_s = {timeout_s}
concurrency = {concurrency}
results_dir = {json.dumps(str(tmp_path / "results"))}
{tables}
[container]
enabled = false

[s12]
goldens = {json.dumps(str(tmp_path / "goldens.json"))}
{extra}"""
        path = tmp_path / f"{mode}-{second}-{'-'.join(suites)}.toml"
        path.write_text(text, encoding="utf-8")
        return config_mod.load(path)

    return make


def read_rows(run_dir: Path) -> list[dict]:
    lines = (run_dir / "suites" / "S1" / "results.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]
