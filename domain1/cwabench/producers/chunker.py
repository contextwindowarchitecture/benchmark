"""A deterministic chunker: documents into evidence chunks at sentence boundaries (domain-1-plan.md, section 9).

Sentences are packed greedily, in order, into chunks of at most `max_tokens` under the route's tokenizer; a sentence
longer than that is a chunk of its own, and never split. Paragraph breaks inside a chunk are kept. A chunk is a pure
function of its document's text and the two limits, so its id and body are stable across runs and machines.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ..canon.tokenizers import TOKENIZERS
from .source import Document

# A sentence ends at . ! or ? followed by whitespace and an upper-case letter or a quote. Decimal points, amounts and
# dotted names never satisfy that, so the split is safe for the source corpus's prose.
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'])")


def sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text.strip()) if s]


def _join(pieces: list[tuple[str, bool]]) -> str:
    return "".join(s if i == 0 else ("\n\n" if new_paragraph else " ") + s
                   for i, (s, new_paragraph) in enumerate(pieces))


@dataclass(frozen=True)
class Chunk:
    id: str
    document: str
    index: int
    source: str
    source_version: str  # the document's own digest, so an edited document versions every chunk it yields
    body: str
    tokens: int


def chunk(docs: list[Document], max_tokens: int = 110, tokenizer: str = "fixture-whitespace/v1") -> list[Chunk]:
    count = TOKENIZERS[tokenizer]
    out = []
    for doc in docs:
        version = hashlib.sha256(doc.text.encode("utf-8")).hexdigest()[:12]
        pieces: list[tuple[str, bool]] = []  # (sentence, starts a paragraph)
        for paragraph in doc.text.split("\n\n"):
            for n, sentence in enumerate(sentences(paragraph)):
                pieces.append((sentence, n == 0))
        groups: list[list[tuple[str, bool]]] = [[]]
        for piece in pieces:
            if groups[-1] and count(_join(groups[-1] + [piece])) > max_tokens:
                groups.append([])
            groups[-1].append(piece)
        for index, group in enumerate(g for g in groups if g):
            body = _join(group)
            out.append(Chunk(f"{doc.id}:c{index + 1:02d}", doc.id, index, f"doc:{doc.id}", version, body,
                             count(body)))
    return out
