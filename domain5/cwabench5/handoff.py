"""How a payload reaches the model (domain-5-plan.md, 9; Domain 2's handoff, cwabench2.model.messages).

Every arm's payload has one shape, `cwa-messages/v1`'s request: system entries, tools, messages. The system entries'
texts, joined by a blank line, become one `system` message when there are any; the payload's messages follow
unchanged, with any field a message carries besides its role and content (a baseline's tool call and the tool
message answering it, `tool-role`) passed through as it is. Nothing is added: the text the model reads is the text
the payload holds.
"""
from __future__ import annotations

import json


def messages(payload: bytes) -> list[dict]:
    request = json.loads(payload.decode("utf-8"))
    system = "\n\n".join(entry["text"] for entry in request.get("system", []))
    handed = [{"role": "system", "content": system}] if system else []
    for message in request.get("messages", []):
        content = message["content"]
        if isinstance(content, list):  # cwa-message-blocks/v1: content parts, joined in order
            content = "".join(block["text"] for block in content)
        handed.append({**message, "content": content})
    return handed


def texts(payload: bytes) -> list[str]:
    """Every text a payload hands the model: the system message's, then each message's content."""
    return [m["content"] for m in messages(payload) if isinstance(m["content"], str)]
