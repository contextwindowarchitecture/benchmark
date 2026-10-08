"""S11 · Producer pipeline and the optional LLM summarizer (domain-1-plan.md, 7.11 and section 9).

R-18 puts every model call before freeze, so the summarizer is a producer stage, measured in two separate ways:

1. **Assembly over frozen output.** A synthetic corpus (producers/source.py) is chunked into evidence items, and each
   arm freezes them into a snapshot: `off` with no variants, `stub` with the deterministic compressors' variants, and
   `llm` with the summarizer's (live in `llm` mode, from the cache in `replay` mode). Every arm's snapshot is swept
   from its full size down to refusal and assembled by every adapter. Each answer is judged against the outcome the
   rules give, by the trace auditor (A8: no synthesized text), by R-18's method check (each compressed row names the
   method its variant was frozen with), and by agreement; at a few budgets, every adapter answers again and must
   repeat itself byte for byte. These gate the suite.
2. **The producer itself**, which is nondeterministic by nature and so measured, never gating: fidelity pass rates
   per check, repeat stability over `repeat_k` samples per chunk, the decision flip rate (how often freezing another
   sample changes the payload or the included set), cache hit rate, cold and warm latency, token cost, and fit
   utility: how many evidence items survive (whole or as a variant) at each budget, off vs stub vs llm. Cache
   invalidation (an edited parent is never offered its old variant) is a property of the harness's cache and gates.

The run writes `summarizer/summary.json`, `summarizer/variants.jsonl` (every proposed variant and what the rules made
of it) and `summarizer/calls.jsonl` (provenance of every summarizer lookup), apart from the frozen snapshots, which
hold only the resulting variants.
"""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from itertools import combinations

from .. import adapters as adapters_mod
from .. import metrics, output, traces
from ..adapters import FAULTS
from ..canon import validity
from ..producers import chunker, compressors, fidelity, freeze, source
from ..producers.cache import Cache, CacheMiss
from ..producers.freeze import Candidate, Frozen
from ..producers.llm_summarizer import EndpointError, Summarizer
from ..rundir import now
from . import Coverage, SuiteContext, SuiteResult
from .generated import Answer, Answers, compared, partition

ID = "S11"
TITLE = "Producer pipeline and the optional LLM summarizer"
REQUIREMENTS = ["R-16", "R-18", "R-21", "R-22", "R-23"]
MODES = ("off", "stub", "llm", "replay")
ARMS = {"off": ["off"], "stub": ["off", "stub"], "llm": ["off", "stub", "llm"], "replay": ["off", "stub", "llm"]}
DEFAULTS = {
    "mode": "stub",
    "corpus_seed": 20261006,
    "documents": 6,
    "chunk_tokens": 110,
    "tokenizer": "fixture-whitespace/v1",
    "lead_words": 24,
    "extractive_ratio": 0.4,
    "fidelity_band": [0.05, 0.85],
    "repeat_k": 5,
    "sweep_step_percent": 2,
    "determinism_ratios": [1.0, 0.6, 0.3, 0.1],
    "determinism_repeats": 3,
    "flip_ratios": [0.8, 0.6, 0.4, 0.2],
    "cache": "summarizer-cache",
    "concurrency": 2,
    "judge": False,
}


def settings_of(ctx: SuiteContext) -> dict:
    out = {**DEFAULTS, **ctx.config.section("summarizer")}
    if out["mode"] not in MODES:
        raise ValueError(f"[summarizer].mode must be one of {', '.join(MODES)}, not {out['mode']!r}")
    out["repeat_k"] = max(1, int(out["repeat_k"]))
    return out


def corpus(contract, arm: str, settings: dict | None = None):
    """The `producer.off` and `producer.stub` corpora: each arm's frozen snapshot at the determinism budgets, built
    with the default settings and no model, so S2 and S10 can run assembly over producer output too."""
    from ..corpora import Snapshot

    settings = {**DEFAULTS, **(settings or {})}
    tokenizer, band = settings["tokenizer"], tuple(settings["fidelity_band"])
    chunks = chunker.chunk(source.documents(int(settings["corpus_seed"]), int(settings["documents"])),
                           int(settings["chunk_tokens"]), tokenizer)
    found = {}
    if arm == "stub":
        settings["repeat_k"] = 1
        for chunk_id, proposed in produce_stub(chunks, settings)[0].items():
            chunk = next(c for c in chunks if c.id == chunk_id)
            found[chunk_id] = [c.as_variant() for c in freeze.enforce(chunk, proposed, band, tokenizer) if c.kept]
    frozen = freeze.freeze(contract, f"s11-{arm}", chunks, found, int(settings["corpus_seed"]), tokenizer)
    return [Snapshot(f"producer.{arm}", f"{arm}@{ratio}", "case", tuple(REQUIREMENTS),
                     frozen.at(max(1, round(frozen.full * float(ratio)))))
            for ratio in settings["determinism_ratios"]]


# 1. Producing variants ------------------------------------------------------------------------------------------------

def produce_stub(chunks, settings) -> dict[int, dict[str, list[Candidate]]]:
    """Every stub, once per sample: deterministic, so the samples measure that repeat stability is exact."""
    tokenizer = settings["tokenizer"]
    out = {}
    for sample in range(settings["repeat_k"]):
        out[sample] = {}
        for chunk in chunks:
            parent = freeze.tokens(chunk.body, tokenizer)
            out[sample][chunk.id] = [Candidate(chunk.id, "stub", sample, method, compressors.LINEAGE, body,
                                               freeze.tokens(body, tokenizer), parent)
                                     for method, body in compressors.stub_variants(chunk.body, settings, tokenizer)]
    return out


def produce_llm(ctx, chunks, settings, summarizer):
    """Every chunk × sample through the summarizer, `concurrency` at a time. Returns the candidates, the lookups (for
    calls.jsonl) and the first error (a replay miss or an endpoint failure), if any."""
    tokenizer = settings["tokenizer"]
    jobs = [(chunk, sample) for sample in range(settings["repeat_k"]) for chunk in chunks]

    def work(job):
        chunk, sample = job
        try:
            return job, summarizer.summarize(chunk.body, freeze.tokens(chunk.body, tokenizer), sample), None
        except (CacheMiss, EndpointError) as error:  # reported, and the arm is not frozen
            return job, None, f"{type(error).__name__}: {error}"

    out: dict[int, dict[str, list[Candidate]]] = defaultdict(dict)
    lookups, errors = [], []
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, int(settings["concurrency"]))) as pool:
        for (chunk, sample), result, error in pool.map(work, jobs):
            done += 1
            if done % 20 == 0:
                ctx.log(f"S11: summarizer {done}/{len(jobs)}")
            if error:
                errors.append(f"{chunk.id} sample {sample}: {error}")
                continue
            body = result.text
            out[sample][chunk.id] = [Candidate(chunk.id, "llm", sample, summarizer.method, "summarised", body,
                                               freeze.tokens(body, tokenizer) if body.strip() else 0,
                                               freeze.tokens(chunk.body, tokenizer),
                                               cache={"hit": result.cache_hit, "key": result.key,
                                                      "lookup_ms": result.lookup_ms})]
            lookups.append(("summarize", chunk.id, sample, result))
    return dict(out), lookups, errors


def judge_variants(summarizer, chunks, candidates, lookups) -> dict[str, str]:
    """The optional LLM-as-judge on sample 0's summaries: SUPPORTED or UNSUPPORTED per chunk. Never gates."""
    verdicts = {}
    by_id = {c.id: c for c in chunks}
    for chunk_id, found in candidates.get(0, {}).items():
        for candidate in found:
            if not candidate.body.strip():
                continue
            result = summarizer.judge(by_id[chunk_id].body, candidate.body)
            lookups.append(("judge", chunk_id, 0, result))
            word = result.text.strip().split()[0].upper().strip(".") if result.text.strip() else ""
            verdicts[chunk_id] = word if word in ("SUPPORTED", "UNSUPPORTED") else "UNCLEAR"
    return verdicts


# 2. Measures of the producer ------------------------------------------------------------------------------------------

def word_distance(a: str, b: str) -> float:
    """Levenshtein distance over whitespace-separated words, divided by the longer length (0 equal, 1 disjoint)."""
    x, y = a.split(), b.split()
    if not x and not y:
        return 0.0
    previous = list(range(len(y) + 1))
    for i, wx in enumerate(x, 1):
        current = [i]
        for j, wy in enumerate(y, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (wx != wy)))
        previous = current
    return previous[-1] / max(len(x), len(y))


def repeat_stability(per_sample: dict[int, dict[str, list[Candidate]]]) -> dict:
    """Over every pair of samples of the same chunk and method: how often the bodies are identical, their mean
    normalized word edit distance, and per chunk the spread of token counts ((max − min) ÷ mean)."""
    bodies: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
    for found in per_sample.values():
        for candidates in found.values():
            for c in candidates:
                bodies[(c.chunk, c.method)].append(c)
    pairs = identical = 0
    distance = 0.0
    spreads = []
    for group in bodies.values():
        group.sort(key=lambda c: c.sample)
        for a, b in combinations(group, 2):
            pairs += 1
            identical += a.body == b.body
            distance += word_distance(a.body, b.body)
        counts = [c.tokens for c in group]
        mean = sum(counts) / len(counts)
        spreads.append((max(counts) - min(counts)) / mean if mean else 0.0)
    return {"groups": len(bodies), "samples": len(per_sample), "pairs": pairs, "identical": identical,
            "exact_match_rate": identical / pairs if pairs else None,
            "mean_edit_distance": round(distance / pairs, 4) if pairs else None,
            "token_spread_mean": round(sum(spreads) / len(spreads), 4) if spreads else None,
            "token_spread_max": round(max(spreads), 4) if spreads else None}


def fidelity_rates(candidates: list[Candidate]) -> dict:
    out = {}
    for check in fidelity.CHECKS:
        found = [c for c in candidates for k in c.checks if k["id"] == check]
        passed = sum(1 for c in found for k in c.checks if k["id"] == check and k["status"] == "pass")
        out[check] = {"passed": passed, "total": len(found)}
    whole = sum(fidelity.passed(c.checks) for c in candidates)
    out["all"] = {"passed": whole, "total": len(candidates)}
    return out


# 3. Assembly ----------------------------------------------------------------------------------------------------------

def sweep_budgets(frozen: Frozen, step_percent: float) -> list[int]:
    """From the full size down in steps, then exactly at the protected threshold and one below it."""
    step = max(1, round(frozen.full * step_percent / 100))
    budgets = set(range(frozen.full, frozen.protected, -step))
    budgets |= {frozen.protected, max(1, frozen.protected - 1)}
    return sorted(budgets, reverse=True)


def retained(trace: dict, evidence: set[str]) -> dict:
    included = {r.get("item_id") for r in trace.get("included") or [] if r.get("item_id") in evidence}
    compressed = {r.get("item_id") for r in trace.get("compressed") or [] if r.get("item_id") in evidence}
    omitted = {r.get("item_id") for r in trace.get("excluded") or []
               if r.get("reason") == "over_budget" and r.get("item_id") in evidence}
    tokens = sum(r.get("tokens") or 0 for r in trace.get("included") or [] if r.get("item_id") in evidence)
    return {"whole": len(included - compressed), "variant": len(included & compressed), "omitted": len(omitted),
            "evidence": len(evidence), "tokens": tokens}


def method_problems(trace: dict, methods: dict[tuple[str, str], str]) -> list[str]:
    """R-18: every compressed row names a variant frozen for that item, and the method it was frozen with."""
    out = []
    for row in trace.get("compressed") or []:
        frozen_method = methods.get((row.get("item_id"), row.get("variant_id")))
        if frozen_method is None:
            out.append(f"{row.get('item_id')}: variant {row.get('variant_id')} was not frozen for it")
        elif row.get("method") != frozen_method:
            out.append(f"{row.get('item_id')}: method {row.get('method')!r}, frozen as {frozen_method!r}")
    return out


class Findings:
    """One finding per signature (oracle, checks, arm, adapter), with every occurrence counted."""

    def __init__(self, ctx: SuiteContext):
        self.ctx = ctx
        self.by_key: dict[str, dict] = {}

    def add(self, oracle: str, checks: list[str], adapter: str | None, arm: str, case_id: str, summary: str,
            data: bytes | None, severity: str = "error") -> str:
        signature = {"suite": ID, "oracle": oracle, "checks": checks, "adapter": adapter, "arm": arm}
        key = hashlib.sha256(output.dumps(signature, compact=True).encode()).hexdigest()[:12]
        if key in self.by_key:
            self.by_key[key]["occurrences"] += 1
            return key
        self.by_key[key] = {
            "$schema": output.schema_name("finding"),
            "finding_id": key,
            "run_id": self.ctx.run.run_id,
            "suite": ID,
            "adapter": adapter,
            "case_id": case_id,
            "oracle": oracle,
            "checks": checks,
            "severity": severity,
            "summary": summary[:1000],
            "first_pointer": None,
            "requirements": REQUIREMENTS,
            "occurrences": 1,
            "reproducer": {"snapshot": self.ctx.run.blobs.put(data, "application/json"), "spec_path": None}
            if data else None,
        }
        return key

    def all(self) -> list[dict]:
        return list(self.by_key.values())


def _answer_entry(answer: Answer, expected, evidence, methods, renderer) -> dict:
    entry = answer.summary()
    trace = answer.outcome.trace if isinstance(answer.outcome.trace, dict) else None
    entry["charged_tokens"] = traces.charged_tokens(trace) if trace else None
    if answer.kind in FAULTS:
        entry["prediction"] = "skipped"
    else:
        entry["prediction"] = "pass" if (answer.kind, answer.outcome.refusal_reason) == expected else "fail"
    entry["retained"] = retained(trace, evidence) if trace and answer.kind == "assembled" else None
    entry["method_problems"] = method_problems(trace, methods) if trace else []
    entry["repeats"] = None
    # Kept only until the flip rate is computed; rows are written without it.
    entry["included_set"] = sorted({r.get("item_id") for r in trace.get("included") or []}) if trace else None
    return entry


# The suite ------------------------------------------------------------------------------------------------------------

def run(ctx: SuiteContext) -> SuiteResult:
    started = now()
    settings = settings_of(ctx)
    tokenizer = settings["tokenizer"]
    band = tuple(float(x) for x in settings["fidelity_band"])
    seed = int(settings["corpus_seed"])
    mode = settings["mode"]
    docs = source.documents(seed, int(settings["documents"]))
    chunks = chunker.chunk(docs, int(settings["chunk_tokens"]), tokenizer)
    ctx.log(f"S11: mode {mode}, {len(docs)} documents → {len(chunks)} chunks, arms {', '.join(ARMS[mode])}")

    # Variants per arm and sample, then R-18's rules and the fidelity checks.
    proposed: dict[str, dict[int, dict[str, list[Candidate]]]] = {"off": {0: {}}}
    lookups, llm_errors, judge, summarizer = [], [], {}, None
    cache_stats = None
    if "stub" in ARMS[mode]:
        proposed["stub"] = produce_stub(chunks, settings)
    if "llm" in ARMS[mode]:
        cache_path = ctx.config.root / settings["cache"]
        cache = Cache(cache_path, writable=mode == "llm")
        summarizer = Summarizer(settings, cache, mode)
        ctx.log(f"S11: summarizer {summarizer.method} at {summarizer.client.host}, cache {cache_path} "
                f"({cache.entries()} entries)")
        produced, lookups, llm_errors = produce_llm(ctx, chunks, settings, summarizer)
        first = {"hits": cache.hits, "misses": cache.misses, "corrupt": cache.corrupt}
        warm = []
        if not llm_errors:
            for chunk in chunks:  # a second pass over sample 0: every lookup must now hit
                result = summarizer.summarize(chunk.body, freeze.tokens(chunk.body, tokenizer), 0)
                warm.append(result)
            if settings["judge"]:
                judge = judge_variants(summarizer, chunks, produced, lookups)
            proposed["llm"] = produced
        cold = [r.lookup_ms for kind, _, _, r in lookups if kind == "summarize" and not r.cache_hit]
        warm_ms = [r.lookup_ms for r in warm]
        usage = Counter()
        for kind, _, _, r in lookups:
            if not r.cache_hit:
                usage.update({k: v for k, v in (r.provenance.get("usage") or {}).items() if isinstance(v, int)})
        recorded = Counter()
        for kind, _, _, r in lookups:
            recorded.update({k: v for k, v in (r.provenance.get("usage") or {}).items() if isinstance(v, int)})
        cache_stats = {
            "path": str(cache_path), "mode": mode, "entries": cache.entries(),
            "first_pass": first, "hit_rate": first["hits"] / (first["hits"] + first["misses"])
            if first["hits"] + first["misses"] else None,
            "warm_pass": {"lookups": len(warm), "hits": sum(r.cache_hit for r in warm)},
            "cold_ms": metrics.percentiles(cold), "warm_ms": metrics.percentiles(warm_ms),
            "tokens_this_run": dict(usage), "tokens_recorded": dict(recorded),
        }
    candidates: list[Candidate] = []
    variants: dict[str, dict[int, dict[str, list[dict]]]] = {}
    for arm, per_sample in proposed.items():
        variants[arm] = {}
        for sample, found in sorted(per_sample.items()):
            variants[arm][sample] = {}
            for chunk in chunks:
                mine = freeze.enforce(chunk, found.get(chunk.id, []), band, tokenizer)
                candidates += mine
                variants[arm][sample][chunk.id] = [c.as_variant() for c in mine if c.kept]

    # Freeze: one snapshot per arm and sample, sample 0 the arm's own.
    frozen: dict[str, dict[int, Frozen]] = {
        arm: {sample: freeze.freeze(ctx.contract, f"s11-{arm}", chunks, found, seed, tokenizer)
              for sample, found in per_sample.items()} for arm, per_sample in variants.items()}
    frozen_docs, invalid = [], []
    for arm, per_sample in frozen.items():
        for sample, f in per_sample.items():
            data = f.at(f.full)
            problems = validity.problems(ctx.contract, data)
            ref = ctx.run.blobs.put(data, "application/json")
            frozen_docs.append({"arm": arm, "sample": sample, "snapshot": ref, "full": f.full,
                                "protected": f.protected, "valid": not problems,
                                "variants": sum(len(v) for v in variants[arm][sample].values())})
            if problems:
                invalid.append((arm, sample, problems, data))
    methods = {arm: {(chunk_id, v["id"]): v["method"] for per in variants[arm].values()
                     for chunk_id, found in per.items() for v in found} for arm in variants}

    # Assembly jobs: every arm's own snapshot swept, the determinism budgets, and every sample at the flip budgets.
    names = list(ctx.adapters)
    answers = Answers(ctx)
    plan: dict[tuple[str, int, int], set[str]] = defaultdict(set)
    step = float(settings["sweep_step_percent"])
    for arm, per_sample in frozen.items():
        own = per_sample[0]
        for budget in sweep_budgets(own, step):
            plan[(arm, 0, budget)].add("sweep")
        for ratio in settings["determinism_ratios"]:
            plan[(arm, 0, max(1, round(own.full * float(ratio))))].add("determinism")
        if len(per_sample) > 1:
            for ratio in settings["flip_ratios"]:
                for sample in per_sample:
                    plan[(arm, sample, max(1, round(own.full * float(ratio))))].add("flip")
    data_of = {key: frozen[key[0]][key[1]].at(key[2]) for key in plan}
    ctx.log(f"S11: {len(plan)} snapshots × {len(names)} adapter(s)")
    answers.fill([(a, data_of[key]) for key in plan for a in names])

    repeats: dict[tuple, list[Answer]] = {}
    repeat_jobs = [(key, a, n) for key, purposes in plan.items() if "determinism" in purposes for a in names
                   for n in range(int(settings["determinism_repeats"]))]

    def again(job):
        key, adapter, _ = job
        invocation = adapters_mod.invoke(ctx.adapters[adapter], data_of[key], ctx.config.timeout_s, ctx.config.root)
        return job, Answer(adapter, adapters_mod.classify(invocation), invocation.exit_code, invocation.wall_ms)

    with ThreadPoolExecutor(max_workers=ctx.config.concurrency) as pool:
        for (key, adapter, _), answer in pool.map(again, repeat_jobs):
            repeats.setdefault((key, adapter), []).append(answer)

    # Rows: one per arm, sample and budget, with every adapter's answer.
    report = Findings(ctx)
    evidence = {c.id for c in chunks}
    rows, coverage = [], []
    frozen_refs = {(d["arm"], d["sample"]): d["snapshot"] for d in frozen_docs}
    for key in sorted(plan, key=lambda k: (list(variants).index(k[0]), k[1], -k[2])):
        arm, sample, budget = key
        f = frozen[arm][sample]
        data = data_of[key]
        expected = f.expect(budget)
        case_id = f"{arm}/s{sample}@{budget}"
        got = [answers.get(a, data) for a in names]
        entries = []
        row_findings = []
        for answer in got:
            entry = _answer_entry(answer, expected, evidence, methods[arm], f.snapshot["renderer"])
            again_ = repeats.get((key, answer.adapter))
            if again_ is not None:
                same = sum(a.signature == answer.signature for a in again_)
                entry["repeats"] = {"n": len(again_), "identical": same}
                if same != len(again_):
                    row_findings.append(report.add("determinism", ["repeat"], answer.adapter, arm, case_id,
                                                  f"{answer.adapter} answered {case_id} differently on "
                                                  f"{len(again_) - same} of {len(again_)} repeats", data))
            if answer.kind in FAULTS:
                row_findings.append(report.add("fault", [answer.kind], answer.adapter, arm, case_id,
                                              f"{answer.adapter} {answer.kind} on {case_id}: "
                                              f"{(answer.outcome.problem or '')[:200]}", data))
            elif entry["prediction"] == "fail":
                row_findings.append(report.add("label", ["prediction"], answer.adapter, arm, case_id,
                                              f"{answer.adapter} on {case_id}: {answer.kind} "
                                              f"{answer.outcome.refusal_reason or ''}, expected {expected[0]} "
                                              f"{expected[1] or ''}", data))
            if answer.audit_failed:
                detail = "; ".join(f"{k}: {v[0]}" for k, v in answer.audit_detail.items() if v)[:400]
                row_findings.append(report.add("auditor", sorted(answer.audit_failed), answer.adapter, arm, case_id,
                                              f"{answer.adapter} output for {case_id} breaks "
                                              f"{', '.join(answer.audit_failed)}: {detail}", data))
            if entry["method_problems"]:
                row_findings.append(report.add("producer", ["r18_method"], answer.adapter, arm, case_id,
                                              f"{answer.adapter} on {case_id}: {entry['method_problems'][0]}", data))
            trace = answer.outcome.trace if isinstance(answer.outcome.trace, dict) else None
            reasons = sorted({(r.get("reason"), r.get("slot")) for r in (trace or {}).get("excluded") or []
                              if isinstance(r, dict) and isinstance(r.get("reason"), str)}, key=str)
            if answer.outcome.refusal_reason:
                reasons.append((answer.outcome.refusal_reason, None))
            ok = (entry["prediction"] == "pass" and not answer.audit_failed and not entry["method_problems"]
                  and (entry["repeats"] is None or entry["repeats"]["n"] == entry["repeats"]["identical"]))
            coverage.append(Coverage(answer.adapter, REQUIREMENTS, reasons, traces.coverage_tags(trace, None), ok))
            entries.append(entry)
        groups = partition(compared(got, data))
        agree = len(groups) <= 1
        if not agree:
            row_findings.append(report.add("differential", ["agreement"], None, arm, case_id,
                                          f"adapters disagree on {case_id}: " + " vs ".join("/".join(g)
                                                                                        for g in groups), data))
        rows.append({
            "$schema": output.schema_name("summarizer-row"),
            "run_id": ctx.run.run_id,
            "suite": ID,
            "case_id": case_id,
            "arm": arm,
            "sample": sample,
            "purposes": sorted(plan[key]),
            "budget_input": budget,
            "frozen": frozen_refs[(arm, sample)],
            "snapshot_sha256": hashlib.sha256(data).hexdigest(),
            "expected": {"outcome": expected[0], "refusal_reason": expected[1]},
            "answers": entries,
            "agree": agree,
            "groups": groups,
            "verdict": "failed" if row_findings else "passed",
            "findings": sorted(set(row_findings)),
        })
    for arm, sample, problems, data in invalid:
        report.add("producer", ["frozen_valid"] + sorted({c for c, _ in problems}), None, arm, f"{arm}/s{sample}",
                  f"the frozen {arm} snapshot (sample {sample}) breaks {problems[0][0]}: {problems[0][1]}"[:500], data)

    # Cache invalidation: an edited parent must never be offered its old variant.
    invalidation = None
    if summarizer is not None and not llm_errors:
        edits = (("appended", lambda b: b + " This note was revised."), ("one character", lambda b: "#" + b[1:]))
        present = offered = checked = 0
        for chunk in chunks:
            for sample in range(settings["repeat_k"]):
                present += summarizer.would_offer(chunk.body, sample)
                for _, edit in edits:
                    checked += 1
                    offered += summarizer.would_offer(edit(chunk.body), sample)
        invalidation = {"edits": [name for name, _ in edits], "parents": len(chunks), "checked": checked,
                        "offered": offered, "original_present": present,
                        "original_total": len(chunks) * settings["repeat_k"]}
        if offered:
            report.add("producer", ["cache_invalidation"], None, "llm", "cache",
                      f"{offered} of {checked} edited parents were offered a cached variant", None)
    for error in llm_errors[:1]:
        report.add("producer", ["replay_miss" if "CacheMiss" in error else "endpoint"], None, "llm", "summarizer",
                  f"{len(llm_errors)} summarizer lookup(s) failed; first: {error}", None)

    return _finish(ctx, settings, started, docs, chunks, candidates, variants, frozen, frozen_docs, rows, report,
                   coverage, lookups, llm_errors, summarizer, cache_stats, judge, invalidation, names)


def _by_sample(candidates: list[Candidate]) -> dict[int, dict[str, list[Candidate]]]:
    out: dict[int, dict[str, list[Candidate]]] = defaultdict(lambda: defaultdict(list))
    for c in candidates:
        out[c.sample][c.chunk].append(c)
    return out


def _fit_utility(rows: list[dict], arm: str, reference: str | None) -> dict:
    frames = []
    for row in rows:
        if row["arm"] != arm or row["sample"] != 0 or "sweep" not in row["purposes"]:
            continue
        entry = next((e for e in row["answers"] if e["adapter"] == reference), None)
        if entry is None:
            continue
        r = entry["retained"]
        frames.append({"budget_input": row["budget_input"], "outcome": entry["outcome"],
                       "whole": r["whole"] if r else 0, "variant": r["variant"] if r else 0,
                       "omitted": r["omitted"] if r else None, "evidence_tokens": r["tokens"] if r else 0,
                       "retained_rate": (r["whole"] + r["variant"]) / r["evidence"] if r and r["evidence"] else 0.0})
    frames.sort(key=lambda f: -f["budget_input"])
    assembled = [f for f in frames if f["outcome"] == "assembled"]
    whole_all = [f["budget_input"] for f in assembled if f["retained_rate"] == 1.0]
    return {"adapter": reference, "frames": frames,
            "mean_retained_rate": round(sum(f["retained_rate"] for f in assembled) / len(assembled), 4)
            if assembled else None,
            "all_retained_down_to": min(whole_all) if whole_all else None,
            "first_omission_at": max((f["budget_input"] for f in assembled if f["omitted"]), default=None)}


def _flip(rows: list[dict], arm: str, names: list[str]) -> dict:
    """For each flip budget and adapter: over every pair of samples, how often the payload or the included set
    differs when another sample's variants are frozen."""
    by_budget: dict[int, dict[int, dict]] = defaultdict(dict)
    for row in rows:
        if row["arm"] == arm and "flip" in row["purposes"]:
            by_budget[row["budget_input"]][row["sample"]] = row
    out = []
    for budget, per_sample in sorted(by_budget.items(), reverse=True):
        for adapter in names:
            answers = {s: next(e for e in r["answers"] if e["adapter"] == adapter) for s, r in per_sample.items()}
            pairs = payload = included = either = 0
            for a, b in combinations(sorted(answers), 2):
                x, y = answers[a], answers[b]
                pairs += 1
                p = x["payload_hash"] != y["payload_hash"]
                i = x["included_set"] != y["included_set"]
                payload += p
                included += i
                either += p or i
            out.append({"budget_input": budget, "adapter": adapter, "samples": len(answers), "pairs": pairs,
                        "payload_flips": payload, "included_flips": included, "flips": either,
                        "flip_rate": either / pairs if pairs else None})
    return {"budgets": out, "pairs": sum(b["pairs"] for b in out), "flips": sum(b["flips"] for b in out)}


def _finish(ctx, settings, started, docs, chunks, candidates, variants, frozen, frozen_docs, rows, report, coverage,
            lookups, llm_errors, summarizer, cache_stats, judge, invalidation, names) -> SuiteResult:
    base = f"suites/{ID}"
    blobs = ctx.run.blobs
    reference = names[0] if names else None
    arms = list(variants)
    summary_arms = {}
    for arm in arms:
        mine = [c for c in candidates if c.arm == arm]
        own = frozen[arm][0]
        entry = {
            "variants_proposed": len(mine),
            "variants_kept": sum(c.kept for c in mine),
            "dropped": dict(sorted(Counter(c.dropped for c in mine if c.dropped).items())),
            "fidelity": fidelity_rates(mine),
            "repeat_stability": repeat_stability(_by_sample(mine)) if mine else None,
            "full": own.full,
            "protected": own.protected,
            "fit_utility": _fit_utility(rows, arm, reference),
            "flip": None,
            "frozen": [d for d in frozen_docs if d["arm"] == arm],
        }
        summary_arms[arm] = entry

    for arm in arms:
        if len(frozen[arm]) > 1:
            summary_arms[arm]["flip"] = _flip(rows, arm, names)

    suite_metrics = []
    for adapter in names:
        mine = [(r, e) for r in rows for e in r["answers"] if e["adapter"] == adapter]
        judged = [e for _, e in mine if e["prediction"] != "skipped"]
        suite_metrics.append(metrics.rate("s11.prediction", "Answers with the predicted outcome",
                                          sum(e["prediction"] == "pass" for e in judged), len(judged), suite=ID,
                                          adapter=adapter, description="Assembled while the protected items fit, "
                                          "refused protected_content_over_budget below that, at every budget."))
        audited = [e for _, e in mine if e["outcome"] in ("assembled", "refused")]
        suite_metrics.append(metrics.rate("s11.audit_rate", "Outputs passing the trace audit",
                                          sum(not e["audit_failed"] for e in audited), len(audited), suite=ID,
                                          adapter=adapter, description="A1–A16, including A8: every rendered body is "
                                          "a supplied body or frozen variant, byte for byte."))
        compressed = [e for _, e in mine if e["retained"] and e["retained"]["variant"] or e["method_problems"]]
        suite_metrics.append(metrics.rate("s11.method_recorded", "Compressed rows naming their frozen method",
                                          sum(not e["method_problems"] for e in compressed), len(compressed),
                                          suite=ID, adapter=adapter, description="R-18: the trace identifies each "
                                          "selected variant and the method it was frozen with."))
        repeated = [e["repeats"] for _, e in mine if e["repeats"]]
        suite_metrics.append(metrics.rate("s11.determinism", "Repeated answers identical over frozen output",
                                          sum(r["identical"] for r in repeated), sum(r["n"] for r in repeated),
                                          suite=ID, adapter=adapter, description="The same frozen snapshot, assembled "
                                          "again, gives the same decision, payload and normalized trace (R-23)."))
    suite_metrics.append(metrics.rate("s11.agreement", "Snapshots on which every adapter agrees",
                                      sum(r["agree"] for r in rows), len(rows), suite=ID))
    suite_metrics.append(metrics.rate("s11.frozen_valid", "Frozen snapshots passing every snapshot check",
                                      sum(d["valid"] for d in frozen_docs), len(frozen_docs), suite=ID))
    suite_metrics.append(metrics.rate("s11.cache_invalidation", "Edited parents never offered a cached variant",
                                      invalidation["checked"] - invalidation["offered"] if invalidation else 0,
                                      invalidation["checked"] if invalidation else 0, suite=ID,
                                      description="Null without an LLM arm: only the summarizer's cache is keyed."))
    llm = summary_arms.get("llm")
    stability = llm["repeat_stability"] if llm else None
    suite_metrics.append(metrics.rate("s11.repeat_stability", "Summarizer repeat stability (exact match)",
                                      stability["identical"] if stability else 0,
                                      stability["pairs"] if stability else 0, target=None, suite=ID,
                                      description="Pairs of samples of one chunk with identical summaries; null when "
                                                  "the summarizer was off."))
    flips = llm["flip"] if llm and llm["flip"] else None
    ref_flips = [b for b in flips["budgets"] if b["adapter"] == reference] if flips else []
    suite_metrics.append(metrics.rate("s11.flip_rate", "Summarizer decision flip rate",
                                      sum(b["flips"] for b in ref_flips), sum(b["pairs"] for b in ref_flips),
                                      target=None, suite=ID, adapter=reference if flips else None,
                                      description="Pairs of sample snapshots whose payload or included set differs, "
                                                  "over the flip budgets; null when the summarizer was off."))
    first = cache_stats["first_pass"] if cache_stats else {"hits": 0, "misses": 0}
    suite_metrics.append(metrics.rate("s11.cache_hit_rate", "Summarizer cache hit rate", first["hits"],
                                      first["hits"] + first["misses"], target=None, suite=ID,
                                      description="First pass over every chunk and sample; null when the summarizer "
                                                  "was off."))
    fid = llm["fidelity"]["all"] if llm else {"passed": 0, "total": 0}
    suite_metrics.append(metrics.rate("s11.fidelity", "Summarizer fidelity pass rate", fid["passed"], fid["total"],
                                      target=None, suite=ID, description="LLM variants passing every deterministic "
                                      "fidelity check; null when the summarizer was off."))
    for arm in arms:
        frames = [f for f in summary_arms[arm]["fit_utility"]["frames"] if f["outcome"] == "assembled"]
        kept = sum(f["whole"] + f["variant"] for f in frames)
        total = sum(f["whole"] + f["variant"] + (f["omitted"] or 0) for f in frames)
        suite_metrics.append(metrics.rate(f"s11.fit_utility.{arm}", f"Evidence retained over the sweep ({arm})",
                                          kept, total, target=None, suite=ID, adapter=reference,
                                          description="Evidence items kept whole or as a variant, summed over every "
                                                      "assembled budget of the arm's sweep."))

    findings = report.all()
    errors = [f for f in findings if f["severity"] == "error"]
    if llm_errors:
        status = "error"
    elif errors:
        status = "fail"
    else:
        status = "partial" if ctx.unavailable else "pass"

    # Files: rows, findings, variants, calls, the summarizer summary, the suite summary.
    for row in rows:
        for entry in row["answers"]:
            entry.pop("included_set", None)
    ctx.run.write_jsonl(f"{base}/results.jsonl", rows, "summarizer-row",
                        "S11: one row per arm, sample and budget, every adapter's answer")
    ctx.run.write_jsonl(f"{base}/findings.jsonl", findings, "finding", "S11: assembly, cache and summarizer findings")
    variant_rows = [{
        "$schema": output.schema_name("summarizer-variant"),
        "run_id": ctx.run.run_id,
        "arm": c.arm,
        "sample": c.sample,
        "chunk": c.chunk,
        "variant_id": c.id,
        "method": c.method,
        "lineage": c.lineage,
        "tokens": c.tokens,
        "parent_tokens": c.parent_tokens,
        "kept": c.kept,
        "dropped": c.dropped,
        "failed_checks": [k["id"] for k in c.checks if k["status"] != "pass"],
        "checks": c.checks,
        "cache": c.cache,
        "judge": judge.get(c.chunk) if c.arm == "llm" and c.sample == 0 else None,
        "body": blobs.put_text(c.body.encode("utf-8")) if c.body else None,
    } for c in candidates]
    ctx.run.write_jsonl("summarizer/variants.jsonl", variant_rows, "summarizer-variant",
                        "S11: every proposed variant, its fidelity checks and whether R-18's rules kept it")
    call_rows = [{
        "$schema": output.schema_name("summarizer-call"),
        "run_id": ctx.run.run_id,
        "kind": kind,
        "chunk": chunk_id,
        "sample": sample,
        "key": result.key,
        "cache_hit": result.cache_hit,
        "lookup_ms": result.lookup_ms,
        "provenance": result.provenance,
    } for kind, chunk_id, sample, result in lookups]
    ctx.run.write_jsonl("summarizer/calls.jsonl", call_rows, "summarizer-call",
                        "S11: provenance of every summarizer lookup, live or from the cache")
    client = summarizer.client if summarizer else None
    summarizer_doc = {
        "$schema": output.schema_name("summarizer-summary"),
        "run_id": ctx.run.run_id,
        "mode": settings["mode"],
        "arms": arms,
        "summarizer": None if summarizer is None else {
            "method": summarizer.method, "model": client.model, "endpoint_host": client.host,
            "params": summarizer.params, "params_sha256": summarizer.params_sha256,
            "prompt_id": "summarize/v1", "prompt_sha256": summarizer.prompt_sha256,
            "target_ratio": summarizer.ratio, "repeat_k": settings["repeat_k"], "judge": bool(settings["judge"]),
        },
        "errors": llm_errors[:20],
        "corpus": {
            "generator": "cwabench.producers.source", "seed": int(settings["corpus_seed"]), "documents": len(docs),
            "chunker": {"max_tokens": int(settings["chunk_tokens"]), "tokenizer": settings["tokenizer"]},
            "chunks": [{"id": c.id, "tokens": c.tokens, "sha256": hashlib.sha256(c.body.encode()).hexdigest()}
                       for c in chunks],
        },
        "stub": {"lead_words": int(settings["lead_words"]), "extractive_ratio": float(settings["extractive_ratio"])},
        "fidelity_band": [float(x) for x in settings["fidelity_band"]],
        "by_arm": summary_arms,
        "cache": cache_stats,
        "invalidation": invalidation,
        "judge": None if not judge else {"verdicts": dict(sorted(Counter(judge.values()).items())),
                                         "agrees_with_fidelity": _judge_agreement(judge, candidates)},
        "files": {"variants": "summarizer/variants.jsonl", "calls": "summarizer/calls.jsonl"},
    }
    ctx.run.write_json("summarizer/summary.json", summarizer_doc,
                       "S11: the producer measured: fidelity, stability, flips, cache, fit utility")
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
        "corpora": [{"id": f"summarizer.{arm}", "count": len(frozen[arm])} for arm in arms],
        "adapters": [],
        "metrics": suite_metrics,
        "files": {"results": f"{base}/results.jsonl", "findings": f"{base}/findings.jsonl"},
        "summarizer": {
            "mode": settings["mode"], "arms": arms, "chunks": len(chunks), "rows": len(rows),
            "summary": "summarizer/summary.json",
            "fit_utility": {arm: {k: v for k, v in summary_arms[arm]["fit_utility"].items() if k != "frames"}
                            for arm in arms},
        },
    }, "S11: assembly over frozen producer output, and the producer's own measures")
    ctx.log(f"S11: {sum(r['verdict'] == 'passed' for r in rows)}/{len(rows)} rows passed, {len(errors)} error "
            f"finding(s)")
    return SuiteResult(ID, TITLE, status, summary_path, suite_metrics, findings, coverage)


def _judge_agreement(judge: dict[str, str], candidates: list[Candidate]) -> dict:
    out = Counter()
    for c in candidates:
        if c.arm == "llm" and c.sample == 0 and c.chunk in judge:
            out[f"{'pass' if fidelity.passed(c.checks) else 'fail'}/{judge[c.chunk].lower()}"] += 1
    return dict(sorted(out.items()))
