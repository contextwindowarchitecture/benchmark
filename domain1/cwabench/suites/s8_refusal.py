"""S8 · Refusal, degraded evidence and fitting (domain-1-plan.md, 7.8). Every combination of the six refusal
conditions, evidence lost one mechanism at a time until the route must refuse, and fitting decisions small enough to
label exactly: tier order, shedding order, variant choice, caps, floors, the route's fitting order and the margin."""
from __future__ import annotations

from . import SuiteContext, SuiteResult
from .labeled import run_labeled

ID = "S8"
TITLE = "Refusal, degraded evidence and fitting"
CORPORA = ["labeled.refusal", "labeled.degradation", "labeled.fitting"]


def run(ctx: SuiteContext) -> SuiteResult:
    from ..corpora.labeled.degradation import MECHANISMS, M, N
    from ..corpora.labeled.refusal import CONDITIONS

    return run_labeled(ctx, ID, TITLE, ctx.config.section("s8").get("corpus", CORPORA),
                       ["R-4", "R-11", "R-12", "R-16", "R-17", "R-18", "R-20", "R-21"],
                       {"refusal_matrix": {"conditions": list(CONDITIONS), "combinations": 2 ** len(CONDITIONS) - 1},
                        "degradation": {"chunks": N, "min_included": M,
                                        "mechanisms": [*MECHANISMS, "over_budget (no variants)",
                                                       "over_budget (variants)"]}})
