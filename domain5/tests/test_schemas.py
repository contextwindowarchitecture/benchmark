from __future__ import annotations

import json
from pathlib import Path

import pytest

from cwabench2 import output as d2_output
from cwabench5 import output

ROOT = Path(__file__).resolve().parent.parent
# Kinds Domain 5 reuses unchanged (domain-5-plan.md, 11): Domain 2's schema with the prefix swapped, and the plan its
# descriptions cite (arms are section 7 in both plans).
COPIES = ("blob", "call-row", "contract", "drift-row", "finding", "manifest", "run-index", "runs-index", "summary",
          "upstream")
SWAPS = (("cwa-bench-d2", "cwa-bench-d5"), ("domain-2-plan.md", "domain-5-plan.md"))


@pytest.mark.parametrize("kind", COPIES)
def test_reused_kinds_keep_domain_2s_shape(kind):
    text = (d2_output.SCHEMA_DIR / f"{kind}.v1.schema.json").read_text(encoding="utf-8")
    for old, new in SWAPS:
        text = text.replace(old, new)
    assert json.loads(text) == json.loads((output.SCHEMA_DIR / f"{kind}.v1.schema.json").read_text(encoding="utf-8"))


def test_every_schema_is_valid_and_rejects_strangers():
    paths = sorted(output.SCHEMA_DIR.glob("*.v1.schema.json"))
    assert {p.name.removesuffix(".v1.schema.json") for p in paths} >= set(COPIES)
    for path in paths:
        kind = path.name.removesuffix(".v1.schema.json")
        output.D5.validator(kind)  # check_schema runs here
        with pytest.raises(output.OutputError):
            output.validate({"$schema": output.schema_name(kind), "unexpected": True})


def test_the_upstream_file_validates():
    output.validate(json.loads((ROOT / "findings" / "upstream.json").read_text(encoding="utf-8")))
