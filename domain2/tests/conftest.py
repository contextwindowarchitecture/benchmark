from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cwabench import gitinfo

ROOT = Path(__file__).resolve().parent.parent
SPEC = Path(os.environ.get("CWA_SPEC", ROOT / "../../../contextwindowarchitecture")).resolve()

FAMILIES = """
[families.vt]
seed = 11
sizes = { pilot = 1, recorded = 2 }
variables = 2
distractors = 2
assignment_density = 0.5
filler_sentences = 1
reply_sentences = 1

[families.fr]
seed = 12
sizes = { pilot = 2, recorded = 2 }
tasks = ["record", "compute"]
fields = 4
steps = 4
filler_sentences = 1
reply_sentences = 1
"""


@pytest.fixture(scope="session")
def spec() -> Path:
    if not (SPEC / "SPEC.md").is_file():
        pytest.skip(f"no specification checkout at {SPEC} (set CWA_SPEC)")
    return SPEC


@pytest.fixture
def small_config(tmp_path: Path, spec: Path):
    """A config at the spec checkout's commit, with small families, writing under tmp_path."""
    from cwabench2 import config as config_mod

    def make(extra: str = "", families: str = FAMILIES) -> config_mod.Config:
        text = f"""
[contract]
path = {json.dumps(str(spec))}
commit = {json.dumps(gitinfo.inspect(spec).commit)}
allow_dirty = true

[run]
suites = ["S0"]
results_dir = {json.dumps(str(tmp_path / "results"))}

[turns]
counts = [10, 25]
checkpoint_every = 10
{families}
{extra}"""
        path = tmp_path / "domain2.toml"
        path.write_text(text, encoding="utf-8")
        return config_mod.load(path)

    return make
