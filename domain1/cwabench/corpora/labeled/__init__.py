"""Labeled corpora (domain-1-plan.md, section 5, oracle 1): snapshots built from a table of intended outcomes, each
carrying a label that states every decision its trace must record. Labels come from the rules as the generator's
table writes them, never from an assembler, so they can catch a mistake all four assemblers share.

A label states:
    outcome      "assembled" or "refused" (rejections are their own corpus kind)
    refusal      the refusal code, or None
    recovery     recovery.action, or None
    fates        recorded item id → {"fate": "kept"} | {"fate": "excluded", "reason": …, ["duplicate_of"/"superseded_by"]}
                 | {"fate": "compressed", "variant_id": …}. "kept" means included when assembled, and no exclusion row
                 when refused. Every candidate has a fate.
    conflicts    group id → {"decided_by", "resolution", "winner"}
    notes        why, for people reading a failure

Token counts, row order, the digest and the hash are not restated: the auditor and the differential oracle judge
those for every answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import Snapshot


def kept() -> dict:
    return {"fate": "kept"}


def excluded(reason: str, **refs) -> dict:
    return {"fate": "excluded", "reason": reason, **refs}


def compressed(variant_id: str) -> dict:
    return {"fate": "compressed", "variant_id": variant_id}


@dataclass
class Label:
    outcome: str = "assembled"
    refusal: str | None = None
    recovery: str | None = None
    fates: dict = field(default_factory=dict)
    conflicts: dict = field(default_factory=dict)
    notes: str = ""

    def as_json(self) -> dict:
        return {"outcome": self.outcome, "refusal": self.refusal, "recovery": self.recovery, "fates": self.fates,
                "conflicts": self.conflicts, "notes": self.notes}

    def reasons(self) -> list[tuple[str, str | None]]:
        """(reason, slot) pairs this label exercises, for coverage. Slots come from the fates' `slot` hints."""
        out = [(f["reason"], f.get("slot")) for f in self.fates.values() if f["fate"] == "excluded"]
        if self.refusal:
            out.append((self.refusal, None))
        return out


def labeled(corpus: str, case_id: str, data: bytes, label: Label, rules=()) -> Snapshot:
    return Snapshot(corpus, case_id, "case", tuple(rules), data, label=label.as_json())


class NotConstructible(Exception):
    """A generator cannot build this combination; the reason is reported, never silently dropped."""
