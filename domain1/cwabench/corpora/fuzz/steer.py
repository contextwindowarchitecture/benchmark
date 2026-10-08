"""Coverage-guided steering (domain-1-plan.md, 7.5): between rounds of generation, raise the weight of intents whose
coverage tags the traces have shown least, so later rounds spend their snapshots on empty cells.

Coverage is read from what assemblers actually recorded, never from what the generator meant: an intent whose fault
an earlier code masks keeps its weight until a trace shows its reason.
"""
from __future__ import annotations

from collections import Counter

from ...traces import coverage_tags
from .generate import FAMILY_WEIGHT, FAULT_INTENTS, FEATURES, INTENTS, targets

ZERO_BOOST = 4.0  # an intent aiming at a tag no trace has shown yet


def observe(trace: dict | None, snapshot: dict | None) -> list[str]:
    """The tags one answer exercised: the trace's coverage tags plus the components and layouts the snapshot used."""
    tags = set(coverage_tags(trace, snapshot))
    if isinstance(snapshot, dict):
        tags.add(f"component:{snapshot.get('renderer')}|{snapshot.get('tokenizer')}")
        placement = ((snapshot.get("profile") or {}).get("placement")) or []
        slots = [p.get("slot") for p in placement if isinstance(p, dict)]
        if len(slots) != len(set(slots)):
            tags.add("layout:twice")
        if any(isinstance(p, dict) and p.get("wrap") in ("system", "tools") for p in placement):
            tags.add("layout:messages")
    return sorted(tags)


class Steering:
    def __init__(self):
        self.universe = [t for t in targets() if not t.startswith("edge:")]  # edge pools aim at values, not tags
        self.counts: Counter[str] = Counter()
        self.history: list[dict] = []  # per round: coverage before it, and the weights it ran with

    def observe(self, tags) -> None:
        self.counts.update(set(tags))

    def covered(self) -> int:
        return sum(1 for t in self.universe if self.counts[t] > 0)

    def intent_weight(self, intent) -> float:
        aimed = [t for t in intent.targets if t in self.universe]
        if not aimed:
            return 1.0
        least = min(self.counts[t] for t in aimed)
        return (ZERO_BOOST if least == 0 else 1.0) / (1 + least) ** 0.5

    def weights(self) -> tuple[dict[str, float], dict[str, float]]:
        intents = {name: self.intent_weight(i) for name, i in INTENTS.items()}
        families = {}
        for family, base in FAMILY_WEIGHT.items():
            members = FAULT_INTENTS if family == "fault" else [i for i in FEATURES if i.family == family]
            aimed = {t for i in members for t in i.targets if t in self.universe}
            open_ = sum(1 for t in aimed if self.counts[t] == 0) / len(aimed) if aimed else 0.0
            families[family] = base * (0.5 + 3 * open_)
        return intents, families

    def start_round(self, number: int, size: int) -> tuple[dict[str, float], dict[str, float]]:
        intents, families = self.weights()
        self.history.append({"round": number, "snapshots": size, "covered_before": self.covered(),
                             "universe": len(self.universe),
                             "family_weights": {k: round(v, 4) for k, v in sorted(families.items())}})
        return intents, families

    def uncovered(self) -> list[str]:
        return [t for t in self.universe if self.counts[t] == 0]
