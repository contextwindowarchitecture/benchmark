"""S9 · Conflicts (domain-1-plan.md, 7.9). Instruction groups across every combination of peer policies, fact groups
across precedence, scope eligibility and the freshness tie-break, protected members, every unresolved action, and the
same groups with trust and the verified flag changed, which must change nothing."""
from __future__ import annotations

from . import SuiteContext, SuiteResult
from .labeled import run_labeled

ID = "S9"
TITLE = "Conflicts"


def run(ctx: SuiteContext) -> SuiteResult:
    return run_labeled(ctx, ID, TITLE, ctx.config.section("s9").get("corpus", ["labeled.conflicts"]),
                       ["R-6", "R-7", "R-11", "R-12"])
