"""The retriever every LQ retrieval arm shares (domain-2-plan.md, 2 and 7): its scores are held equal across arms, and
the assembler's part is admission and shedding.

BM25 (k1 = 1.2, b = 0.75) over a corpus's chunks, words lower-cased, a short list of stop words left out. A chunk's
relevance is its score divided by the best chunk's, rounded to four places, so the top candidate scores 1.0 and the
spec's `min_relevance` (a fraction) compares with it as a reranker's normalized score would (R-13). The candidates are
the `candidates` best chunks, by relevance and then id; the `rag` baseline and the CWA arms receive the same list.

The query is the question without its options (longcontext.py, `query`), so the short and multiple-choice forms of a
question retrieve alike.
"""
from __future__ import annotations

import math
import re
from collections import Counter

WORD = re.compile(r"[a-z0-9]+")
STOP = frozenset("a an and are at by can did do does for from has how in is it of on or the to what which who with "
                 "more most".split())
K1, B = 1.2, 0.75


def terms(text: str) -> list[str]:
    return [w for w in WORD.findall(text.lower()) if w not in STOP]


class Index:
    def __init__(self, chunks: list[dict]):
        self.ids = [c["id"] for c in chunks]
        self.counts = [Counter(terms(c["body"])) for c in chunks]
        self.lengths = [sum(c.values()) for c in self.counts]
        self.average = sum(self.lengths) / len(self.lengths) if self.lengths else 0.0
        frequency = Counter(t for c in self.counts for t in c)
        n = len(chunks)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in frequency.items()}

    def scores(self, query: str) -> list[float]:
        wanted = set(terms(query))
        out = []
        for counts, length in zip(self.counts, self.lengths):
            score = 0.0
            for term in wanted:
                tf = counts.get(term, 0)
                if tf:
                    score += self.idf[term] * tf * (K1 + 1) / (tf + K1 * (1 - B + B * length / self.average))
            out.append(score)
        return out


def retrieve(index: Index, query: str, candidates: int) -> list[tuple[str, float]]:
    """The `candidates` best chunks as (chunk id, relevance), best first: relevance descending, then id."""
    scores = index.scores(query)
    best = max(scores, default=0.0)
    if best <= 0:
        return []
    scored = [(chunk_id, round(score / best, 4)) for chunk_id, score in zip(index.ids, scores) if score > 0]
    scored.sort(key=lambda pair: (-pair[1], pair[0]))
    return scored[:candidates]
