"""What S4 and S5 share: answers cached by snapshot, compact answer summaries, findings deduplicated by signature, and
minimization of each finding into a conformance-case draft (domain-1-plan.md, sections 7.3 and 10).

Generated corpora are large, so rows summarize each answer by hashes, and blobs (snapshots, traces, payloads) are
stored only for answers that are part of a finding. A finding is one signature, however many snapshots show it: its
smallest reproducer is minimized, and the reduced snapshot is written out as a case directory the spec could adopt.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from .. import adapters as adapters_mod
from .. import output, traces
from ..adapters import FAULTS, Outcome
from ..canon import jcs
from ..minimize import Minimizer
from ..oracles.auditor import audit
from . import SuiteContext

OPTIONAL_RENDERERS = ("cwa-message-blocks/v1",)
# Requirements a rejection exercises, by the snapshot check it breaks (conformance/README.md, Snapshot checks).
CHECK_RULES = {"json": ["R-17"], "i-json": ["R-17"], "schema": ["R-17"], "one-batch-per-producer": ["R-15", "R-17"],
               "conflict-groups": ["R-11", "R-17"], "producer-exclusions": ["R-9", "R-13", "R-17"],
               "profile": ["R-19", "R-20", "R-17"], "realizable": ["R-7", "R-17"]}

# Requirements each audit check serves (domain-1-plan.md, section 6), so an auditor finding's draft names them.
AUDIT_RULES = {"A1": ["R-21"], "A2": ["R-9", "R-21"], "A3": ["R-17"], "A4": ["R-21", "R-22"], "A5": ["R-16"],
               "A6": ["R-16", "R-17"], "A7": ["R-16"], "A8": ["R-18"], "A9": ["R-7", "R-21"], "A10": ["R-21", "R-22"],
               "A11": ["R-11", "R-12"], "A12": ["R-6", "R-11"], "A13": ["R-24", "R-25", "R-26"], "A14": ["R-7", "R-10"],
               "A15": ["R-3", "R-22"], "A16": ["R-20", "R-21", "R-22"]}


def with_rules(rules, extra) -> list[str]:
    return sorted(set(rules) | set(extra), key=lambda r: int(r[2:]))


@dataclass
class Answer:
    adapter: str
    outcome: Outcome
    exit_code: int | None
    wall_ms: float
    audit_failed: list[str] = field(default_factory=list)
    audit_detail: dict[str, list[str]] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return self.outcome.kind

    @property
    def payload_hash(self) -> str | None:
        return hashlib.sha256(self.outcome.payload).hexdigest() if self.outcome.payload is not None else None

    @property
    def trace_hash(self) -> str | None:
        trace = self.outcome.trace
        if not isinstance(trace, dict):
            return None
        try:
            return hashlib.sha256(jcs.serialize_bytes(traces.normalize(trace))).hexdigest()
        except jcs.CanonicalizationError:
            return "uncanonicalizable"

    @property
    def signature(self) -> tuple:
        return self.kind, self.outcome.refusal_reason, self.payload_hash, self.trace_hash

    def summary(self, blobs=None) -> dict:
        """The fuzz-row answer object; with `blobs`, the trace and payload are stored too."""
        trace, payload = self.outcome.trace, self.outcome.payload
        result = trace.get("result") if isinstance(trace, dict) else None
        tokens = result.get("input_tokens") if isinstance(result, dict) else None
        return {
            "adapter": self.adapter, "outcome": self.kind, "exit_code": self.exit_code,
            "refusal_reason": self.outcome.refusal_reason, "payload_hash": self.payload_hash,
            "trace_hash": self.trace_hash,
            "input_tokens": tokens if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0 else None,
            "audit_failed": self.audit_failed, "wall_ms": round(self.wall_ms, 3),
            "problem": (self.outcome.problem or "")[:500] or None,
            "trace": blobs.put_json(trace) if blobs is not None and trace is not None else None,
            "payload": blobs.put_text(payload) if blobs is not None and payload is not None else None,
        }


class Answers:
    """Every adapter's answer to every snapshot asked about, each run once and audited once."""

    def __init__(self, ctx: SuiteContext):
        self.ctx = ctx
        self._cache: dict[tuple[str, str], Answer] = {}

    def _run(self, adapter_name: str, data: bytes) -> Answer:
        adapter = self.ctx.adapters[adapter_name]
        invocation = adapters_mod.invoke(adapter, data, self.ctx.config.timeout_s, self.ctx.config.root)
        outcome = adapters_mod.classify(invocation)
        answer = Answer(adapter_name, outcome, invocation.exit_code, invocation.wall_ms)
        if outcome.kind in ("assembled", "refused") and isinstance(outcome.trace, dict):
            result = audit(self.ctx.contract, data, outcome.payload, outcome.trace)
            answer.audit_failed = result.failed
            answer.audit_detail = {k: result.checks[k].violations for k in result.failed}
        return answer

    def get(self, adapter: str, data: bytes) -> Answer:
        key = (adapter, hashlib.sha256(data).hexdigest())
        if key not in self._cache:
            self._cache[key] = self._run(adapter, data)
        return self._cache[key]

    def fill(self, jobs: list[tuple[str, bytes]], progress: Callable[[int, int], None] | None = None) -> None:
        todo, seen = [], set()
        for adapter, data in jobs:
            key = (adapter, hashlib.sha256(data).hexdigest())
            if key not in self._cache and key not in seen:
                seen.add(key)
                todo.append((key, adapter, data))
        with ThreadPoolExecutor(max_workers=self.ctx.config.concurrency) as pool:
            for n, (key, answer) in enumerate(pool.map(lambda j: (j[0], self._run(j[1], j[2])), todo), 1):
                self._cache[key] = answer
                if progress and n % 2000 == 0:
                    progress(n, len(todo))


# Signatures --------------------------------------------------------------------------------------------------------------

_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
_HEX = re.compile(r"\b[0-9a-f]{8,}…?")
_NUMBER = re.compile(r"\d+")


def normalized(text: str | None, limit: int = 160) -> str:
    """Text with its ids, digests and numbers blanked, so the same defect on different snapshots reads the same."""
    if not text:
        return ""
    return _NUMBER.sub("N", _HEX.sub("H", _QUOTED.sub("Q", text.splitlines()[0])))[:limit]


def last_line(text: str | None) -> str:
    """What a crash says about itself: the last non-empty line of its output, where a traceback ends in its error."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def pointer_shape(pointer: str | None) -> str | None:
    """Where two traces first differ, coarsely enough that one defect is one finding: the first two segments, indices
    blanked. A missing row and a reordered one both read /defaults_filled/*."""
    if pointer is None:
        return None
    return "/".join(re.sub(r"^\d+$", "*", part) for part in pointer.split("/")[:3])


def first_difference(a: Answer, b: Answer) -> dict:
    for stage, x, y in (("outcome", a.kind, b.kind), ("outcome", a.outcome.refusal_reason, b.outcome.refusal_reason),
                        ("payload", a.payload_hash, b.payload_hash), ("trace", a.trace_hash, b.trace_hash)):
        if x != y:
            pointer = None
            if stage in ("payload", "trace") and isinstance(a.outcome.trace, dict) and isinstance(b.outcome.trace, dict):
                found = traces.diff(traces.normalize(a.outcome.trace), traces.normalize(b.outcome.trace), limit=1)
                pointer = found[0].pointer if found else None
            return {"stage": stage, "pointer": pointer, "between": [a.adapter, b.adapter]}
    return {"stage": "trace", "pointer": None, "between": [a.adapter, b.adapter]}


def compared(answers: list[Answer], data: bytes) -> list[Answer]:
    """The answers agreement is judged on: not faults, which are findings of their own, and not an optional renderer
    an adapter does not provide."""
    renderer = json.loads(data).get("renderer")
    return [a for a in answers if a.kind not in FAULTS and not (a.kind == "unsupported"
                                                                and renderer in OPTIONAL_RENDERERS)]


def partition(answers: list[Answer]) -> list[list[str]]:
    groups: dict[tuple, list[str]] = defaultdict(list)
    for answer in answers:
        groups[answer.signature].append(answer.adapter)
    return sorted((sorted(g) for g in groups.values()), key=lambda g: (-len(g), g))


def majority(answers: list[Answer]) -> list[Answer] | None:
    """The answers of the largest agreeing group, when at least three adapters agree and their answers audit clean:
    enough to propose an expected output for a draft (domain-1-plan.md, 7.3)."""
    groups: dict[tuple, list[Answer]] = defaultdict(list)
    for answer in answers:
        groups[answer.signature].append(answer)
    best = max(groups.values(), key=len, default=[])
    if len(best) >= 3 and best[0].kind in ("assembled", "refused") and not any(a.audit_failed for a in best):
        return best
    return None


# Findings ----------------------------------------------------------------------------------------------------------------

@dataclass
class Occurrence:
    """One failure seen on one snapshot. Occurrences with the same signature make one finding."""

    suite: str
    oracle: str
    checks: list[str]
    signature: dict
    adapter: str | None
    adapters: list[str]
    summary: str
    first_pointer: str | None
    requirements: list[str]
    case_id: str
    data: bytes  # the snapshot that failed (the variant, for a relation)
    severity: str = "error"
    context: dict = field(default_factory=dict)  # what minimization needs to re-run the oracle

    @property
    def key(self) -> str:
        return hashlib.sha256(output.dumps(self.signature, compact=True).encode()).hexdigest()[:12]


def group(occurrences: list[Occurrence]) -> dict[str, list[Occurrence]]:
    out: dict[str, list[Occurrence]] = defaultdict(list)
    for occurrence in occurrences:
        out[occurrence.key].append(occurrence)
    for found in out.values():
        found.sort(key=lambda o: (len(o.data), o.case_id))  # the smallest reproducer first
    return dict(out)


def finding(ctx: SuiteContext, occurrences: list[Occurrence]) -> dict:
    first = occurrences[0]
    return {
        "$schema": output.schema_name("finding"),
        "finding_id": first.key,
        "run_id": ctx.run.run_id,
        "suite": first.suite,
        "adapter": first.adapter,
        "adapters": first.adapters,
        "case_id": first.case_id,
        "oracle": first.oracle,
        "checks": first.checks,
        "severity": first.severity,
        "summary": f"{first.summary} ({len(occurrences)} snapshot{'s' if len(occurrences) != 1 else ''})"[:1000],
        "first_pointer": first.first_pointer,
        "requirements": first.requirements,
        "occurrences": len(occurrences),
        "reproducer": {"snapshot": ctx.run.blobs.put(first.data, "application/json"), "spec_path": None},
        "signature": first.signature,
        "minimized": None,
    }


# Minimization and drafts -----------------------------------------------------------------------------------------------

@dataclass
class Plan:
    """How to minimize one finding: the document to reduce, the failing snapshot a reduced document yields (or None
    when it no longer applies), and whether that snapshot still fails the same way."""

    document: dict
    materialize: Callable[[dict], bytes | None]
    reproduces: Callable[[bytes], bool]
    draft_kind: str = "case"  # "rejection" for mutants
    expected: Callable[[bytes], list[Answer] | None] | None = None  # the agreeing answers for an expected output
    note: str | None = None
    extra: Callable[[bytes], dict[str, bytes]] | None = None  # more files for the draft, by name
    original: bytes | None = None  # the snapshot that failed, the draft's fallback when no reduction reproduces


def reproducer(occurrence: Occurrence, answers: Answers, names: list[str]) -> Callable[[bytes], bool]:
    """Whether a snapshot still fails the way the occurrence did: the same partition of adapters, the same audit
    violation (ids and numbers aside), or the same outcome."""
    context, adapter = occurrence.context, occurrence.adapter
    if occurrence.oracle == "differential":
        return lambda data: partition(compared([answers.get(a, data) for a in names], data)) == context["groups"]
    if occurrence.oracle == "auditor":
        def audited(data):
            found = answers.get(adapter, data)
            return context["check"] in found.audit_failed and any(
                normalized(v) == context["violation"] for v in found.audit_detail.get(context["check"], []))
        return audited
    return lambda data: answers.get(adapter, data).kind == context["kind"]


def snapshot_plan(ctx: SuiteContext, occurrence: Occurrence, answers: Answers, names: list[str]) -> Plan:
    """Minimize a failing valid snapshot directly, keeping every reduction valid."""
    from ..canon import validity
    from ..canon.spelling import dump_keeping, load_keeping

    def materialize(document):
        data = dump_keeping(document)  # numbers as the failing snapshot spelled them
        return data if validity.is_valid(ctx.contract, data) else None

    return Plan(load_keeping(occurrence.data), materialize, reproducer(occurrence, answers, names), "case",
                lambda data: majority([answers.get(a, data) for a in names]), original=occurrence.data)


def minimize(ctx: SuiteContext, findings: list[tuple[dict, Plan]], max_tests: int) -> list[dict]:
    """Minimize each finding's reproducer and write minimized/<finding-id>/; returns the minimization documents."""

    def one(item):
        doc, plan = item

        def test(document):
            data = plan.materialize(document)
            return data is not None and plan.reproduces(data)

        result = Minimizer(test, max_tests).run(plan.document)
        data = plan.materialize(result.document)
        reproduces = data is not None and plan.reproduces(data)
        if not reproduces:  # keep the original, which failed when it was found
            data = plan.original if plan.original is not None else plan.materialize(plan.document)
            reproduces = data is not None and plan.reproduces(data)
            result.document, result.after = plan.document, result.before
        return doc, plan, result, data, reproduces

    with ThreadPoolExecutor(max_workers=max(1, ctx.config.concurrency // 2)) as pool:
        done = list(pool.map(one, findings))
    return [_write_draft(ctx, *item) for item in done]


def _write_draft(ctx: SuiteContext, doc: dict, plan: Plan, result, data: bytes | None, reproduces: bool) -> dict:
    fid = doc["finding_id"]
    base = f"minimized/{fid}"
    draft_id = f"bench-{doc['suite'].lower()}-{fid}"
    files = []
    original = doc["reproducer"]["snapshot"]
    data = data or b""
    stored = ctx.run.blobs.put(data, "application/json")
    case = {"id": draft_id, "rules": doc["requirements"] or ["R-23"],
            "description": f"Found by {doc['suite']} ({doc['oracle']}: {', '.join(doc['checks'])}): {doc['summary']}"}
    ctx.run.write_bytes(f"{base}/case.json", (json.dumps(case, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                        "conformance-draft", None, f"{fid}: draft case.json")
    files.append("case.json")
    snapshot_schema = "snapshot.schema.json" if plan.draft_kind == "case" else None
    ctx.run.write_bytes(f"{base}/snapshot.json", data, "conformance-draft", snapshot_schema, f"{fid}: draft snapshot")
    files.append("snapshot.json")
    for name, content in (plan.extra(data) if plan.extra and data else {}).items():
        ctx.run.write_bytes(f"{base}/{name}", content, "conformance-draft", None, f"{fid}: {name}")
        files.append(name)
    expected_from = None
    if plan.draft_kind == "case" and plan.expected is not None and data:
        agreeing = plan.expected(data)
        if agreeing:
            trace = {"trace_id": draft_id, **traces.normalize(agreeing[0].outcome.trace)}
            ctx.run.write_bytes(f"{base}/expected.trace.json",
                                (json.dumps(trace, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
                                "conformance-draft", "trace.schema.json", f"{fid}: draft expected trace")
            files.append("expected.trace.json")
            if agreeing[0].outcome.payload is not None:
                ctx.run.write_bytes(f"{base}/expected.payload.txt", agreeing[0].outcome.payload, "conformance-draft",
                                    None, f"{fid}: draft expected payload")
                files.append("expected.payload.txt")
            expected_from = "agreement of " + ", ".join(a.adapter for a in agreeing) + ", audited clean"
    document = {
        "$schema": output.schema_name("minimization"),
        "run_id": ctx.run.run_id,
        "finding_id": fid,
        "suite": doc["suite"],
        "signature": doc.get("signature") or {},
        "tests": result.tests,
        "reductions": result.reductions,
        "exhausted": result.exhausted,
        "reproduces": reproduces,
        "before": result.before,
        "after": result.after,
        "original": original,
        "minimized": stored,
        "draft": {"kind": plan.draft_kind, "id": draft_id, "path": base, "files": files,
                  "expected_from": expected_from, "note": plan.note},
    }
    ctx.run.write_json(f"{base}/minimization.json", document, f"{fid}: minimization and draft")
    doc["minimized"] = {"path": base, "snapshot": stored, "items_before": result.before.get("items", 0),
                        "items_after": result.after.get("items", 0), "tests": result.tests, "reproduces": reproduces}
    return document

