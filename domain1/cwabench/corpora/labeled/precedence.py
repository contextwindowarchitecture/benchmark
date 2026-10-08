"""labeled.precedence: every pair of admission codes that one item can fail together, built on one item. R-21: the
recorded reason is the earliest applicable code in contract/reasons.json; among missing_field codes, the
alphabetically first field.

Each pair is verified before its label is trusted: the auditor's independent condition checks must confirm that the
earlier code's condition holds for the built item, and the later code's too, where they can tell. A pair that cannot
be built, or whose conditions do not both hold, is reported by build_report(), never silently dropped.
"""
from __future__ import annotations

import json
from itertools import combinations

from ...contract import Contract
from . import Label, NotConstructible, excluded, kept, labeled
from .builder import Builder
from .faults import FAULTS, recorded_id, target

CORPUS = "labeled.precedence"
SLOT_ORDER = ("evidence.knowledge", "evidence.tool_results", "interaction.history", "interaction.memory",
              "governance.examples", "state.user", "state.task", "governance.instructions",
              "governance.capabilities", "governance.output_contract", "interaction.query")


def canonical(contract: Contract) -> list:
    """One fault per code: its first variant, merged across the slots it is defined for one at a time."""
    from .faults import Fault

    # duplicate_item_id uses the producer-row variant: a twin would be a second candidate with faults of its own.
    preferred = {"duplicate_item_id": "producer-row"}
    chosen: dict[str, list] = {}
    for fault in FAULTS:
        variant = preferred.get(fault.code)
        if variant is not None and fault.variant != variant:
            continue
        group = chosen.setdefault(fault.code, [])
        if not group or group[0].variant == fault.variant:
            group.append(fault)
    out = []
    for code, group in chosen.items():
        if len(group) == 1:
            out.append(group[0])
            continue

        def apply(b, item, group=group):
            return next(f for f in group if item["slot"] in f.slots).apply(b, item)

        slots = tuple(dict.fromkeys(s for f in group for s in f.slots))
        out.append(Fault(code, group[0].variant, slots, apply))
    return out


def order(contract: Contract, code: str) -> tuple[int, str]:
    codes = [r["code"] for r in contract.reasons]
    if code.startswith("missing_field:"):
        return codes.index("missing_field:<name>"), code.split(":", 1)[1]
    return codes.index(code), ""


def _holds(contract, data: bytes, item_id: str, code: str):
    from ...oracles.auditor.checks import Audit

    audit = Audit(contract, data, None, {})
    candidates = audit.view.by_id.get(item_id, [])
    if not candidates:
        return None
    return audit._justified(candidates[0], code)


def pair(contract: Contract, first, second):
    """Build an item failing both faults; `first` must win."""
    slots = [None] if first.slots is None or second.slots is None else [
        s for s in SLOT_ORDER if s in first.slots and s in second.slots]
    if not slots:
        raise NotConstructible("no slot both faults apply to")
    last_problem = "no slot worked"
    for slot in slots:
        for sequence in ((second, first), (first, second)):
            name = f"{first.code.replace(':', '-')}--over--{second.code.replace(':', '-')}--{slot or 'no-slot'}"
            b = Builder(contract, name)
            b.base()
            item = target(b, slot or "evidence.knowledge")
            try:
                for fault in sequence:
                    fault.apply(b, item)
            except NotConstructible as why:
                last_problem = f"in {slot}: {why}"
                continue
            data = b.bytes()
            rid = recorded_id(b, item)
            first_holds = _holds(contract, data, rid, first.code)
            second_holds = _holds(contract, data, rid, second.code)
            if first_holds is False or second_holds is False:
                last_problem = (f"in {slot}: {first.code} holds={first_holds}, {second.code} holds={second_holds}")
                continue
            if first_holds is None and not (first.code.startswith(("missing_field:", "unknown_", "producer_not")) or
                                             first.code in ("duplicate_item_id",)):
                last_problem = f"in {slot}: cannot confirm {first.code} holds"
                continue
            fates = {recorded_id(b, i): kept() for i in b.items() if i is not item}
            fates[rid] = excluded(first.code, slot=slot)
            label = Label(fates=fates, notes=f"fails {first.code} and {second.code}; {first.code} comes first "
                                             f"(second verified: {second_holds})")
            return labeled(CORPUS, name, data, label, rules=("R-21",)), second_holds
    raise NotConstructible(last_problem)


def build_report(contract: Contract) -> tuple[list, list[dict]]:
    faults = sorted(canonical(contract), key=lambda f: order(contract, f.code))
    built, report = [], []
    for a, b in combinations(faults, 2):
        if order(contract, a.code) > order(contract, b.code):
            a, b = b, a
        try:
            snapshot, second = pair(contract, a, b)
            built.append(snapshot)
            report.append({"first": a.code, "second": b.code, "built": True, "second_verified": second is True,
                           "case_id": snapshot.case_id})
        except NotConstructible as why:
            report.append({"first": a.code, "second": b.code, "built": False, "why": str(why)})
    return built, report


def build(contract: Contract):
    return build_report(contract)[0]
