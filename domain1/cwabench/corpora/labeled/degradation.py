"""labeled.degradation: a route that requires evidence, with min_included 3 in knowledge and four chunks, loses
k = 0…4 of them by one mechanism at a time (SPEC.md R-12; conformance/README.md, Refusals). The outcome must flip
to evidence_required exactly when fewer than three survive, with the recovery action the mechanism implies:
request_context when nothing was omitted for budget, precompute_summary when an omitted chunk had no variants, and
retrieve_narrower when every omitted chunk had variants and they did not fit."""
from __future__ import annotations

from ...contract import Contract
from . import Label, compressed, excluded, kept, labeled
from .builder import T, Builder, cost
from .faults import move, recorded_id

CORPUS = "labeled.degradation"
KB = "evidence.knowledge"
N, M = 4, 3
LONG = " ".join(f"word{i}" for i in range(10))  # ten tokens: an item costs 13
SHORT = " ".join(f"word{i}" for i in range(9))  # its variant: nine tokens, 12 with the wrapper


def _chunks(b: Builder, text: bool = False, variants: bool = False):
    chunks = []
    for n in range(N):
        fields = {"relevance": 0.9 - n / 100, "source": "src:doc"}
        if variants:
            fields["variants"] = [{"id": f"kb:c{n}~short", "body": f"c{n} " + SHORT[SHORT.index(" ") + 1:],
                                   "method": "summary", "lineage": "summarised"}]
        chunks.append(b.item(f"kb:c{n}", KB, f"c{n} " + LONG[LONG.index(" ") + 1:] if text else None, **fields))
    return chunks  # ranked: kb:c0 highest, kb:c3 lowest


MECHANISMS = {}


def mechanism(name, max_k=N):
    def wrap(fn):
        MECHANISMS[name] = (fn, max_k)
        return fn
    return wrap


@mechanism("below_threshold")
def _(b, chunks, k):
    b.rule(KB, min_relevance=0.8)
    for c in chunks[N - k:]:
        c["relevance"] = 0.5
    return {c["id"]: excluded("below_threshold", slot=KB) for c in chunks[N - k:]}


@mechanism("not_eligible")
def _(b, chunks, k):
    b.rule(KB, max_age_seconds=3600)
    for c in chunks[N - k:]:
        c["freshness"] = "2026-09-22T09:00:00Z"
    return {c["id"]: excluded("not_eligible", slot=KB) for c in chunks[N - k:]}


@mechanism("out_of_scope")
def _(b, chunks, k):
    for c in chunks[N - k:]:
        c["scope"] = {"tenant": "globex"}
    return {c["id"]: excluded("out_of_scope", slot=KB) for c in chunks[N - k:]}


@mechanism("revoked")
def _(b, chunks, k):
    for c in chunks[N - k:]:
        c["revoked_by"] = "turn:0"
    return {c["id"]: excluded("revoked", slot=KB) for c in chunks[N - k:]}


@mechanism("expired")
def _(b, chunks, k):
    for c in chunks[N - k:]:
        c["expires"] = T
    return {c["id"]: excluded("expired", slot=KB) for c in chunks[N - k:]}


@mechanism("unauthenticated")
def _(b, chunks, k):
    for c in chunks[N - k:]:
        move(b, c, "rogue", "retrieval")
    return {c["id"]: excluded("producer_not_authenticated", slot=KB) for c in chunks[N - k:]}


@mechanism("producer_suppressed")
def _(b, chunks, k):
    for c in chunks[N - k:]:
        b.batches["kb"]["items"] = [i for i in b.batches["kb"]["items"] if i is not c]
        b.exclude("kb", c["id"], "below_threshold")
    return {}


@mechanism("duplicate_content", max_k=N - 1)
def _(b, chunks, k):
    b.rule(KB, dedupe="exact")
    for c in chunks[N - k:]:
        c["body"] = chunks[0]["body"]
    return {c["id"]: excluded("duplicate_content", duplicate_of="kb:c0", slot=KB) for c in chunks[N - k:]}


@mechanism("source_diversity_cap", max_k=N - 1)
def _(b, chunks, k):
    b.rule(KB, max_per_source=N - k)
    return {c["id"]: excluded("source_diversity_cap", slot=KB) for c in chunks[N - k:]}


@mechanism("superseded", max_k=N - 1)
def _(b, chunks, k):
    b.rule(KB, supersede="source")
    chunks[0]["freshness"] = "2026-09-22T11:59:50Z"
    for c in chunks[1:]:
        c["source"] = f"src:{c['id']}"  # its own call, unless it is one of the k to supersede
    for c in chunks[N - k:]:
        c["source"] = chunks[0]["source"]
        c["freshness"] = "2026-09-22T11:00:00Z"
    return {c["id"]: excluded("superseded", superseded_by="kb:c0", slot=KB) for c in chunks[N - k:]}


def _budget_case(contract, k: int, variants: bool):
    name = f"budget-{'variants' if variants else 'no-variants'}-k{k}"
    b = Builder(contract, name)
    b.base()
    b.route["requires_evidence"] = True
    b.rule(KB, min_included=M)
    chunks = _chunks(b, text=True, variants=variants)
    base = sum(cost(i["body"]) for i in b.items() if i["slot"] != KB)
    # Room for exactly N - k whole chunks. Variants save one token each, never enough for another chunk, so when
    # k > 0 every chunk is compressed and the k lowest-ranked are still omitted.
    b.budget["input"] = base + (N - k) * cost(chunks[0]["body"])
    fates = {recorded_id(b, i): kept() for i in b.items()}
    for c in chunks[N - k:]:
        fates[c["id"]] = excluded("over_budget", slot=KB)
    if variants and k:
        for c in chunks[:N - k]:
            fates[c["id"]] = compressed(f"{c['id']}~short")
    refused = N - k < M
    label = Label(outcome="refused" if refused else "assembled", refusal="evidence_required" if refused else None,
                  recovery=("retrieve_narrower" if variants else "precompute_summary") if refused else None,
                  fates=fates, notes=f"{k} chunk(s) omitted for budget, {'with' if variants else 'without'} variants")
    return labeled(CORPUS, name, b.bytes(), label, ("R-12", "R-16"))


def build(contract: Contract):
    out = []
    for name, (fn, max_k) in MECHANISMS.items():
        for k in range(max_k + 1):
            b = Builder(contract, f"{name}-k{k}")
            b.base()
            b.route["requires_evidence"] = True
            b.rule(KB, min_included=M)
            chunks = _chunks(b)
            changes = fn(b, chunks, k)
            fates = {recorded_id(b, i): kept() for i in b.items()}
            fates.update(changes)
            refused = N - k < M
            label = Label(outcome="refused" if refused else "assembled",
                          refusal="evidence_required" if refused else None,
                          recovery="request_context" if refused else None, fates=fates,
                          notes=f"{k} of {N} chunks lost to {name}; min_included {M}")
            out.append(labeled(CORPUS, f"{name}-k{k}", b.bytes(), label, ("R-12",)))
    for k in range(N + 1):
        for variants in (False, True):
            out.append(_budget_case(contract, k, variants))
    # No min_included: any one evidence item is enough, none is not.
    for k in (0, 1):
        b = Builder(contract, f"no-min-included-k{k}")
        b.base()
        b.route["requires_evidence"] = True
        chunk = b.item("kb:only", KB)
        fates = {recorded_id(b, i): kept() for i in b.items()}
        if k:
            chunk["revoked_by"] = "turn:0"
            fates["kb:only"] = excluded("revoked", slot=KB)
        out.append(labeled(CORPUS, f"no-min-included-k{k}", b.bytes(), Label(
            outcome="refused" if k else "assembled", refusal="evidence_required" if k else None,
            recovery="request_context" if k else None, fates=fates,
            notes="a route requiring evidence with no min_included needs one evidence item"), ("R-12",)))
    return out
