"""R-18's variant rules, then the freeze: chunks and their kept variants into one snapshot (domain-1-plan.md, 9.2).

A proposed variant is kept only if, in this order:

    empty            it has a non-whitespace character
    not_shorter      it has fewer tokens than its parent under the route's tokenizer (rendered bodies, as fitting
                     counts them)
    duplicate_id     its id is new: ids are `<parent id>#v-<sha8 of the body>`, so two variants with one body share an
                     id, and the later is dropped rather than letting the item fail duplicate_variant_id
    fidelity         it passes every fidelity check (fidelity.py)

The id can never equal the parent's, since it extends it. A kept variant carries its method and lineage; it inherits
its parent's provenance, scope and authority by being the parent's own variant. Each chunk becomes an
evidence.knowledge item (compressible), on a route whose only other items are the protected instructions and query.
"""
from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass, field

from ..canon import render
from ..canon.payloads import escape_body
from ..canon.tokenizers import TOKENIZERS
from ..contract import Contract
from ..corpora.labeled.builder import Builder
from . import fidelity
from .chunker import Chunk

SLOT = "evidence.knowledge"
RULES = ("empty", "not_shorter", "duplicate_id", "fidelity")


def variant_id(parent_id: str, body: str) -> str:
    return f"{parent_id}#v-{hashlib.sha256(body.encode('utf-8')).hexdigest()[:8]}"


def tokens(body: str, tokenizer: str) -> int:
    return TOKENIZERS[tokenizer](escape_body(body))


@dataclass
class Candidate:
    """One proposed variant of one chunk, and what the rules made of it."""

    chunk: str
    arm: str
    sample: int
    method: str
    lineage: str
    body: str
    tokens: int
    parent_tokens: int
    checks: list[dict] = field(default_factory=list)
    cache: dict | None = None  # {"hit", "key", "lookup_ms"} for a summarizer variant
    dropped: str | None = None

    @property
    def id(self) -> str:
        return variant_id(self.chunk, self.body)

    @property
    def kept(self) -> bool:
        return self.dropped is None

    def as_variant(self) -> dict:
        return {"id": self.id, "body": self.body, "method": self.method, "lineage": self.lineage}


def enforce(chunk: Chunk, proposed: list[Candidate], band: tuple[float, float], tokenizer: str) -> list[Candidate]:
    """Apply the rules to one chunk's proposals for one arm and sample, in order. Every proposal is returned, each
    with its fidelity checks and, when it is not kept, the first rule it broke."""
    parent_tokens = tokens(chunk.body, tokenizer)
    seen: set[str] = set()
    for candidate in proposed:
        candidate.checks = fidelity.check(chunk.body, candidate.body, parent_tokens, candidate.tokens, band)
        if not candidate.body.strip():
            candidate.dropped = "empty"
        elif candidate.tokens >= parent_tokens:
            candidate.dropped = "not_shorter"
        elif candidate.id in seen:
            candidate.dropped = "duplicate_id"
        elif not fidelity.passed(candidate.checks):
            candidate.dropped = "fidelity"
        else:
            seen.add(candidate.id)
    return proposed


def relevance(chunk: Chunk, seed: int) -> float:
    """A retriever's score, fixed per chunk and seed, so ranks (and so shedding order) are reproducible."""
    return round(random.Random(f"{seed}:{chunk.id}").uniform(0.5, 0.99), 3)


@dataclass
class Frozen:
    """A frozen snapshot, with the counts that decide its outcome at any budget."""

    name: str
    snapshot: dict
    evidence: list[str]  # chunk ids, in snapshot order
    full: int  # the payload's count with every item whole
    protected: int  # with only the protected items: below it, assembly must refuse

    def at(self, budget: int) -> bytes:
        snapshot = dict(self.snapshot, budget=dict(self.snapshot["budget"], input=budget))
        return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def expect(self, budget: int) -> tuple[str, str | None]:
        """No route rule here can refuse but the protected items': every evidence item can be reduced or omitted."""
        if render.charged(self.protected) > budget:
            return "refused", "protected_content_over_budget"
        return "assembled", None


def freeze(contract: Contract, name: str, chunks: list[Chunk], variants: dict[str, list[dict]], seed: int,
           tokenizer: str = "fixture-whitespace/v1") -> Frozen:
    b = Builder(contract, name)
    b.tokenizer = tokenizer
    b.base()
    protected_items = list(b.items())
    for chunk in chunks:
        b.item(chunk.id, SLOT, chunk.body, source=chunk.source, source_version=chunk.source_version,
               relevance=relevance(chunk, seed), variants=list(variants.get(chunk.id, [])))
    snapshot = b.snapshot()
    full = render.render(snapshot, b.items()).count(tokenizer)
    protected = render.render(snapshot, protected_items).count(tokenizer)
    snapshot["budget"] = dict(snapshot["budget"], input=full)
    return Frozen(name, snapshot, [c.id for c in chunks], full, protected)
