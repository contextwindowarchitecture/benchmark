"""S7 · Budget pressure at scale (domain-1-plan.md, 7.7 and section 8).

Four parts, all on snapshots from corpora/scale.py, whose outcome and refusal code the harness knows from the rules
and its own renderer before any adapter runs:

1. **Grid.** Every shape × candidates × candidate tokens at each budget ratio, one below the protected threshold, and a
   few fixed windows; plus each shape under every tokenizer and renderer at one size. Each answer is judged against
   the predicted outcome, for protected preservation, by the auditor, and by agreement. Within one adapter, shape and
   budget, cells run from the least work (candidates × tokens) up; once one times out, every cell with at least as
   much work is skipped for that adapter, shape and budget, and listed as skipped.
2. **Threshold search.** For each shape, a binary search per adapter for the smallest budget that assembles, never
   told the answer, which must equal the harness's protected-only charged count (or, for the floors shape, the
   floored one).
3. **Sweeps.** For each shape at one small size, every budget from the full size down to refusal in steps, refined to
   exact single-token boundaries wherever the answer changes: the shedding curve. Written as sweep frames, with
   timelines at a few budgets.
4. **Performance.** Serially, one invocation at a time: each adapter's startup on a minimal snapshot, then end-to-end,
   net and in-process times with peak RSS on three series per shape (more candidates at fixed tokens, more tokens at
   fixed candidates, and both together at fixed tokens per candidate), a log-log scaling exponent per series, shape
   and budget, throughput at concurrency 1 and N, and time to refusal.

Timeouts are performance findings (warnings), never correctness failures.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from .. import adapters as adapters_mod
from .. import metrics, output, perf, timelines, traces
from ..adapters import FAULTS
from ..canon import payloads
from ..corpora import scale
from ..corpora.labeled.builder import Builder
from ..oracles.auditor import audit
from ..rundir import now
from . import Coverage, SuiteContext, SuiteResult
from .generated import (AUDIT_RULES, OPTIONAL_RENDERERS, Answer, Answers, Occurrence, compared, finding,
                        first_difference, group, minimize, normalized, partition, pointer_shape, snapshot_plan,
                        with_rules)

ID = "S7"
TITLE = "Budget pressure at scale"
REQUIREMENTS = ["R-16", "R-17", "R-18", "R-21", "R-22", "R-23"]
SENTINEL = 9007199254740991  # budget.input in the cached template, replaced per budget; nothing else spells it

DEFAULTS = {
    "timeout_s": 30,
    "candidates": [10, 100, 500, 1000, 5000, 10000],
    "candidate_tokens": [10000, 100000, 500000, 2000000],
    "budget_ratio": [1.0, 0.5, 0.25, 0.1, 0.01],
    "below_threshold": True,
    "components_cell": [500, 100000],
    "components_ratio": [1.0, 0.5, 0.1],
    "window_cell": [1000, 500000],
    "window_shapes": ["compressible-v1"],
    "windows": [8192, 32768, 131072, 200000, 1000000],
    "threshold_cells": [[100, 10000], [1000, 20000]],
    "sweep_cell": [40, 2000],
    "sweep_step_percent": 2,
    "timeline_points": [0.75, 0.5, 0.25],
    "perf": True,
    "perf_candidates": [50, 100, 200, 400],
    "perf_tokens_per_candidate": 250,
    "perf_fixed_tokens": 50000,
    "perf_fixed_candidates": 100,
    "perf_labels": ["ratio:1.0", "ratio:0.1", "threshold-1"],
    "perf_min_samples": 10,
    "perf_max_samples": 30,
    "perf_max_seconds": 4,
    "perf_ci_target": 0.05,
    "inprocess": True,
    "throughput_cell": [100, 10000],
    "throughput_invocations": 32,
    "max_tests": 100,
}


def _settings(ctx) -> dict:
    return {**DEFAULTS, **ctx.config.section("s7")}


def _ratio_label(ratio: float) -> str:
    return f"ratio:{float(ratio)}"


# Cell cache ----------------------------------------------------------------------------------------------------------

@dataclass
class Meta:
    """What the harness knows about one built cell; the snapshot itself lives in the cache file."""

    cell: scale.Cell
    path: Path
    full: int
    protected: int
    floor: int | None
    protected_items: list[tuple[str, str]]  # (id, body)
    items: int
    bytes: int

    @property
    def work(self) -> int:
        return self.cell.candidates * self.cell.tokens

    def expect(self, budget: int) -> tuple[str, str | None]:
        if budget < self.protected:
            return "refused", "protected_content_over_budget"
        if self.floor is not None and budget < self.floor:
            return "refused", "slot_floor_over_budget"
        return "assembled", None

    def at(self, budget: int) -> bytes:
        data = self.path.read_bytes()
        marker = f'"input":{SENTINEL},'.encode()
        assert data.count(marker) == 1
        return data.replace(marker, f'"input":{max(1, int(budget))},'.encode())

    def budget(self, label: str) -> int:
        kind, _, value = label.partition(":")
        if kind == "ratio":
            return max(1, round(self.full * float(value)))
        if kind == "window":
            return int(value)
        if kind == "threshold-1":
            return max(1, self.protected - 1)
        raise ValueError(label)


class CellCache:
    """Builds each cell once, to a file under .build, so concurrent chains share it without holding it in memory."""

    def __init__(self, ctx: SuiteContext):
        self.ctx = ctx
        self.dir = ctx.config.build_dir / "s7" / ctx.run.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._meta: dict[scale.Cell, Meta | str] = {}
        self._locks: dict[scale.Cell, threading.Lock] = defaultdict(threading.Lock)
        self._guard = threading.Lock()

    def get(self, cell: scale.Cell) -> Meta | str:
        """The cell's Meta, or why it cannot be built."""
        with self._guard:
            lock = self._locks[cell]
        with lock:
            if cell not in self._meta:
                self._meta[cell] = self._build(cell)
            return self._meta[cell]

    def _build(self, cell: scale.Cell) -> Meta | str:
        try:
            built = scale.build(self.ctx.contract, cell)
        except scale.NotConstructible as error:
            return str(error)
        data = built.at(SENTINEL)
        name = hashlib.sha256(cell.key.encode()).hexdigest()[:16] + ".json"
        path = self.dir / name
        path.write_bytes(data)
        return Meta(cell, path, built.full, built.protected, built.floor,
                    [(i["id"], i["body"]) for i in built.protected_items], len(built.all_items), len(data))

    def close(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


# Judging one answer ---------------------------------------------------------------------------------------------------

@dataclass
class Judged:
    """One adapter's answer at one cell and budget, kept small: hashes, checks, counts."""

    adapter: str
    answer: dict  # the fuzz-row-style answer summary
    expected: tuple[str, str | None]
    prediction: str  # "pass" | "fail" | "skipped" (an optional renderer not provided, or a fault)
    protected: dict | None  # {"admitted", "preserved"} for an assembled answer
    reasons: list[tuple[str, str | None]] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    audit_detail: dict = field(default_factory=dict)
    self_reported_ms: float | None = None

    @property
    def signature(self) -> tuple:
        a = self.answer
        return a["outcome"], a["refusal_reason"], a["payload_hash"], a["trace_hash"]


class Judge:
    def __init__(self, ctx: SuiteContext):
        self.ctx = ctx
        self._audits: dict[tuple, tuple[list[str], dict]] = {}
        self._lock = threading.Lock()

    def audit(self, data: bytes, outcome, payload_hash, trace_hash) -> tuple[list[str], dict]:
        """Audited once per distinct answer: adapters that agree byte for byte share the result."""
        key = (hashlib.sha256(data).hexdigest(), payload_hash, trace_hash)
        with self._lock:
            if key in self._audits:
                return self._audits[key]
        result = audit(self.ctx.contract, data, outcome.payload, outcome.trace)
        value = (result.failed, {k: result.checks[k].violations for k in result.failed})
        with self._lock:
            self._audits[key] = value
        return value

    def judge(self, adapter: str, meta_expect, protected_items, renderer: str, data: bytes, invocation) -> Judged:
        outcome = adapters_mod.classify(invocation)
        answer = Answer(adapter, outcome, invocation.exit_code, invocation.wall_ms)
        trace = outcome.trace if isinstance(outcome.trace, dict) else None
        payload_hash, trace_hash = answer.payload_hash, answer.trace_hash
        audit_failed, detail = [], {}
        if outcome.kind in ("assembled", "refused") and trace is not None:
            audit_failed, detail = self.audit(data, outcome, payload_hash, trace_hash)
            answer.audit_failed = audit_failed
        summary = answer.summary()
        summary["charged_tokens"] = traces.charged_tokens(trace) if trace else None
        if outcome.kind in FAULTS or (outcome.kind == "unsupported" and renderer in OPTIONAL_RENDERERS):
            prediction = "skipped"
        else:
            prediction = "pass" if (outcome.kind, outcome.refusal_reason) == meta_expect else "fail"
        protected = None
        if outcome.kind == "assembled" and outcome.payload is not None:
            try:
                parsed = payloads.parse(renderer, outcome.payload)
                bodies = defaultdict(set)
                for occurrence in parsed.occurrences:
                    bodies[occurrence.id].add(occurrence.body)
                preserved = sum(1 for item_id, body in protected_items if body in bodies.get(item_id, ()))
            except payloads.ParseError:
                preserved = 0
            protected = {"admitted": len(protected_items), "preserved": preserved}
        reasons = []
        if trace is not None:
            reasons = sorted({(r["reason"], r.get("slot")) for r in trace.get("excluded") or []
                              if isinstance(r, dict) and isinstance(r.get("reason"), str)}, key=str)
            if outcome.refusal_reason:
                reasons.append((outcome.refusal_reason, None))
        self_reported = _self_reported(trace)
        return Judged(adapter, summary, meta_expect, prediction, protected, reasons,
                      traces.coverage_tags(trace, None), detail, self_reported)


def _self_reported(trace) -> float | None:
    """The assembler's own total from trace.timings: its total_ms when it reports one (Rust's stage laps sit inside
    it), else the sum of its stages (TypeScript reports stages only)."""
    timings = trace.get("timings") if isinstance(trace, dict) else None
    if not isinstance(timings, dict) or not timings:
        return None
    numbers = {k: v for k, v in timings.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    total = next((v for k, v in numbers.items() if k in ("total_ms", "total")), None)
    return round(total if total is not None else sum(numbers.values()), 3)


# 1. Grid --------------------------------------------------------------------------------------------------------------

def _grid_plan(settings: dict) -> dict[scale.Cell, list[str]]:
    """Every cell and the budget labels it runs at."""
    plan: dict[scale.Cell, list[str]] = {}
    seed = int(settings.get("seed", 20261006))
    shapes = settings.get("shapes", list(scale.SHAPES))
    below = ["threshold-1"] if settings["below_threshold"] else []
    ratios = [_ratio_label(r) for r in settings["budget_ratio"]]
    for shape in shapes:
        for n in settings["candidates"]:
            for t in settings["candidate_tokens"]:
                plan[scale.Cell(shape, n, t, seed=seed)] = ratios + below
        cn, ct = settings["components_cell"]
        for tokenizer in scale.TOKENIZERS:
            for renderer in scale.RENDERERS:
                cell = scale.Cell(shape, cn, ct, tokenizer, renderer, seed)
                labels = [_ratio_label(r) for r in settings["components_ratio"]] + below
                plan[cell] = list(dict.fromkeys(plan.get(cell, []) + labels))
        if shape in settings["window_shapes"]:
            wn, wt = settings["window_cell"]
            cell = scale.Cell(shape, wn, wt, seed=seed)
            plan[cell] = list(dict.fromkeys(plan.get(cell, []) + [f"window:{w}" for w in settings["windows"]]))
    return plan


def _series(cell: scale.Cell) -> tuple:
    return cell.shape, cell.tokenizer, cell.renderer


def run_grid(ctx: SuiteContext, settings: dict, cache: CellCache, judge: Judge):
    plan = _grid_plan(settings)
    timeout = float(settings["timeout_s"])
    results: dict[tuple[scale.Cell, str], dict[str, Judged]] = defaultdict(dict)
    skipped: dict[tuple[scale.Cell, str], dict[str, str]] = defaultdict(dict)
    timeouts: list[tuple[str, scale.Cell, str]] = []
    shas: dict[tuple[scale.Cell, str], str] = {}
    lock = threading.Lock()
    chains = defaultdict(list)
    for cell in plan:
        chains[_series(cell)].append(cell)
    # The longest chains first, so none is left to run alone at the end.
    work_items = sorted(((series, adapter) for series in chains for adapter in ctx.adapters),
                        key=lambda job: -sum(c.candidates * c.tokens for c in chains[job[0]]))
    done = Counter()
    started = time.monotonic()

    def chain(job):
        series, adapter_name = job
        adapter = ctx.adapters[adapter_name]
        frontier: dict[str, tuple[int, str]] = {}  # label → (work, cell key) of the cell that timed out
        for cell in sorted(chains[series], key=lambda c: (c.candidates * c.tokens, c.candidates)):
            meta = cache.get(cell)
            if isinstance(meta, str):
                continue
            for label in plan[cell]:
                if label in frontier and meta.work >= frontier[label][0]:
                    with lock:
                        skipped[(cell, label)][adapter_name] = (f"at least the work of {frontier[label][1]}, which "
                                                                f"timed out")
                    continue
                budget = meta.budget(label)
                data = meta.at(budget)
                invocation = adapters_mod.invoke(adapter, data, timeout, ctx.config.root)
                judged = judge.judge(adapter_name, meta.expect(budget), meta.protected_items, cell.renderer, data,
                                     invocation)
                sha = hashlib.sha256(data).hexdigest()
                del data
                with lock:
                    shas[(cell, label)] = sha
                    results[(cell, label)][adapter_name] = judged
                    if judged.answer["outcome"] == "timeout":
                        timeouts.append((adapter_name, cell, label))
                        frontier[label] = (meta.work, cell.key)
                    done[adapter_name] += 1
                    total = sum(done.values())
                if total % 500 == 0:
                    ctx.log(f"S7: grid: {total} answers, {len(timeouts)} timeout(s), "
                            f"{time.monotonic() - started:.0f} s")

    ctx.log(f"S7: grid of {len(plan)} cells × {len(ctx.adapters)} adapter(s), {len(work_items)} chains, "
            f"timeout {timeout:.0f} s")
    with ThreadPoolExecutor(max_workers=ctx.config.concurrency) as pool:
        for _ in pool.map(chain, work_items):
            pass
    ctx.log(f"S7: grid done in {time.monotonic() - started:.0f} s: {sum(done.values())} answers, "
            f"{len(timeouts)} timeout(s), {sum(len(v) for v in skipped.values())} skipped")
    return plan, results, skipped, timeouts, shas


# 2. Threshold search --------------------------------------------------------------------------------------------------

def run_thresholds(ctx: SuiteContext, settings: dict, cache: CellCache, answers: Answers) -> list[dict]:
    seed = int(settings.get("seed", 20261006))
    cells = [scale.Cell(shape, n, t, seed=seed) for shape in settings.get("shapes", list(scale.SHAPES))
             for n, t in settings["threshold_cells"]]
    jobs = [(cell, adapter) for cell in cells for adapter in ctx.adapters]

    def search(job):
        cell, adapter = job
        meta = cache.get(cell)
        if isinstance(meta, str):
            return {"cell": cell.as_json(), "adapter": adapter, "status": "not_constructible", "detail": meta}
        probes = []

        def assembled(budget: int) -> bool:
            found = answers.get(adapter, meta.at(budget))
            probes.append({"budget_input": budget, "outcome": found.kind, "refusal_reason": found.outcome.refusal_reason,
                           "predicted": list(meta.expect(budget)),
                           "audit_failed": found.audit_failed})
            return found.kind == "assembled"

        lo, hi = 0, meta.full  # lo never assembles (budget 0 is below every payload), hi must
        if not assembled(hi):
            status, found = "full_did_not_assemble", None
        else:
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if assembled(mid):
                    hi = mid
                else:
                    lo = mid
            found = hi
            status = None
        computed = meta.floor if meta.floor is not None else meta.protected
        if status is None:
            status = "exact" if found == computed else "inexact"
        mispredicted = [p for p in probes if [p["outcome"], p["refusal_reason"]] != p["predicted"]]
        return {"cell": cell.as_json(), "adapter": adapter, "status": status, "found": found,
                "computed": computed, "computed_from": "floor" if meta.floor is not None else "protected",
                "protected": meta.protected, "floor": meta.floor, "full": meta.full, "probes": len(probes),
                "mispredicted": len(mispredicted),
                "audit_failed": sorted({c for p in probes for c in p["audit_failed"]}),
                "probe_log": probes}

    with ThreadPoolExecutor(max_workers=ctx.config.concurrency) as pool:
        out = list(pool.map(search, jobs))
    ctx.log(f"S7: threshold search: {sum(r['status'] == 'exact' for r in out)}/{len(out)} exact")
    return out


# 3. Sweeps ------------------------------------------------------------------------------------------------------------

def _frame(found: Answer, budget: int) -> dict:
    trace = found.outcome.trace if isinstance(found.outcome.trace, dict) else {}
    variant = {r.get("item_id"): r.get("variant_id") for r in trace.get("compressed") or []}
    return {
        "budget_input": budget,
        "outcome": found.kind,
        "refusal_reason": found.outcome.refusal_reason,
        "included": [{"item_id": r.get("item_id"), "slot": r.get("slot"), "tokens": r.get("tokens"),
                      "variant_id": variant.get(r.get("item_id"))} for r in trace.get("included") or []],
        "omitted": [r.get("item_id") for r in trace.get("excluded") or [] if r.get("reason") == "over_budget"],
        "input_tokens": (trace.get("result") or {}).get("input_tokens"),
        "charged_tokens": traces.charged_tokens(trace) if trace else None,
        "audit_failed": found.audit_failed,
    }


def _frame_key(frame: dict) -> tuple:
    return (frame["outcome"], frame["refusal_reason"],
            tuple((i["item_id"], i["variant_id"]) for i in frame["included"]), tuple(frame["omitted"]))


def run_sweeps(ctx: SuiteContext, settings: dict, cache: CellCache, answers: Answers) -> list[dict]:
    seed = int(settings.get("seed", 20261006))
    n, t = settings["sweep_cell"]
    cells = [scale.Cell(shape, n, t, seed=seed) for shape in settings.get("shapes", list(scale.SHAPES))]
    step_percent = float(settings["sweep_step_percent"])

    def sweep(job):
        cell, adapter = job
        meta = cache.get(cell)
        frames: dict[int, dict] = {}

        def frame(budget: int) -> dict:
            if budget not in frames:
                frames[budget] = _frame(answers.get(adapter, meta.at(budget)), budget)
                frames[budget]["predicted"] = list(meta.expect(budget))
                frames[budget]["exact_step"] = False
            return frames[budget]

        step = max(1, round(meta.full * step_percent / 100))
        coarse, budget = [], meta.full
        while budget >= 1:
            coarse.append(budget)
            if frame(budget)["refusal_reason"] == "protected_content_over_budget":
                break
            budget -= step

        def refine(lo: int, hi: int) -> None:
            if hi - lo <= 1 or _frame_key(frame(lo)) == _frame_key(frame(hi)):
                return
            mid = (lo + hi) // 2
            frame(mid)
            refine(lo, mid)
            refine(mid, hi)

        for hi, lo in zip(coarse, coarse[1:]):
            refine(lo, hi)
        ordered = [frames[b] for b in sorted(frames, reverse=True)]
        for upper, lower in zip(ordered, ordered[1:]):
            if upper["budget_input"] - lower["budget_input"] == 1 and _frame_key(upper) != _frame_key(lower):
                upper["exact_step"] = lower["exact_step"] = True
        return cell, adapter, meta, ordered

    jobs = [(cell, adapter) for cell in cells for adapter in ctx.adapters]
    with ThreadPoolExecutor(max_workers=ctx.config.concurrency) as pool:
        done = list(pool.map(sweep, jobs))
    by_cell = defaultdict(dict)
    metas = {}
    for cell, adapter, meta, ordered in done:
        by_cell[cell][adapter] = ordered
        metas[cell] = meta
    out = []
    for cell, per_adapter in by_cell.items():
        out.append(_write_sweep(ctx, settings, cell, metas[cell], per_adapter, answers))
    ctx.log(f"S7: {len(out)} sweeps, {sum(s['agree'] for s in out)} agreeing across adapters")
    return out


def _shedding(meta: Meta, frames: list[dict], tiers: dict[str, str], slots: dict[str, str]) -> list[dict]:
    """For each candidate, the largest budget at which it was first compressed and first omitted, from the frames in
    descending budget order: the shedding curve, item by item."""
    out = {}
    for item_id in slots:
        out[item_id] = {"item_id": item_id, "slot": slots[item_id], "tier": tiers.get(item_id),
                        "first_compressed_at": None, "variant_id": None, "first_omitted_at": None}
    for frame in frames:
        if frame["outcome"] != "assembled":
            continue
        for row in frame["included"]:
            entry = out.get(row["item_id"])
            if entry and row["variant_id"] and entry["first_compressed_at"] is None:
                entry["first_compressed_at"], entry["variant_id"] = frame["budget_input"], row["variant_id"]
        for item_id in frame["omitted"]:
            entry = out.get(item_id)
            if entry and entry["first_omitted_at"] is None:
                entry["first_omitted_at"] = frame["budget_input"]
    return sorted(out.values(), key=lambda e: (-(e["first_omitted_at"] or 0), -(e["first_compressed_at"] or 0),
                                               e["item_id"]))


def _write_sweep(ctx, settings, cell: scale.Cell, meta: Meta, per_adapter: dict[str, list[dict]],
                 answers: Answers) -> dict:
    base = meta.at(meta.full)
    base_ref = ctx.run.blobs.put(base, "application/json")
    digest = base_ref.removeprefix("sha256:")
    snapshot = json.loads(base)
    tiers, slots = {}, {}
    from ..oracles.auditor.model import View
    view = View.build(ctx.contract, snapshot)
    for candidate in view.candidates:
        tiers[candidate.recorded_id] = view.tier(candidate)
        slots[candidate.recorded_id] = candidate.slot
    keys = {a: [(f["budget_input"], _frame_key(f)) for f in frames] for a, frames in per_adapter.items()}
    reference = next(iter(per_adapter))
    agree = all(k == keys[reference] for k in keys.values())
    mispredicted = {a: sum(1 for f in frames if [f["outcome"], f["refusal_reason"]] != f["predicted"])
                    for a, frames in per_adapter.items()}
    audit_failed = {a: sorted({c for f in frames for c in f["audit_failed"]}) for a, frames in per_adapter.items()}
    assembled = [f for f in per_adapter[reference] if f["outcome"] == "assembled"]
    threshold = min((f["budget_input"] for f in assembled), default=None)

    # Timelines at a few budgets, per adapter.
    written = []
    points = sorted({min(per_adapter[reference], key=lambda f: abs(f["budget_input"] - meta.full * p))["budget_input"]
                     for p in settings["timeline_points"]} | ({threshold} if threshold else set()), reverse=True)
    below = [f["budget_input"] for f in per_adapter[reference] if threshold and f["budget_input"] < threshold]
    if below:
        points.append(max(below))
    for budget in points:
        data = meta.at(budget)
        ref = ctx.run.blobs.put(data, "application/json")
        frame_snapshot = json.loads(data)
        for adapter in per_adapter:
            found = answers.get(adapter, data)
            if not isinstance(found.outcome.trace, dict):
                continue
            document = timelines.build(ctx.contract, frame_snapshot, found.outcome.trace, adapter, ref, ctx.run.run_id)
            path = f"timelines/{ref.removeprefix('sha256:')}/{adapter}.json"
            ctx.run.write_json(path, document, f"S7: assembly timeline, {cell.shape} at budget {budget}, {adapter}")
            written.append(path)

    document = {
        "$schema": output.schema_name("sweep"),
        "run_id": ctx.run.run_id,
        "suite": ID,
        "cell": cell.as_json(),
        "snapshot": base_ref,
        "full": meta.full,
        "protected": meta.protected,
        "floor": meta.floor,
        "step_percent": float(settings["sweep_step_percent"]),
        "agree": agree,
        "threshold": threshold,
        "mispredicted": mispredicted,
        "audit_failed": audit_failed,
        "shedding": _shedding(meta, per_adapter[reference], tiers, slots),
        "shedding_from": reference,
        "adapters": {a: {"frames": frames} for a, frames in per_adapter.items()},
        "timelines": written,
    }
    path = f"suites/{ID}/sweeps/{digest}.json"
    ctx.run.write_json(path, document, f"S7: budget sweep of {cell.shape}, frames per adapter")
    return {"cell": cell.as_json(), "path": path, "agree": agree, "frames": {a: len(f) for a, f in per_adapter.items()},
            "exact_steps": sum(f["exact_step"] for f in per_adapter[reference]), "threshold": threshold,
            "computed": meta.floor if meta.floor is not None else meta.protected, "mispredicted": mispredicted,
            "audit_failed": audit_failed, "timelines": len(written)}


# 4. Performance -------------------------------------------------------------------------------------------------------

def _minimal(ctx) -> bytes:
    b = Builder(ctx.contract, "s7-minimal")
    b.base()
    return b.bytes()


def run_perf(ctx: SuiteContext, settings: dict, cache: CellCache, plan: dict, grid_results: dict):
    """Serial measurement. Returns (samples rows, summary document parts, timeout occurrences)."""
    wall0, mono0 = time.time(), time.monotonic()
    timeout = float(settings["timeout_s"])
    policy = perf.Policy(min_samples=int(settings["perf_min_samples"]), max_samples=int(settings["perf_max_samples"]),
                         max_seconds=float(settings["perf_max_seconds"]), ci_target=float(settings["perf_ci_target"]))
    samples: list[dict] = []
    startup, cells_out, timeouts = {}, [], []
    minimal = _minimal(ctx)

    def record(adapter, method, cell, label, budget, sha, index, wall_ms, rss, outcome, net=None, self_ms=None):
        samples.append({"$schema": output.schema_name("perf-sample"), "run_id": ctx.run.run_id, "adapter": adapter,
                        "method": method, "cell": cell, "label": label, "budget_input": budget, "snapshot_sha256": sha,
                        "index": index, "wall_ms": round(wall_ms, 3), "net_ms": None if net is None else round(net, 3),
                        "rss_bytes": rss, "outcome": outcome,
                        "self_reported_ms": None if self_ms is None else round(self_ms, 3)})

    def e2e(adapter, data):
        """One timed end-to-end run: (ms, rss, outcome kind, self-reported ms) or None on a failure."""
        measured = perf.invoke_measured(adapter, data, timeout, ctx.config.root)
        outcome = adapters_mod.classify(measured.invocation)
        if outcome.kind in FAULTS:
            return None, outcome.kind
        self_ms = _self_reported(outcome.trace)
        return (measured.invocation.wall_ms, measured.rss_bytes, outcome.kind, self_ms), outcome.kind

    # Startup baseline: the minimal snapshot, many samples.
    sha = hashlib.sha256(minimal).hexdigest()
    for name, adapter in ctx.adapters.items():
        got = []

        def take():
            value, _ = e2e(adapter, minimal)
            if value is None:
                return None
            got.append(value)
            return value[0]

        stats, ok = perf.sample(take, perf.Policy(warmup=2, min_samples=20, max_samples=50, max_seconds=20,
                                                  ci_target=policy.ci_target))
        for i, (ms, rss, kind, self_ms) in enumerate(got[-stats.n:] if stats.n else []):
            record(name, "e2e", None, "minimal", None, sha, i, ms, rss, kind, self_ms=self_ms)
        startup[name] = {"adapter": name, **(stats.as_json(policy.ci_target) if stats.n else {"samples": 0}),
                         "rss_peak_bytes": max((g[1] for g in got if g[1] is not None), default=None)}
        ctx.log(f"S7: perf: {name} startup p50 {startup[name].get('p50_ms')} ms over {stats.n} samples")

    # Cells: each shape's three series, default tokenizer and renderer, at the perf labels.
    labels = list(settings["perf_labels"])
    perf_cells = sorted({cell for series in perf_series(settings).values() for cell in series},
                        key=lambda c: (c.shape, c.candidates * c.tokens, c.candidates))
    for name, adapter in ctx.adapters.items():
        frontier: dict[tuple, int] = {}
        base = startup[name].get("p50_ms")
        for cell in perf_cells:
            meta = cache.get(cell)
            if isinstance(meta, str):
                continue
            for label in labels:
                entry = {"adapter": name, "cell": cell.as_json(), "label": label, "work": meta.work,
                         "budget_input": None, "outcome": None, "status": "measured", "skipped_reason": None,
                         "e2e": None, "net_p50_ms": None, "inprocess": None, "rss_peak_bytes": None,
                         "self_reported_p50_ms": None}
                cells_out.append(entry)
                key = (cell.shape, label)
                if key in frontier and meta.work >= frontier[key]:
                    entry["status"], entry["skipped_reason"] = "skipped", "at least the work of a cell that timed out"
                    continue
                if (cell, label) in grid_results and grid_results[(cell, label)].get(name) is not None and \
                        grid_results[(cell, label)][name].answer["outcome"] == "timeout":
                    entry["status"], entry["skipped_reason"] = "skipped", "timed out in the grid"
                    frontier[key] = meta.work
                    continue
                budget = meta.budget(label)
                entry["budget_input"] = budget
                data = meta.at(budget)
                sha = hashlib.sha256(data).hexdigest()
                got, kinds = [], []

                def take():
                    value, kind = e2e(adapter, data)
                    kinds.append(kind)
                    if value is None:
                        return None
                    got.append(value)
                    return value[0]

                stats, ok = perf.sample(take, policy)
                if not ok:
                    entry["status"] = kinds[-1] if kinds else "failed"
                    if kinds and kinds[-1] == "timeout":
                        frontier[key] = meta.work
                        timeouts.append((name, cell, label))
                    if not stats.n:
                        continue
                kept = got[-stats.n:]
                entry["outcome"] = kept[-1][2] if kept else None
                entry["e2e"] = stats.as_json(policy.ci_target)
                entry["rss_peak_bytes"] = max((g[1] for g in kept if g[1] is not None), default=None)
                self_values = [g[3] for g in kept if g[3] is not None]
                entry["self_reported_p50_ms"] = round(perf.Stats(self_values).percentile(50), 3) if self_values else None
                if base is not None:
                    entry["net_p50_ms"] = round(stats.percentile(50) - base, 3)
                for i, (ms, rss, kind, self_ms) in enumerate(kept):
                    record(name, "e2e", cell.as_json(), label, budget, sha, i, ms, rss, kind,
                           net=None if base is None else ms - base, self_ms=self_ms)
                if settings["inprocess"] and adapter.timing_command and ok:
                    entry["inprocess"] = _inprocess(ctx, adapter, data, policy, timeout, record, cell, label, budget, sha)
        ctx.log(f"S7: perf: {name}: {sum(1 for c in cells_out if c['adapter'] == name and c['e2e'])} cells measured")

    throughput = _throughput(ctx, settings, cache, timeout)
    suspended = round(max(0.0, (time.time() - wall0) - (time.monotonic() - mono0)), 1)
    return samples, startup, cells_out, throughput, suspended, timeouts, policy


def _inprocess(ctx, adapter, data, policy, timeout, record, cell, label, budget, sha) -> dict | None:
    """Method 3: the adapter's timing loop, called for more samples until the policy is met."""
    stats, started, outcome = perf.Stats([]), time.monotonic(), None
    while not policy.done(stats, time.monotonic() - started):
        want = policy.min_slow if stats.n and stats.mean >= policy.slow_ms else max(1, policy.min_samples - stats.n)
        want = min(want, policy.max_samples - stats.n) or 1
        measured = perf.invoke_measured(adapter, data, timeout * (want + 1), ctx.config.root,
                                        command=adapter.timing_command, args=["1", str(want)],
                                        env={**adapter.env, **adapter.timing_env})
        invocation = measured.invocation
        if invocation.timed_out or invocation.exit_code != 0:
            return {"error": "timeout" if invocation.timed_out else
                    f"exit {invocation.exit_code}: {invocation.stderr.decode('utf-8', 'replace')[-300:]}",
                    **(stats.as_json(policy.ci_target) if stats.n else {"samples": 0})}
        try:
            answer = json.loads(invocation.stdout.decode("utf-8"))
            values = [int(v) / 1e6 for v in answer["samples_ns"]]
            outcome = answer["outcome"]
        except (ValueError, KeyError, TypeError) as error:
            return {"error": f"unreadable timing output: {error}", "samples": stats.n}
        for value in values:
            record(adapter.name, "inprocess", cell.as_json(), label, budget, sha, stats.n, value, None, outcome)
            stats.samples.append(value)
    return {"error": None, "outcome": outcome, **stats.as_json(policy.ci_target)}


def _throughput(ctx, settings, cache, timeout) -> list[dict]:
    n, t = settings["throughput_cell"]
    meta = cache.get(scale.Cell("compressible-v1", n, t, seed=int(settings.get("seed", 20261006))))
    if isinstance(meta, str):
        return []
    data = meta.at(meta.budget("ratio:0.5"))
    count = int(settings["throughput_invocations"])
    out = []
    for name, adapter in ctx.adapters.items():
        for concurrency in sorted({1, ctx.config.concurrency}):
            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                kinds = list(pool.map(lambda _: adapters_mod.classify(
                    adapters_mod.invoke(adapter, data, timeout, ctx.config.root)).kind, range(count)))
            seconds = time.perf_counter() - started
            out.append({"adapter": name, "cell": meta.cell.as_json(), "label": "ratio:0.5", "concurrency": concurrency,
                        "invocations": count, "seconds": round(seconds, 3),
                        "per_second": round(count / seconds, 3) if seconds > 0 else None,
                        "outcomes": dict(Counter(kinds))})
    return out


def perf_series(settings: dict) -> dict[tuple[str, str], list[scale.Cell]]:
    """(shape, series) → its cells, in order. "candidates" grows the candidates at a fixed token total, "tokens" grows
    the total at fixed candidates, and "diagonal" grows both at fixed tokens per candidate, the series whose exponent
    tests the README's note that fitting turns quadratic when most candidates are shed."""
    seed = int(settings.get("seed", 20261006))
    ns, per = settings["perf_candidates"], int(settings["perf_tokens_per_candidate"])
    out = {}
    for shape in settings.get("shapes", list(scale.SHAPES)):
        out[(shape, "candidates")] = [scale.Cell(shape, n, int(settings["perf_fixed_tokens"]), seed=seed) for n in ns]
        out[(shape, "tokens")] = [scale.Cell(shape, int(settings["perf_fixed_candidates"]), per * n, seed=seed)
                                  for n in ns]
        out[(shape, "diagonal")] = [scale.Cell(shape, n, per * n, seed=seed) for n in ns]
    return out


def fits(settings: dict, cells_out: list[dict]) -> list[dict]:
    """A log-log slope per adapter, shape, budget label, series and method: of time against candidates (the candidates
    and diagonal series) or candidate tokens (the tokens series)."""
    measured = {}
    for entry in cells_out:
        if entry["status"] == "measured" and entry["e2e"]:
            cell = entry["cell"]
            measured[(entry["adapter"], cell["shape"], cell["candidates"], cell["candidate_tokens"], entry["label"])] = entry
    out = []
    adapters = sorted({e["adapter"] for e in cells_out})
    labels = list(dict.fromkeys(e["label"] for e in cells_out))
    for (shape, series), cells in perf_series(settings).items():
        variable = "tokens" if series == "tokens" else "candidates"
        for adapter in adapters:
            for label in labels:
                for method in ("inprocess", "net", "e2e"):
                    points = []
                    for cell in cells:
                        e = measured.get((adapter, shape, cell.candidates, cell.tokens, label))
                        if e is None:
                            continue
                        if method == "net":
                            y = e["net_p50_ms"]
                        elif method == "inprocess":
                            y = (e["inprocess"] or {}).get("p50_ms") if not (e["inprocess"] or {}).get("error") else None
                        else:
                            y = e["e2e"]["p50_ms"]
                        if y is not None and y > 0:
                            points.append(({variable: cell.tokens if series == "tokens" else cell.candidates}, y))
                    fit = perf.loglog(points, [variable]) if len(points) >= 3 else None
                    out.append({"adapter": adapter, "shape": shape, "label": label, "series": series,
                                "variable": variable, "method": method, "points": len(points),
                                "exponent": round(fit.coefficients[variable], 4) if fit else None,
                                "standard_error": round(fit.standard_errors.get(variable, 0.0), 4) if fit else None,
                                "r2": None if fit is None or fit.r2 is None else round(fit.r2, 4)})
    return out


# The suite ------------------------------------------------------------------------------------------------------------

def run(ctx: SuiteContext) -> SuiteResult:
    started = now()
    settings = _settings(ctx)
    cache = CellCache(ctx)
    judge = Judge(ctx)
    answers = Answers(ctx)
    names = list(ctx.adapters)
    try:
        plan, results, skipped, grid_timeouts, shas = run_grid(ctx, settings, cache, judge)
        thresholds = run_thresholds(ctx, settings, cache, answers)
        sweeps = run_sweeps(ctx, settings, cache, answers)
        perf_parts = run_perf(ctx, settings, cache, plan, results) if settings["perf"] else None
        rows, occurrences, timeout_findings, coverage, cases_of = _rows(ctx, settings, plan, results, skipped, shas,
                                                                        cache, answers)
        findings, plans = [], []
        for found in group(occurrences).values():
            doc = finding(ctx, found)
            findings.append(doc)
            cases_of[doc["finding_id"]] = {o.case_id for o in found}
            first = found[0]
            if first.oracle in ("differential", "auditor") and first.data:
                plans.append((doc, snapshot_plan(ctx, first, answers, names)))
        if plans:
            ctx.log(f"S7: minimizing {len(plans)} finding(s)")
            minimize(ctx, plans, int(settings["max_tests"]))
        findings += timeout_findings
        for row in rows:
            row["findings"] = sorted(fid for fid, cases in cases_of.items() if row["case_id"] in cases)
        findings += _threshold_findings(ctx, thresholds) + _sweep_findings(ctx, sweeps)
        if perf_parts:
            findings += _perf_timeout_findings(ctx, perf_parts[5], timeout_findings)
        return _finish(ctx, settings, started, plan, rows, findings, thresholds, sweeps, perf_parts, cache, coverage)
    finally:
        cache.close()


def _case_id(cell: scale.Cell, label: str) -> str:
    return f"{cell.key}@{label}"


def _rows(ctx, settings, plan, results, skipped, shas, cache: CellCache, answers: Answers):
    """One row per cell and budget; the failures in them as occurrences; timeouts as one warning per adapter, shape and
    kind of budget; coverage per answer; and the case ids each timeout finding covers."""
    rows, occurrences, timeout_findings, coverage = [], [], [], []
    cases_of: dict[str, set[str]] = {}
    timeouts_by_sig: dict[tuple, list] = defaultdict(list)
    for cell, labels in plan.items():
        meta = cache.get(cell)
        for label in labels:
            case_id = _case_id(cell, label)
            if isinstance(meta, str):
                rows.append(_row(ctx, cell, label, None, {}, {}, case_id, None, not_constructible=meta))
                continue
            per = results.get((cell, label), {})
            budget = meta.budget(label)
            row = _row(ctx, cell, label, meta, per, skipped.get((cell, label), {}), case_id,
                       shas.get((cell, label)) or hashlib.sha256(meta.at(budget)).hexdigest())
            rows.append(row)
            for judged in per.values():
                coverage.append(Coverage(judged.adapter, REQUIREMENTS, judged.reasons, judged.tags,
                                         judged.prediction != "fail" and not judged.answer["audit_failed"]))
            for adapter, judged in per.items():
                if judged.answer["outcome"] == "timeout":
                    timeouts_by_sig[(adapter, cell.shape, label.partition(":")[0])].append((cell, label, judged))
            failing = row["verdict"] == "failed"
            if not failing:
                continue
            data = meta.at(budget)
            rules = with_rules(REQUIREMENTS, [])
            for adapter, judged in per.items():
                a = judged.answer
                if judged.prediction == "fail":
                    occurrences.append(Occurrence(
                        ID, "expected", ["prediction"],
                        {"suite": ID, "oracle": "expected", "adapter": adapter, "shape": cell.shape,
                         "label": label.partition(":")[0], "expected": list(judged.expected),
                         "got": [a["outcome"], a["refusal_reason"]]},
                        adapter, [adapter],
                        f"{adapter} answered {a['outcome']} {a['refusal_reason'] or ''} on {case_id}; the rules give "
                        f"{judged.expected[0]} {judged.expected[1] or ''}".replace("  ", " "),
                        None, with_rules(rules, ["R-16", "R-17"]), case_id, data))
                if judged.protected and judged.protected["preserved"] < judged.protected["admitted"]:
                    occurrences.append(Occurrence(
                        ID, "expected", ["protected_preservation"],
                        {"suite": ID, "oracle": "expected", "check": "protected_preservation", "adapter": adapter,
                         "shape": cell.shape}, adapter, [adapter],
                        f"{adapter} rendered {judged.protected['preserved']} of {judged.protected['admitted']} "
                        f"admitted protected items byte-exact on {case_id}", None, with_rules(rules, ["R-16", "R-17"]),
                        case_id, data))
                for check in a["audit_failed"]:
                    violation = normalized((judged.audit_detail.get(check) or [""])[0])
                    occurrences.append(Occurrence(
                        ID, "auditor", [check],
                        {"suite": ID, "oracle": "auditor", "check": check, "adapter": adapter, "violation": violation},
                        adapter, [adapter], f"{adapter} breaks {check} on {case_id}: "
                                            f"{(judged.audit_detail.get(check) or [''])[0]}"[:600],
                        None, with_rules(rules, AUDIT_RULES.get(check, [])), case_id, data,
                        context={"check": check, "violation": violation}))
                if a["outcome"] in ("crashed", "invalid_output"):
                    occurrences.append(Occurrence(
                        ID, "fault", [a["outcome"]],
                        {"suite": ID, "oracle": "fault", "adapter": adapter, "outcome": a["outcome"],
                         "problem": normalized(a["problem"])}, adapter, [adapter],
                        f"{adapter} {a['outcome']} on {case_id}: {a['problem'] or ''}"[:600], None, rules, case_id,
                        data, context={"kind": a["outcome"]}))
            if not row["agree"]:
                full = [answers.get(a, data) for a in ctx.adapters]
                usable = compared(full, data)
                groups = partition(usable)
                biggest = [a for a in usable if a.adapter in groups[0]]
                other = next(a for a in usable if a.adapter not in groups[0])
                difference = first_difference(biggest[0], other)
                occurrences.append(Occurrence(
                    ID, "differential", ["agreement"],
                    {"suite": ID, "oracle": "differential", "groups": groups, "stage": difference["stage"],
                     "pointer": pointer_shape(difference["pointer"])}, None, list(ctx.adapters),
                    f"adapters disagree on {case_id}: " + " vs ".join("/".join(g) for g in groups),
                    difference["pointer"], with_rules(rules, ["R-23"]), case_id, data, context={"groups": groups}))
    for (adapter, shape, kind), cases in timeouts_by_sig.items():
        cells = sorted(cases, key=lambda c: c[0].candidates * c[0].tokens)
        first_cell, first_label, _ = cells[0]
        signature = {"suite": ID, "oracle": "fault", "check": "timeout", "adapter": adapter, "shape": shape,
                     "label": kind}
        fid = hashlib.sha256(output.dumps(signature, compact=True).encode()).hexdigest()[:12]
        timeout_findings.append({
            "$schema": output.schema_name("finding"), "finding_id": fid, "run_id": ctx.run.run_id, "suite": ID,
            "adapter": adapter, "adapters": [adapter], "case_id": _case_id(first_cell, first_label), "oracle": "fault",
            "checks": ["timeout"], "severity": "warning",
            "summary": (f"{adapter} timed out ({settings['timeout_s']} s) on {len(cells)} {shape} cell(s) at "
                        f"{kind} budgets, the least work {first_cell.candidates} candidates × {first_cell.tokens} "
                        f"tokens; larger cells were skipped. A performance finding, not a correctness one."),
            "first_pointer": None, "requirements": ["R-16"], "occurrences": len(cells), "reproducer": None,
            "signature": signature, "minimized": None,
        })
        cases_of[fid] = {_case_id(cell, label) for cell, label, _ in cells}
    return rows, occurrences, timeout_findings, coverage, cases_of


def _row(ctx, cell, label, meta: Meta | None, per: dict[str, Judged], skipped: dict[str, str], case_id: str,
         sha: str | None, not_constructible: str | None = None) -> dict:
    budget = meta.budget(label) if meta else None
    usable = [j for j in per.values() if j.answer["outcome"] not in FAULTS
              and not (j.answer["outcome"] == "unsupported" and cell.renderer in OPTIONAL_RENDERERS)]
    signatures = defaultdict(list)
    for j in usable:
        signatures[j.signature].append(j.adapter)
    groups = sorted((sorted(g) for g in signatures.values()), key=lambda g: (-len(g), g))
    agree = len(groups) <= 1
    problems = []
    for j in per.values():
        a = j.answer
        if j.prediction == "fail":
            problems.append(f"{j.adapter}: {a['outcome']} {a['refusal_reason'] or ''}, expected "
                            f"{j.expected[0]} {j.expected[1] or ''}".strip())
        if a["audit_failed"]:
            problems.append(f"{j.adapter}: audit {', '.join(a['audit_failed'])}")
        if j.protected and j.protected["preserved"] < j.protected["admitted"]:
            problems.append(f"{j.adapter}: protected {j.protected['preserved']}/{j.protected['admitted']}")
        if a["outcome"] in ("crashed", "invalid_output"):
            problems.append(f"{j.adapter}: {a['outcome']}")
    if not agree:
        problems.append("disagreement: " + " vs ".join("/".join(g) for g in groups))
    expected = meta.expect(budget) if meta else (None, None)
    return {
        "$schema": output.schema_name("scale-row"),
        "run_id": ctx.run.run_id,
        "suite": ID,
        "case_id": case_id,
        "cell": cell.as_json(),
        "label": label,
        "budget_input": budget,
        "snapshot_sha256": sha,
        "work": cell.candidates * cell.tokens,
        "full": meta.full if meta else None,
        "protected": meta.protected if meta else None,
        "floor": meta.floor if meta else None,
        "items": meta.items if meta else None,
        "bytes": meta.bytes if meta else None,
        "not_constructible": not_constructible,
        "expected": {"outcome": expected[0], "refusal_reason": expected[1]},
        "answers": [{**j.answer, "prediction": j.prediction, "protected": j.protected,
                     "self_reported_ms": j.self_reported_ms} for j in per.values()],
        "skipped": [{"adapter": a, "reason": r} for a, r in sorted(skipped.items())],
        "agree": agree,
        "groups": groups,
        "verdict": "failed" if problems else "passed",
        "problems": problems,
        "coverage_tags": sorted({t for j in per.values() for t in j.tags}),
        "findings": [],
    }


def _threshold_findings(ctx, thresholds: list[dict]) -> list[dict]:
    out = []
    for t in thresholds:
        if t["status"] in ("exact", "not_constructible") and not t["mispredicted"] and not t["audit_failed"]:
            continue
        signature = {"suite": ID, "oracle": "expected", "check": "threshold", "adapter": t["adapter"],
                     "shape": t["cell"]["shape"], "status": t["status"]}
        fid = hashlib.sha256(output.dumps(signature, compact=True).encode()).hexdigest()[:12]
        out.append({
            "$schema": output.schema_name("finding"), "finding_id": fid, "run_id": ctx.run.run_id, "suite": ID,
            "adapter": t["adapter"], "adapters": [t["adapter"]], "case_id": f"threshold/{t['cell']['shape']}",
            "oracle": "expected", "checks": ["threshold"], "severity": "error",
            "summary": (f"{t['adapter']} threshold search on {t['cell']['shape']}: {t['status']}, smallest assembling "
                        f"budget {t.get('found')}, the rules give {t.get('computed')}; {t.get('mispredicted', 0)} "
                        f"probe(s) mispredicted, audit {', '.join(t.get('audit_failed') or []) or 'clean'}"),
            "first_pointer": None, "requirements": ["R-16", "R-17"], "occurrences": 1, "reproducer": None,
            "signature": signature, "minimized": None,
        })
    return out


def _sweep_findings(ctx, sweeps: list[dict]) -> list[dict]:
    out = []
    for s in sweeps:
        problems = []
        if not s["agree"]:
            problems.append("adapters' frames differ")
        problems += [f"{a}: {n} frame(s) mispredicted" for a, n in s["mispredicted"].items() if n]
        problems += [f"{a}: audit {', '.join(c)}" for a, c in s["audit_failed"].items() if c]
        if not problems:
            continue
        signature = {"suite": ID, "oracle": "expected", "check": "sweep", "shape": s["cell"]["shape"],
                     "problems": [normalized(p) for p in problems]}
        fid = hashlib.sha256(output.dumps(signature, compact=True).encode()).hexdigest()[:12]
        out.append({
            "$schema": output.schema_name("finding"), "finding_id": fid, "run_id": ctx.run.run_id, "suite": ID,
            "adapter": None, "adapters": [], "case_id": f"sweep/{s['cell']['shape']}", "oracle": "expected",
            "checks": ["sweep"], "severity": "error",
            "summary": f"sweep of {s['cell']['shape']} ({s['path']}): " + "; ".join(problems),
            "first_pointer": None, "requirements": ["R-16", "R-17", "R-23"], "occurrences": 1, "reproducer": None,
            "signature": signature, "minimized": None,
        })
    return out


def _perf_timeout_findings(ctx, perf_timeouts, grid_findings) -> list[dict]:
    """Timeouts the serial perf phase met that the grid did not already report."""
    known = {(f["adapter"], f["signature"]["shape"], f["signature"]["label"]) for f in grid_findings}
    out = []
    for adapter, cell, label in perf_timeouts:
        kind = label.partition(":")[0]
        if (adapter, cell.shape, kind) in known:
            continue
        known.add((adapter, cell.shape, kind))
        signature = {"suite": ID, "oracle": "fault", "check": "timeout", "adapter": adapter, "shape": cell.shape,
                     "label": kind}
        fid = hashlib.sha256(output.dumps(signature, compact=True).encode()).hexdigest()[:12]
        out.append({
            "$schema": output.schema_name("finding"), "finding_id": fid, "run_id": ctx.run.run_id, "suite": ID,
            "adapter": adapter, "adapters": [adapter], "case_id": _case_id(cell, label), "oracle": "fault",
            "checks": ["timeout"], "severity": "warning",
            "summary": f"{adapter} timed out while being measured on {_case_id(cell, label)}",
            "first_pointer": None, "requirements": ["R-16"], "occurrences": 1, "reproducer": None,
            "signature": signature, "minimized": None,
        })
    return out


def _finish(ctx, settings, started, plan, rows, findings, thresholds, sweeps, perf_parts, cache,
            coverage) -> SuiteResult:
    base = f"suites/{ID}"
    suite_metrics = []
    answers_by = defaultdict(list)
    for row in rows:
        for a in row["answers"]:
            answers_by[a["adapter"]].append((row, a))
    for adapter in ctx.adapters:
        mine = answers_by[adapter]
        judged = [a for _, a in mine if a["prediction"] != "skipped"]
        suite_metrics.append(metrics.rate("s7.prediction", "Answers with the predicted outcome and refusal code",
                                          sum(a["prediction"] == "pass" for a in judged), len(judged), suite=ID,
                                          adapter=adapter, description="The outcome and refusal code the rules give at "
                                          "each budget, computed by the harness's own renderer before assembly."))
        admitted = sum(a["protected"]["admitted"] for _, a in mine if a["protected"])
        preserved = sum(a["protected"]["preserved"] for _, a in mine if a["protected"])
        suite_metrics.append(metrics.rate("s7.protected_preserved", "Protected preservation (assembled)", preserved,
                                          admitted, suite=ID, adapter=adapter,
                                          description="Admitted protected items rendered byte-exact, over every "
                                                      "assembled answer."))
        refusals = [a for _, a in mine if a["outcome"] == "refused"]
        suite_metrics.append(metrics.rate("s7.refusal_correct", "Protected preservation (refused): correct code",
                                          sum(a["prediction"] == "pass" for a in refusals), len(refusals), suite=ID,
                                          adapter=adapter, description="Refusals carrying the code the rules give."))
        audited = [a for _, a in mine if a["outcome"] in ("assembled", "refused")]
        suite_metrics.append(metrics.rate("s7.audit_rate", "Outputs passing the trace audit",
                                          sum(not a["audit_failed"] for a in audited), len(audited), suite=ID,
                                          adapter=adapter))
        mine_thresholds = [t for t in thresholds if t["adapter"] == adapter and t["status"] != "not_constructible"]
        suite_metrics.append(metrics.rate("s7.threshold_exact", "Refusal thresholds found exactly",
                                          sum(t["status"] == "exact" for t in mine_thresholds), len(mine_thresholds),
                                          suite=ID, adapter=adapter, description="Binary search for the smallest "
                                          "budget that assembles equals the harness's protected-only (or floored) "
                                          "charged count."))
        suite_metrics.append(metrics.count("s7.timeouts", "Grid answers that timed out",
                                           sum(a["outcome"] == "timeout" for _, a in mine), suite=ID, adapter=adapter,
                                           description=f"Per-call timeout {settings['timeout_s']} s; a performance "
                                                       "finding, not a correctness one."))
    compared_rows = [r for r in rows if r["answers"]]
    suite_metrics.append(metrics.rate("s7.agreement", "Grid cells on which every adapter agrees",
                                      sum(r["agree"] for r in compared_rows), len(compared_rows), suite=ID))
    suite_metrics.append(metrics.rate("s7.sweep_agreement", "Sweeps whose frames agree across adapters",
                                      sum(s["agree"] for s in sweeps), len(sweeps), suite=ID))

    perf_summary_path = None
    if perf_parts:
        samples, startup, cells_out, throughput, suspended, _, policy = perf_parts
        fitted = fits(settings, cells_out)
        for adapter in ctx.adapters:
            s = startup.get(adapter, {})
            if s.get("p50_ms") is not None:
                suite_metrics.append(metrics.value("s7.startup_ms", "Startup (minimal snapshot), p50", s["p50_ms"], "ms",
                                                   suite=ID, adapter=adapter))
            diagonal = {method: sorted(f["exponent"] for f in fitted if f["adapter"] == adapter
                                       and f["method"] == method and f["series"] == "diagonal"
                                       and f["label"] == "ratio:0.1" and f["exponent"] is not None)
                        for method in ("inprocess", "net")}
            method = "inprocess" if diagonal["inprocess"] else "net"
            if diagonal[method]:
                values = diagonal[method]
                suite_metrics.append(metrics.value(
                    "s7.exponent", "Scaling exponent under pressure (median over shapes)", values[len(values) // 2],
                    "exponent", suite=ID, adapter=adapter,
                    description=f"{method} time against candidates at fixed tokens per candidate, budget ratio 0.1: "
                                "about 2 means fitting re-counts the whole payload per reduction."))
        ctx.run.write_jsonl("perf/samples.jsonl", samples, "perf-sample", "S7: every timing sample, raw")
        refusal_times = defaultdict(list)
        for entry in cells_out:
            if entry["outcome"] == "refused" and entry["e2e"]:
                refusal_times[entry["adapter"]].append(entry["e2e"]["p50_ms"])
        perf_summary_path = "perf/summary.json"
        ctx.run.write_json(perf_summary_path, {
            "$schema": output.schema_name("perf-summary"),
            "run_id": ctx.run.run_id,
            "suite": ID,
            "host_suspended_seconds": suspended,
            "reliable": suspended <= 1.0,
            "policy": {"warmup": policy.warmup, "min_samples": policy.min_samples, "min_slow": policy.min_slow,
                       "slow_ms": policy.slow_ms, "max_samples": policy.max_samples, "max_seconds": policy.max_seconds,
                       "ci_target": policy.ci_target, "serial": True},
            "methods": {"e2e": "adapter started per snapshot", "net": "e2e p50 minus the adapter's startup p50",
                        "inprocess": "the assembler's own timing loop (adapters/timing/), no process start",
                        "self_reported": "sum of trace.timings, where the assembler reports them"},
            "startup": list(startup.values()),
            "cells": cells_out,
            "series": {"candidates": settings["perf_candidates"], "fixed_tokens": settings["perf_fixed_tokens"],
                       "fixed_candidates": settings["perf_fixed_candidates"],
                       "tokens_per_candidate": settings["perf_tokens_per_candidate"]},
            "fits": fitted,
            "throughput": throughput,
            "time_to_refusal": [{"adapter": a, "cells": len(v), "p50_ms": round(perf.Stats(v).percentile(50), 3)}
                                for a, v in sorted(refusal_times.items())],
            "samples": "perf/samples.jsonl",
        }, "S7: performance per adapter and cell: percentiles, CIs, RSS, scaling fits, throughput")

    errors = [f for f in findings if f["severity"] == "error"]
    unavailable_timing = {n: a.timing_error for n, a in ctx.adapters.items() if a.timing_error}
    status = "fail" if errors else ("partial" if ctx.unavailable or unavailable_timing else "pass")
    ctx.run.write_jsonl(f"{base}/results.jsonl", rows, "scale-row", "S7: one row per cell and budget, all adapters")
    ctx.run.write_jsonl(f"{base}/findings.jsonl", findings, "finding", "S7: correctness and performance findings")
    corpus = [{"case_id": r["case_id"], "sha256": r["snapshot_sha256"]} for r in rows if r["snapshot_sha256"]]
    ctx.run.write_json("corpora/scale/index.json", {
        "$schema": output.schema_name("corpus-index"), "run_id": ctx.run.run_id, "corpus": "scale",
        "generator": {"module": "cwabench.corpora.scale", "seed": int(settings.get("seed", 20261006)),
                      "settings": {k: settings[k] for k in ("candidates", "candidate_tokens", "budget_ratio",
                                                            "components_cell", "components_ratio", "window_cell",
                                                            "windows", "window_shapes")}},
        "count": len(corpus), "rounds": [], "snapshots": corpus,
    }, "S7: every scale snapshot by cell and budget, enough to regenerate it")
    not_constructible = sorted({(r["cell"]["shape"], r["cell"]["candidates"], r["cell"]["candidate_tokens"],
                                 r["not_constructible"]) for r in rows if r["not_constructible"]})
    summary_path = f"{base}/summary.json"
    ctx.run.write_json(summary_path, {
        "$schema": output.schema_name("suite-summary"),
        "run_id": ctx.run.run_id,
        "suite": ID,
        "title": TITLE,
        "status": status,
        "started_at": started,
        "finished_at": now(),
        "requirements": REQUIREMENTS,
        "corpora": [{"id": "scale", "count": len(corpus)}],
        "adapters": [],
        "metrics": suite_metrics,
        "files": {"results": f"{base}/results.jsonl", "findings": f"{base}/findings.jsonl",
                  **({"perf": perf_summary_path} if perf_summary_path else {})},
        "scale": {
            "shapes": {name: spec[0] for name, spec in scale.SHAPES.items()},
            "settings": {k: v for k, v in settings.items()},
            "cells": len(plan),
            "rows": len(rows),
            "not_constructible": [{"shape": s, "candidates": n, "candidate_tokens": t, "why": w}
                                  for s, n, t, w in not_constructible],
            "skipped": sum(len(r["skipped"]) for r in rows),
            "timeouts": [{"adapter": f["adapter"], "summary": f["summary"]} for f in findings
                         if f["checks"] == ["timeout"]],
            "thresholds": [{k: v for k, v in t.items() if k != "probe_log"} for t in thresholds],
            "sweeps": sweeps,
            "timing_loops": {n: {"available": bool(a.timing_command), "error": a.timing_error}
                             for n, a in ctx.adapters.items()},
        },
    }, "S7: grid, threshold, sweep and performance results")
    ctx.log(f"S7: {sum(r['verdict'] == 'passed' for r in rows)}/{len(rows)} rows passed, {len(errors)} error "
            f"finding(s), {len(findings) - len(errors)} warning(s)")
    return SuiteResult(ID, TITLE, status, summary_path, suite_metrics, findings, coverage)

