from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from cwabench import output as d1_output
from cwabench2 import output

ROOT = Path(__file__).resolve().parent.parent
# Kinds Domain 2 reuses unchanged (domain-2-plan.md, 11): Domain 1's schema with the prefix swapped.
COPIES = ("blob", "contract", "finding", "manifest", "run-index", "runs-index", "upstream")
SWAPS = (("cwa-bench-d1", "cwa-bench-d2"),)


@pytest.mark.parametrize("kind", COPIES)
def test_reused_kinds_keep_domain_1s_shape(kind):
    text = (d1_output.SCHEMA_DIR / f"{kind}.v1.schema.json").read_text(encoding="utf-8")
    for old, new in SWAPS:
        text = text.replace(old, new)
    assert json.loads(text) == json.loads((output.SCHEMA_DIR / f"{kind}.v1.schema.json").read_text(encoding="utf-8"))


def test_every_schema_is_valid_and_rejects_strangers():
    paths = sorted(output.SCHEMA_DIR.glob("*.v1.schema.json"))
    assert len(paths) == 12
    for path in paths:
        kind = path.name.removesuffix(".v1.schema.json")
        output.D2.validator(kind)  # check_schema runs here
        with pytest.raises(output.OutputError):
            output.validate({"$schema": output.schema_name(kind), "unexpected": True})


def test_the_contract_pin_is_domain_1s():
    def pin(path):
        return tomllib.loads(path.read_text(encoding="utf-8"))["contract"]["commit"]

    assert pin(ROOT / "domain2.toml") == pin(ROOT.parent / "domain1" / "domain1.toml")


def test_the_upstream_file_validates():
    output.validate(json.loads((ROOT / "findings" / "upstream.json").read_text(encoding="utf-8")))
