"""Generated corpora (domain-1-plan.md, 7.5): valid snapshots built at random from a seed (generate.py), steered toward
coverage the traces have not shown yet (steer.py), and mutants that break exactly one snapshot check (mutate.py).

`fuzz.seeds` is a fixed, unsteered corpus of 500, the plan's fuzz seeds: S4 transforms some of them, and S2 or S10 can
list it. S5 generates its own corpora as it runs, since steering depends on the answers.
"""
from __future__ import annotations

from ...contract import Contract
from .. import Snapshot

SEED = 20261006
SEEDS = 500


def seeds(contract: Contract) -> list[Snapshot]:
    from .generate import corpus

    return [Snapshot("fuzz.seeds", g.case_id, "case", (), g.data) for g in corpus(contract, SEED, SEEDS, "seed")]
