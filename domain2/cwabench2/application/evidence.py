"""The CWA arms of the LQ family (domain-2-plan.md, 6.5 and 7): a question over a corpus, its retrieved chunks as
evidence, frozen as a snapshot, and what assembly must make of it.

**The route** is the specification's `long-context-qa` (examples/route-policies.json): its rule for
evidence.knowledge (`min_relevance` 0.2, `min_included` 1, the tenant scope, exact deduplication, at most 8 chunks a
source) and `requires_evidence`, read from the pinned checkout on every run. It is named for this benchmark, because
its producers are the benchmark's own (the policy registry, the conversation for the query, a document index of kind
`retrieval`) and the graders are a downstream parser (`parser: true`).

**The arms.** The spec publishes one profile for the route, `long-context-reinforced`, which places the
instructions twice: as the system message and again just before the query. `cwa-reinforced` uses its placement as
published. `cwa-evidence` is the same placement with the second occurrence of the instructions removed, so the two
differ in the reinforcement alone.

**The snapshot.** The instructions and output contract (protected), the question as the query (protected) and each
retrieved candidate as its own evidence.knowledge item (application/retrieval.py), its relevance the retriever's,
its source the document it comes from. Every candidate is admissible but for the route's own rules.

**The prediction.** Evidence ranks by the default `order_by` (`-relevance`, `-freshness`, then id; every chunk is
equally fresh). Admission excludes chunks below `min_relevance` (`below_threshold`); exact duplicates go next, then
any chunk past `max_per_source` for its document, in rank order (`source_diversity_cap`). Fitting omits the
lowest-ranked of the rest until the payload fits, so the kept evidence is the longest prefix of that ranking that
fits (conformance/README.md, Fitting). With fewer than `min_included` chunks left, assembly refuses with
`evidence_required`; with the protected items alone over budget, with `protected_content_over_budget`. Evidence renders
in id order, the corpus's own (each chunk's id is its document's and position).
"""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from datetime import timedelta

from cwabench.canon import render

from . import writers
from .snapshots import Expected
from .writers import START, TENANT, USER

SPEC_PROFILE = "long-context-reinforced"
SPEC_ROUTE = "long-context-qa"
ROUTE = "cwa-bench-lq"
ARMS = {"cwa-evidence": False, "cwa-reinforced": True}  # arm → whether the instructions are placed twice
PRODUCERS = {
    "policy-registry": {"kind": "policy", "slots": ["governance.instructions", "governance.output_contract"]},
    "conversation": {"kind": "interaction", "slots": ["interaction.query"]},
    "document-index": {"kind": "retrieval", "slots": ["evidence.knowledge"]},
}
ASKED = START + timedelta(hours=1)  # the documents are indexed at START; the question is asked an hour later


class ProfileError(Exception):
    pass


def _spec(contract, name: str, key: str, value: str) -> dict:
    documents = json.loads((contract.path / "examples" / name).read_text(encoding="utf-8"))
    found = [d for d in documents if d.get(key) == value]
    if len(found) != 1:
        raise ProfileError(f"examples/{name} no longer has exactly one {value}")
    return copy.deepcopy(found[0])


def placement(contract, reinforced: bool) -> list[dict]:
    """The spec's reinforced placement, or the same with its second occurrence of the instructions removed."""
    placed = _spec(contract, "profiles.json", "id", SPEC_PROFILE)["placement"]
    if reinforced:
        return placed
    at = [n for n, p in enumerate(placed) if p["slot"] == "governance.instructions"]
    if len(at) != 2:
        raise ProfileError(f"{SPEC_PROFILE} no longer places the instructions twice")
    return [p for n, p in enumerate(placed) if n != at[1]]


def route_policy(contract) -> dict:
    spec = _spec(contract, "route-policies.json", "route", SPEC_ROUTE)
    return {
        "route": ROUTE,
        "version": "cwa-bench-d2/lq/v1",
        "clock_skew_seconds": 5,
        "parser": True,
        "requires_evidence": bool(spec.get("requires_evidence")),
        "producers": copy.deepcopy(PRODUCERS),
        "slots": {"evidence.knowledge": spec["slots"]["evidence.knowledge"]},
    }


def profile(contract, route: dict, reinforced: bool) -> dict:
    return {
        "spec": "cwa/draft",
        "id": f"{ROUTE}-reinforced" if reinforced else ROUTE,
        "version": 1,
        "route": route["route"],
        "model_family": None,
        "route_policy_version": route["version"],
        "placement": placement(contract, reinforced),
        "evaluation": {"status": "unevaluated", "suite": None, "date": None, "result": None, "artifact": None},
    }


def check(contract) -> list[str]:
    """Problems with the LQ arms' route and profiles against the pinned spec; empty when they pass."""
    problems = []
    try:
        route = route_policy(contract)
        documents = [("route_policy.schema.json", route)]
        documents += [("profile.schema.json", profile(contract, route, r)) for r in ARMS.values()]
    except (ProfileError, KeyError) as error:
        return [str(error)]
    for name, document in documents:
        errors = list(contract.validator(name).iter_errors(document))
        if errors:
            problems.append(f"{name}: {errors[0].message[:200]}")
    used = {slot for producer in route["producers"].values() for slot in producer["slots"]}
    for reinforced in ARMS.values():
        missing = sorted(used - {p["slot"] for p in placement(contract, reinforced)})
        if missing:
            problems.append(f"the placement does not place {', '.join(missing)}")
    return problems


@dataclass
class Frozen:
    """One LQ arm's snapshot for one question, without its budget, and what any budget must make of it."""

    snapshot: dict
    protected: list[dict]
    ranked: list[dict]  # the evidence admission and resolution leave, highest rank first: what fitting sheds from
    excluded: dict[str, str]  # item id → why admission or resolution excluded it
    carriers: dict[str, list[str]]  # chunk id → the item ids carrying it
    evidence: dict[str, list[str]]  # chunk id → the sentences carrying the answer
    min_included: int = 1
    full: int = 0
    floor: int = 0

    def at(self, budget: int) -> bytes:
        snapshot = dict(self.snapshot, budget=dict(self.snapshot["budget"], input=budget))
        return json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def charged(self, items: list[dict]) -> int:
        count = render.render(self.snapshot, items).count(self.snapshot["tokenizer"])
        return render.charged(count, self.snapshot["budget"].get("margin_percent", 0))

    def expect(self, budget: int) -> Expected:
        if self.floor > budget:
            return Expected("refused", "protected_content_over_budget", frozenset(), None, None)
        low, high = 0, len(self.ranked)  # the longest prefix that fits; a longer one never costs less
        while low < high:
            middle = (low + high + 1) // 2
            low, high = (middle, high) if self.charged(self.protected + self.ranked[:middle]) <= budget else (
                low, middle - 1)
        if low < self.min_included:
            return Expected("refused", "evidence_required", frozenset(), None, None)
        items = self.protected + self.ranked[:low]
        rendered = render.render(self.snapshot, items)
        return Expected("assembled", None, frozenset(i["id"] for i in items), rendered.payload,
                        rendered.count(self.snapshot["tokenizer"]))


def item_id(chunk_id: str) -> str:
    return f"chunk:{chunk_id}"


def freeze(contract, corpus: dict, question: dict, candidates: list[tuple[str, float]], reinforced: bool,
           settings) -> Frozen:
    """The snapshot for `question` with the retriever's `candidates` as evidence; `settings` are the application's
    (snapshots.Settings: reserved output, margin, tokenizer, renderer)."""
    chunks = {c["id"]: c for c in corpus["chunks"]}
    sources = {d["id"]: d for d in corpus["documents"]}
    governance = [
        writers.item(contract, "policy:instructions", "governance.instructions", corpus["instructions"],
                     source="policy-registry:instructions", freshness=START, trust="verified"),
        writers.item(contract, "policy:output-contract", "governance.output_contract", corpus["output_contract"],
                     source="policy-registry:output-contract", freshness=START, trust="verified"),
    ]
    query = writers.item(contract, f"question:{question['question_id']}", "interaction.query", question["question"],
                         source=f"question:{question['question_id']}", freshness=ASKED)
    evidence_items = []
    for chunk_id, relevance in candidates:
        c = chunks[chunk_id]
        evidence_items.append(writers.item(
            contract, item_id(chunk_id), "evidence.knowledge", c["body"], source=c["source"],
            source_version=sources[c["document"]]["source_version"], freshness=START, relevance=relevance,
            scope={"tenant": TENANT}))
    route = route_policy(contract)
    snapshot = {
        "assembly_time": writers.instant(ASKED),
        "scope": {"tenant": TENANT, "user": USER, "session": corpus["corpus_id"], "task": corpus["corpus_id"]},
        "budget": {"input": 0, "reserved_output": settings.reserved_output, "margin_percent": settings.margin_percent},
        "profile": profile(contract, route, reinforced),
        "route_policy": route,
        "tokenizer": settings.tokenizer,
        "renderer": settings.renderer,
        "batches": [
            {"producer": {"id": "policy-registry", "kind": "policy"}, "items": governance, "excluded": []},
            {"producer": {"id": "conversation", "kind": "interaction"}, "items": [query], "excluded": []},
            {"producer": {"id": "document-index", "kind": "retrieval"}, "items": evidence_items, "excluded": []},
        ],
        "conflicts": [],
    }

    rule = route["slots"]["evidence.knowledge"]
    from cwabench.canon.strings import utf16_key

    ranked = sorted(sorted(evidence_items, key=lambda i: utf16_key(i["id"])), key=lambda i: -i["relevance"])
    excluded: dict[str, str] = {}
    kept, bodies, per_source = [], set(), {}
    for entry in ranked:
        if entry["relevance"] < rule.get("min_relevance", 0):
            excluded[entry["id"]] = "below_threshold"
            continue
        key = " ".join(entry["body"].split())
        if rule.get("dedupe") == "exact" and key in bodies:
            excluded[entry["id"]] = "duplicate_content"
            continue
        bodies.add(key)
        cap = rule.get("max_per_source")
        if cap is not None and per_source.get(entry["source"], 0) >= cap:
            excluded[entry["id"]] = "source_diversity_cap"
            continue
        per_source[entry["source"]] = per_source.get(entry["source"], 0) + 1
        kept.append(entry)
    carriers = {need: [item_id(need)] for need in question["needs"]}
    frozen = Frozen(snapshot, [*governance, query], kept, excluded, carriers,
                    {need: list(texts) for need, texts in question["evidence"].items()},
                    int(rule.get("min_included", 1)) if route["requires_evidence"] else 0)
    frozen.floor = frozen.charged(frozen.protected)
    frozen.full = frozen.charged(frozen.protected + frozen.ranked)
    snapshot["budget"]["input"] = frozen.full
    return frozen
