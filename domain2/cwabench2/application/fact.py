"""The fact-in-payload oracle (domain-2-plan.md, 5.3): was the fact a probe's answer needs in the payload at all?

It decides two ways, which S1 requires to agree:

- **by the trace**: a needed turn is present when any item carrying it (its user message in history, its memory, or a
  state item holding its fact) is among the trace's `included` rows. This is how S2 attributes every wrong answer:
  with the fact present it is the model's failure, with it absent the assembly's or a producer's;
- **by the text**: the payload's own text (its system entries and user message, decoded from the payload's JSON)
  contains one of the texts that carry the turn: its fact sentences, or the state body holding its fact.

Present means every needed turn is present. S0 checks `by_trace` against planted omissions.
"""
from __future__ import annotations

import json

from cwabench.canon.payloads import escape_body


def by_trace(included: set[str], carriers: dict[str, list[str]], needs: list[str]) -> list[bool]:
    return [any(item_id in included for item_id in carriers.get(need, [])) for need in needs]


def payload_texts(payload: bytes) -> tuple[str, str]:
    """A cwa-messages/v1 payload's system and tool texts, and its user message's content."""
    request = json.loads(payload.decode("utf-8"))
    system = "\n".join(entry["text"] for entry in [*request.get("system", []), *request.get("tools", [])])
    content = "".join(message["content"] if isinstance(message["content"], str)
                      else "".join(block["text"] for block in message["content"])
                      for message in request.get("messages", []))
    return system, content


def by_text(payload: bytes | None, evidence: dict[str, list[str]], needs: list[str]) -> list[bool]:
    if payload is None:
        return [False] * len(needs)
    system, content = payload_texts(payload)
    return [any(text in system or escape_body(text) in content for text in evidence.get(need, [])) for need in needs]


def by_text_chat(payload: bytes, evidence: dict[str, list[str]], needs: list[str]) -> list[bool]:
    """The same for a baseline's chat payload: every system text and message content searched on its own, unescaped,
    since native chat has no wrapper."""
    request = json.loads(payload.decode("utf-8"))
    texts = [entry["text"] for entry in request.get("system", [])] + [m["content"] for m in request.get("messages", [])]
    return [any(fact in text for fact in evidence.get(need, []) for text in texts) for need in needs]
