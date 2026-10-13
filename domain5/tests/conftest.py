from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cwabench import gitinfo

ROOT = Path(__file__).resolve().parent.parent
SPEC = Path(os.environ.get("CWA_SPEC", ROOT / "../../../contextwindowarchitecture")).resolve()

TASKS = """
[tasks.em]
seed = 1
sizes = { pilot = 1, recorded = 2 }

[tasks.cq]
seed = 2
sizes = { pilot = 1, recorded = 2 }

[tasks.sl]
seed = 3
sizes = { pilot = 1, recorded = 2 }
"""


@pytest.fixture(scope="session")
def spec() -> Path:
    if not (SPEC / "SPEC.md").is_file():
        pytest.skip(f"no specification checkout at {SPEC} (set CWA_SPEC)")
    return SPEC


@pytest.fixture
def small_config(tmp_path: Path, spec: Path):
    """A config at the spec checkout's commit, with small task families, writing under tmp_path."""
    from cwabench5 import config as config_mod

    def make(extra: str = "", families: str = TASKS) -> config_mod.Config:
        text = f"""
[contract]
path = {json.dumps(str(spec))}
commit = {json.dumps(gitinfo.inspect(spec).commit)}
allow_dirty = true

[run]
suites = ["S0"]
results_dir = {json.dumps(str(tmp_path / "results"))}
{families}
[attacks]
seed = 7
{extra}"""
        path = tmp_path / "domain5.toml"
        path.write_text(text, encoding="utf-8")
        return config_mod.load(path)

    return make
