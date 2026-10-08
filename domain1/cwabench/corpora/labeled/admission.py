"""labeled.admission: one snapshot per admission fault per slot it applies to. The target is the only item that may
be excluded, and it must be excluded with exactly the fault's code; the base items must stay."""
from __future__ import annotations

from ...contract import Contract
from . import Label, excluded, kept, labeled
from .builder import SLOTS, Builder
from .faults import FAULTS, recorded_id, target

CORPUS = "labeled.admission"


def case(contract: Contract, fault, slot: str | None):
    name = f"{fault.code.replace(':', '-')}--{fault.variant}--{slot or 'no-slot'}"
    b = Builder(contract, name)
    b.base()
    item = target(b, slot or "evidence.knowledge")
    overrides = fault.apply(b, item)
    fates = {recorded_id(b, i): kept() for i in b.items() if i is not item}
    fates[recorded_id(b, item)] = excluded(fault.code, slot=slot)
    fates.update(overrides)
    label = Label(fates=fates, notes=f"{fault.name} in {slot or 'an item with no valid slot'}: excluded with "
                                     f"{fault.code}, nothing else excluded")
    return labeled(CORPUS, name, b.bytes(), label, rules=("R-1", "R-2", "R-3", "R-21"))


def build(contract: Contract):
    out = []
    for fault in FAULTS:
        for slot in (fault.slots if fault.slots is not None else (None,)):
            out.append(case(contract, fault, slot))
    return out
