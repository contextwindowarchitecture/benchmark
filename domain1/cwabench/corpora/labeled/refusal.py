"""labeled.refusal: every combination of the six refusal conditions (SPEC.md R-4, R-11, R-12, R-16, R-17, R-20).
Assembly checks them in contract/reasons.json order and records the first that holds (R-21; conformance/README.md,
Refusals). Each condition is made by one independent change to a base snapshot, so all 63 non-empty combinations
can be built; the label expects the earliest, and the recovery action that one implies."""
from __future__ import annotations

from itertools import combinations

from ...contract import Contract
from . import Label, kept, labeled
from .builder import Builder, payload_tokens, words
from .faults import recorded_id

CORPUS = "labeled.refusal"
CONDITIONS = ("required_slot_missing", "protected_slot_unplaced", "conflict_unresolved",
              "protected_content_over_budget", "slot_floor_over_budget", "evidence_required")


def _apply(b: Builder, condition: str) -> None:
    if condition == "required_slot_missing":
        b.batches["chat"]["items"] = [i for i in b.batches["chat"]["items"] if i["id"] != "q:base"]
    elif condition == "protected_slot_unplaced":
        b.item("st:unplaced", "state.task")
        b.unplace("state.task")
    elif condition == "conflict_unresolved":
        b.route["on_unresolved_instruction"] = "refuse"
        b.item("ex:c1", "governance.examples", conflict_policy="governs")
        b.item("ex:c2", "governance.examples", conflict_policy="governs")
        b.group("g:c", "instruction", ["ex:c1", "ex:c2"])
    elif condition == "protected_content_over_budget":
        b.item("gov:capped", "governance.instructions", "Five words of protected text.", token_budget=2)
    elif condition == "slot_floor_over_budget":
        b.item("ex:f1", "governance.examples")
        b.item("ex:f2", "governance.examples")
    elif condition == "evidence_required":
        b.route["requires_evidence"] = True


def _finish(b: Builder, conditions: tuple) -> None:
    """The floor needs the whole payload: the examples' slot is held at its full size and the budget is one token
    short, so every reduction would break the floor and nothing else can be reduced."""
    if "slot_floor_over_budget" in conditions:
        examples = [i for i in b.items() if i["slot"] == "governance.examples"]
        b.rule("governance.examples", min_tokens=sum(words(i["body"]) for i in examples))
        rendered = [i for i in b.items() if i["slot"] in {p["slot"] for p in b.placement}]
        b.budget["input"] = sum(3 + words(i["body"]) for i in rendered) - 1


def build(contract: Contract):
    out = []
    for size in range(1, len(CONDITIONS) + 1):
        for combo in combinations(CONDITIONS, size):
            name = "+".join(c.split("_")[0] + "_" + c.split("_")[1] for c in combo)
            b = Builder(contract, f"refusal-{name}")
            b.base()
            for condition in combo:
                _apply(b, condition)
            _finish(b, combo)
            winner = next(c for c in CONDITIONS if c in combo)
            label = Label(outcome="refused", refusal=winner,
                          recovery="request_context" if winner == "evidence_required" else None,
                          fates={recorded_id(b, i): kept() for i in b.items()},
                          notes=f"conditions {', '.join(combo)} hold; {winner} is checked first")
            if "conflict_unresolved" in combo:
                label.conflicts["g:c"] = {"decided_by": "escalated", "resolution": "refused", "winner": None}
            out.append(labeled(CORPUS, f"refusal-{'+'.join(combo)}", b.bytes(), label, ("R-17", "R-21")))
    return out
