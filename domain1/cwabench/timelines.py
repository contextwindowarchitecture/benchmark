"""Assembly timelines (domain-1-plan.md, 12.4): one ordered event stream per answer across the eight stages, derived
from the snapshot and the trace alone, without instrumenting any assembler.

Outside fitting the trace's documented row order is the order things happened, so those events are "exact". Inside
fitting, over_budget rows are in omission order, but compressed[] rows follow included[] order. Each compression is
placed before the first omission of a later fitting step (the route's fitting_order steps, then a compress step per
slot in shedding order, then an omit step per slot), from the lowest rank up within its step, and marked "inferred".
The sweep frames are the exact source when fitting order matters.
"""
from __future__ import annotations

from .oracles.auditor.model import View, stage_of

LANES = ["producer", "admit", "resolve", "supersede", "dedupe", "diversity", "fit", "render"]
STAGE_LANE = {0: "admit", 1: "resolve", 2: "supersede", 3: "dedupe", 4: "diversity", 5: "fit"}


def _steps(view: View) -> list[tuple[str, str]]:
    """The compressible-fitting steps in order: (slot, action)."""
    listed = [(s["slot"], s["action"]) for s in view.route.get("fitting_order") or []]
    slots = sorted({c.slot for c in view.candidates if isinstance(c.slot, str)}, key=view.slot_order_key)
    return listed + [(s, a) for a in ("compress", "omit") for s in slots if (s, a) not in listed]


def build(contract, snapshot: dict, trace: dict, adapter: str, snapshot_ref: str, run_id: str) -> dict:
    view = View.build(contract, snapshot)
    events: list[dict] = []

    def add(stage: str, type_: str, order: str = "exact", **fields) -> None:
        events.append({"seq": len(events), "stage": stage, "type": type_, **fields, "order": order})

    assembler_rows = [r for r in trace.get("excluded") or [] if r.get("stage") == "assembler"]
    for row in trace.get("excluded") or []:
        if row.get("stage") == "producer":
            add("producer", "excluded", item_id=row.get("item_id"), reason=row.get("reason"))
    for row in assembler_rows:
        if stage_of(row.get("reason")) == 0:
            add("admit", "excluded", item_id=row.get("item_id"), slot=row.get("slot"), reason=row.get("reason"))
    filled: dict[str, list[str]] = {}
    for row in trace.get("defaults_filled") or []:
        filled.setdefault(row["item_id"], []).append(row["field"])
    for item_id, fields in filled.items():
        add("admit", "defaults_filled", item_id=item_id, fields=fields)
    for group in trace.get("conflicts") or []:
        add("resolve", "conflict", group_id=group.get("group_id"), decided_by=group.get("decided_by"),
            resolution=group.get("resolution"), winner=group.get("winner"))
    for stage in (1, 2, 3, 4):
        for row in assembler_rows:
            if stage_of(row.get("reason")) == stage:
                refs = {k: row[k] for k in ("duplicate_of", "superseded_by") if k in row}
                add(STAGE_LANE[stage], "excluded", item_id=row.get("item_id"), slot=row.get("slot"),
                    reason=row.get("reason"), **refs)

    # Fitting: omissions in trace order, compressions inferred into place.
    steps = _steps(view)
    step_index = {step: i for i, step in enumerate(steps)}

    def tier(item_id):
        candidate = view.unique(item_id)
        return view.tier(candidate) if candidate else None

    omissions = []
    for row in assembler_rows:
        if row.get("reason") == "over_budget":
            t = tier(row.get("item_id"))
            index = -1 if t == "droppable" else step_index.get((row.get("slot"), "omit"), len(steps))
            omissions.append((index, row, t))
    compressions = []
    for row in trace.get("compressed") or []:
        candidate = view.unique(row.get("item_id"))
        rank = view.rank_key(candidate) if candidate else ()
        compressions.append((step_index.get((row.get("slot"), "compress"), len(steps)), rank, row))
    # Lowest rank first within a step: rank keys sort highest first, so reverse within each step.
    compressions.sort(key=lambda c: c[0])
    ordered_compressions = []
    for index in sorted({c[0] for c in compressions}):
        ordered_compressions += sorted((c for c in compressions if c[0] == index), key=lambda c: c[1], reverse=True)
    pending = list(ordered_compressions)
    for index, row, t in omissions:
        while pending and pending[0][0] < index:
            _, _, crow = pending.pop(0)
            add("fit", "compressed", "inferred", item_id=crow.get("item_id"), slot=crow.get("slot"),
                variant_id=crow.get("variant_id"), **{"from": crow.get("from"), "to": crow.get("to")})
        add("fit", "omitted", item_id=row.get("item_id"), slot=row.get("slot"), tier=t)
    for _, _, crow in pending:
        add("fit", "compressed", "inferred", item_id=crow.get("item_id"), slot=crow.get("slot"),
            variant_id=crow.get("variant_id"), **{"from": crow.get("from"), "to": crow.get("to")})

    refused = trace.get("refused") or {}
    if refused.get("bool"):
        add("fit", "refused", reason=refused.get("reason"),
            recovery=(trace.get("recovery") or {}).get("action") if isinstance(trace.get("recovery"), dict) else None)
    for position, row in enumerate(trace.get("included") or []):
        add("render", "placed", item_id=row.get("item_id"), slot=row.get("slot"), position=position,
            tokens=row.get("tokens"))

    admitted = len(view.candidates) - sum(1 for r in assembler_rows if stage_of(r.get("reason")) == 0)
    return {
        "$schema": "cwa-bench-d1/timeline/v1",
        "run_id": run_id,
        "snapshot": snapshot_ref,
        "adapter": adapter,
        "outcome": "refused" if refused.get("bool") else "assembled",
        "refusal_reason": refused.get("reason"),
        "lanes": LANES,
        "events": events,
        "counters": {
            "candidates": len(view.candidates) + len(view.producer_rows),
            "admitted": admitted,
            "included": len({r.get("item_id") for r in trace.get("included") or []}),
            "omitted": len(omissions),
            "compressed": len(compressions),
            "budget_input": snapshot["budget"]["input"],
            "final_tokens": (trace.get("result") or {}).get("input_tokens"),
        },
    }
