"""The conventional arms of the LQ family (domain-2-plan.md, 6.5 and 7), built as the chat baselines are
(baselines/__init__.py): native chat, counted by the same tokenizer and margin, recording what they kept.

| Arm | Payload |
| --- | --- |
| `control-full` | every chunk of the corpus, in order; no budget, but an overflow when it exceeds the model's context |
| `truncate-pinned` | the system prompt pinned, the earliest chunks dropped until the payload fits the budget |
| `rag` | the retriever's candidates (the CWA arms' own), the lowest-ranked dropped until it fits, best first |

The system prompt is the instructions, a blank line and the output contract. The one user message is the documents'
chunks, each separated by a blank line, under "Documents:", then the question under "Question:", which no arm drops.
`rag` admits every candidate, however low its relevance: a conventional retriever's top-k has no threshold, which is
one way CWA's admission differs.

**What it kept** is the chunk ids in payload order, so the fact-in-payload oracle reads the record (every needed
chunk kept) and the text (every answer sentence in the payload), as for the chat baselines.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cwabench.canon.render import charged
from cwabench.canon.tokenizers import TOKENIZERS

from . import Message, payload

ARMS = ("control-full", "truncate-pinned", "rag")


@dataclass
class Built:
    arm: str
    outcome: str  # "fits", or "overflow" when even the question alone exceeds the budget (or the model's context)
    payload: bytes
    input_tokens: int
    charged: int
    kept: list[str]  # chunk ids, in payload order
    offered: int  # the chunks the arm chose from

    @property
    def payload_hash(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


def system_prompt(corpus: dict) -> str:
    return f"{corpus['instructions']}\n\n{corpus['output_contract']}"


def user_message(bodies: list[str], question: str) -> str:
    documents = "\n\n".join(bodies)
    return f"Documents:\n\n{documents}\n\nQuestion: {question}" if bodies else f"Question: {question}"


def build(arm: str, corpus: dict, question: dict, candidates: list[tuple[str, float]], budget: int | None,
          margin_percent: int, tokenizer: str, context_limit: int | None = None) -> Built:
    """`arm`'s payload for `question`. `budget` bounds truncate-pinned and rag; control-full has none, and
    `context_limit`, the model's window less its reserved output, makes it an overflow when it does not fit."""
    count = TOKENIZERS[tokenizer]
    chunks = {c["id"]: c for c in corpus["chunks"]}
    system = system_prompt(corpus)
    if arm == "control-full" or arm == "truncate-pinned":
        offered = [c["id"] for c in corpus["chunks"]]
    elif arm == "rag":
        offered = [chunk_id for chunk_id, _ in candidates]
    else:
        raise ValueError(f"no LQ baseline {arm!r}")

    def cost(kept: list[str]) -> int:
        return charged(count(system) + count(user_message([chunks[i]["body"] for i in kept], question["question"])),
                       margin_percent)

    if arm == "control-full":
        kept = offered
        limit = context_limit
    else:
        # truncate-pinned keeps a suffix of the corpus, rag a prefix of the ranking: the longest that fits
        def pick(n: int) -> list[str]:
            return offered[len(offered) - n:] if arm == "truncate-pinned" else offered[:n]

        low, high = 0, len(offered)
        while low < high:
            middle = (low + high + 1) // 2
            low, high = (middle, high) if cost(pick(middle)) <= budget else (low, middle - 1)
        kept = pick(low)
        limit = budget
    tokens = count(system) + count(user_message([chunks[i]["body"] for i in kept], question["question"]))
    outcome = "fits" if limit is None or charged(tokens, margin_percent) <= limit else "overflow"
    messages = [Message("system", "system", system),
                Message(f"question:{question['question_id']}", "user",
                        user_message([chunks[i]["body"] for i in kept], question["question"]))]
    return Built(arm, outcome, payload(messages), tokens, charged(tokens, margin_percent), kept, len(offered))


def present(built: Built, question: dict) -> tuple[list[bool], list[bool]]:
    """Whether each needed chunk is in the payload: by the record (kept) and by the text (its answer sentences)."""
    from ..application.fact import by_text_chat

    needs = question["needs"]
    return [need in built.kept for need in needs], by_text_chat(built.payload, question["evidence"], needs)
