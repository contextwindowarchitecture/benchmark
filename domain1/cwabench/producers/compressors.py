"""Deterministic stub compressors (domain-1-plan.md, 9.1, mode `stub`): lead-N, first-sentence and extractive.

Each takes text only from its parent, so a stub variant can never add a fact: every one is a prefix of the parent or
a selection of its sentences. They need no network and give the same bytes on every machine, which is what lets every
suite and CI run with variants present. Their `method` strings name the stub and its parameter, so traces show it.
"""
from __future__ import annotations

import re
from collections import Counter

from ..canon.strings import WHITESPACE
from ..canon.tokenizers import TOKENIZERS
from .chunker import sentences

LINEAGE = "extracted"  # every stub selects parent text, never rewords it
_WORD = re.compile(r"[a-z0-9]+")
_STOP = frozenset("a an and are as at be by for from has had have in is it its of on or that the this to was were "
                  "will with which after before all any than then".split())


def lead(body: str, n: int) -> str:
    """The parent's text up to the end of its n-th whitespace-separated word, original spacing kept."""
    words = 0
    inside = False
    for i, char in enumerate(body):
        if char in WHITESPACE:
            if inside:
                words += 1
                if words == n:
                    return body[:i]
            inside = False
        else:
            inside = True
    return body


def first_sentence(body: str) -> str:
    found = sentences(body)
    return found[0] if found else body


def extractive(body: str, ratio: float, tokenizer: str = "fixture-whitespace/v1") -> str:
    """The highest-scoring sentences, in their original order, within `ratio` of the parent's tokens (at least one).
    A sentence scores the mean corpus frequency of its content words, within this parent; ties keep the earlier."""
    count = TOKENIZERS[tokenizer]
    found = [s for p in body.split("\n\n") for s in sentences(p)]
    if len(found) <= 1:
        return body
    frequency = Counter(w for w in _WORD.findall(body.lower()) if w not in _STOP)

    def score(sentence: str) -> float:
        words = [w for w in _WORD.findall(sentence.lower()) if w not in _STOP]
        return sum(frequency[w] for w in words) / len(words) if words else 0.0

    limit = max(1, int(count(body) * ratio))
    ranked = sorted(range(len(found)), key=lambda i: (-score(found[i]), i))
    chosen: list[int] = []
    for i in ranked:
        if not chosen or count(" ".join(found[j] for j in sorted(chosen + [i]))) <= limit:
            chosen.append(i)
    return " ".join(found[i] for i in sorted(chosen))


def stub_variants(body: str, settings: dict, tokenizer: str = "fixture-whitespace/v1") -> list[tuple[str, str]]:
    """(method, body) for each stub, in a fixed order; R-18's variant rules are applied afterwards (freeze.py)."""
    n = int(settings.get("lead_words", 24))
    ratio = float(settings.get("extractive_ratio", 0.4))
    return [
        (f"lead-n/v1 n={n}", lead(body, n)),
        ("first-sentence/v1", first_sentence(body)),
        (f"extractive/v1 ratio={ratio}", extractive(body, ratio, tokenizer)),
    ]
