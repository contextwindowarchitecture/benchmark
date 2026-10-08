"""The published renderers, written from conformance/README.md (Tokenizers and renderers), never from an assembler.

S7 needs the payload's exact count for a chosen set of items without assembling: the protected items alone, which
decide the refusal threshold (Fitting, step 1), and every item, which sets the full size budgets are a fraction of.
`render` lays out the items a caller says are included, with the bodies it gives them, and `count` counts the result
as the fit test does. S0 checks both against every published payload and `input_tokens` before S7 trusts them.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import instants, jcs
from .payloads import escape_attribute, escape_body, stream_of
from .strings import utf16_key
from .tokenizers import TOKENIZERS

HISTORY = "interaction.history"


@dataclass(frozen=True)
class Rendered:
    payload: bytes
    counted: list[str]  # the texts the payload's count is the sum of, as payloads.parse recovers them

    def count(self, tokenizer: str) -> int:
        count = TOKENIZERS[tokenizer]
        return sum(count(text) for text in self.counted)


def _order(items: list[dict], slot: str) -> list[dict]:
    """Within a placement: by id in UTF-16 order, except history, by freshness as an instant and then id (R-7)."""
    if slot == HISTORY:
        return sorted(items, key=lambda i: (instants.parse(i["freshness"]), utf16_key(i["id"])))
    return sorted(items, key=lambda i: utf16_key(i["id"]))


def _xml(tag: str, item: dict, body: str, conflict: str | None, speaker: str | None) -> str:
    attributes = f' id="{escape_attribute(item["id"])}"'
    if speaker is not None:
        attributes += f' speaker="{speaker}"'
    if conflict is not None:
        attributes += f' conflict="{escape_attribute(conflict)}"'
    return f"<{tag}{attributes}>\n{escape_body(body)}\n</{tag}>\n"


def _entry(item: dict, body: str, conflict: str | None) -> dict:
    if conflict is None:
        return {"id": item["id"], "text": body}
    return {"id": item["id"], "text": f'<conflict group="{escape_attribute(conflict)}">\n{body}\n</conflict>',
            "conflict": conflict}


def render(snapshot: dict, items: list[dict], bodies: dict[str, str] | None = None,
           surfaced: dict[str, str] | None = None) -> Rendered:
    """The payload `snapshot`'s renderer writes when exactly `items` are included.

    `bodies` maps an item id to the body it renders with (a chosen variant's); `surfaced` maps the id of each member
    of a surfaced conflict group to its group id. Items are placed by their slot, once per placement of it."""
    bodies, surfaced = bodies or {}, surfaced or {}
    renderer = snapshot["renderer"]
    by_slot: dict[str, list[dict]] = {}
    for item in items:
        by_slot.setdefault(item["slot"], []).append(item)
    occurrences = []  # (stream, tag, item, body, slot)
    for placement in snapshot["profile"]["placement"]:
        stream, tag = stream_of(placement["wrap"])
        for item in _order(by_slot.get(placement["slot"], []), placement["slot"]):
            occurrences.append((stream, tag, item, bodies.get(item["id"], item["body"]), placement["slot"]))

    if renderer == "fixture-xml/v1":
        text = "".join(_xml(tag, item, body, surfaced.get(item["id"]), None) for _, tag, item, body, _ in occurrences)
        return Rendered(text.encode("utf-8"), [text])
    if renderer not in ("cwa-messages/v1", "cwa-message-blocks/v1"):
        raise ValueError(f"no renderer {renderer}")

    def speaker(item, slot):
        if slot != HISTORY:
            return None
        return "assistant" if item.get("lineage") == "generated" else "user"

    system = [_entry(item, body, surfaced.get(item["id"])) for stream, _, item, body, _ in occurrences
              if stream == "system"]
    tools = [_entry(item, body, surfaced.get(item["id"])) for stream, _, item, body, _ in occurrences
             if stream == "tools"]
    blocks = []
    for stream, tag, item, body, slot in occurrences:
        if stream == "xml":
            block = {"id": item["id"], "text": _xml(tag, item, body, surfaced.get(item["id"]), speaker(item, slot))}
            if item["id"] in surfaced:
                block["conflict"] = surfaced[item["id"]]
            blocks.append(block)
    counted = [e["text"] for e in system] + [e["text"] for e in tools]
    if renderer == "cwa-messages/v1":
        content = "".join(b["text"] for b in blocks)
        counted.append(content)
    else:
        content = blocks
        counted += [b["text"] for b in blocks]
    request = {"system": system, "tools": tools, "messages": [{"role": "user", "content": content}]}
    return Rendered(jcs.serialize_bytes(request), counted)


def charged(tokens: int, margin_percent: int = 0) -> int:
    """The count the fit test compares with budget.input (conformance/README.md, Fitting)."""
    return (tokens * (100 + margin_percent) + 99) // 100


def from_trace(snapshot: dict, trace: dict) -> Rendered:
    """Render what a trace says it included: its included items, its compressed rows' variants, its surfaced groups.
    Used to check this module against published payloads; ids of included items must be unique in the snapshot."""
    candidates = {}
    for batch in snapshot["batches"]:
        for item in batch["items"]:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                candidates.setdefault(item["id"], []).append(item)
    seen, items = set(), []
    for row in trace["included"]:
        if row["item_id"] not in seen:
            seen.add(row["item_id"])
            matches = [c for c in candidates[row["item_id"]] if c.get("slot") == row["slot"]]
            items.append(matches[0])
    bodies = {}
    for row in trace["compressed"]:
        item = next(i for i in items if i["id"] == row["item_id"])
        bodies[row["item_id"]] = next(v["body"] for v in item["variants"] if v["id"] == row["variant_id"])
    surfaced = {member: group["group_id"] for group in trace["conflicts"] if group.get("resolution") == "surfaced"
                for member in group["items"] if member in seen}
    return render(snapshot, items, bodies, surfaced)
