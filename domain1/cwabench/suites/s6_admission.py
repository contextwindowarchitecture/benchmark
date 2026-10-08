"""S6 · Admission and pipeline (domain-1-plan.md, 7.6). Every admission code in every slot it applies to, the
admitted side of every boundary, the precedence of every pair of codes one item can fail together, and the order of
conflict resolution, supersession, deduplication and the source-diversity cap."""
from __future__ import annotations

from collections import Counter

from ..corpora.labeled import precedence
from . import SuiteContext, SuiteResult
from .labeled import run_labeled

ID = "S6"
TITLE = "Admission and pipeline"
CORPORA = ["labeled.admission", "labeled.boundaries", "labeled.precedence", "labeled.pipeline"]


def run(ctx: SuiteContext) -> SuiteResult:
    names = ctx.config.section("s6").get("corpus", CORPORA)
    _, report = precedence.build_report(ctx.contract)
    built = [r for r in report if r["built"]]
    matrix = {
        "pairs": len(report),
        "built": len(built),
        "second_verified": sum(r["second_verified"] for r in built),
        "not_constructible": [r for r in report if not r["built"]],
        "why_not": dict(Counter("disjoint slots" if "no slot" in r["why"] else "mutually exclusive conditions"
                                if "holds=False" in r["why"] else r["why"] for r in report if not r["built"])),
    }
    return run_labeled(ctx, ID, TITLE, names, ["R-1", "R-2", "R-3", "R-8", "R-9", "R-10", "R-13", "R-14", "R-15",
                                               "R-16", "R-18", "R-20", "R-21", "R-24", "R-25", "R-26"],
                       {"precedence": matrix})
