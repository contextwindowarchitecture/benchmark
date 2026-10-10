"""S6 · Model in the loop (domain-2-plan.md, 4.3 and 8): the study's setup, where the model's reply at turn t − 1 is
in the payload at turn t.

**A chain** is one conversation of `[s6].families`, in one arm of `[s6].arms`, at `[s6].tier`, for one sample k. At
each turn the arm's payload, with the user's turn as its query and the chain's own replies as the earlier assistant
turns, goes to the model as sample k at `[s6].temperature`; the reply (its reasoning removed, or "(no reply)" when it
is blank, since an item's body may not be) takes the scripted reply's place, and the next turn begins. After each
checkpoint turn the probe is asked with the chain's history, graded as S2 grades it, and, as in S2, never enters the
history. Samples fork: with `[model].seed_per_sample` each sends its own seed, so the k chains of a conversation and
arm differ from their first sampled reply on. A chain is cached as a chain, call by call, each keyed by its request
and sample, and a replay walks it again reply by reply.

**What the application does** is unchanged: the state writers, the memory producer and the fact-in-payload oracle read
the user's turns, never the replies, so the extractor's state (application/producers.py) is shared by every chain. The
rolling summary reads the replies, so each `summary` chain has its own, the summarizer called as each turn leaves the
window, at the producers' settings (sample 0, temperature as configured).

**The gate**, inline: every CWA payload is assembled by every adapter and judged as S1 judges a snapshot (agreement,
the audit, the prediction and the fact oracle) before it is sent. A payload that fails the gate, or an assembly that
refuses, halts the chain and its later probes are left out and counted. A baseline payload that overflows halts it
too, and its later probes grade `overflow`, as in S2.

What gates the suite is the harness: every CWA payload through the gate, every call answered, and no call reading
more prompt tokens than its budget (S5 runs earlier, so S6 checks its own). Measured, never gated: S2's metrics per
arm (aptitude with a cluster bootstrap over conversations, the stale rate, fact-in-payload, paired differences from
`[s6].reference`), the reply length by turn (the study's answer bloat), and, when S2 or S3 ran in the same run, each
arm's aptitude against the scripted result on the same probes: S3's when it ran at S6's temperature, else S2's.
"""
from __future__ import annotations

import hashlib
import threading
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass, field

from cwabench.rundir import now

from .. import baselines, metrics, output, stats
from ..application import fact as fact_mod
from ..application import producers as producers_mod
from ..application.snapshots import ARMS, Point, freeze
from ..grading import grade
from ..grading.compliance import complies
from ..grading.normalize import strip_reasoning
from ..model import CacheMiss, EndpointError, Model
from . import SuiteContext, SuiteResult, finding, produced
from .s1_gate import Report, _candidates, _init, _judge, assemble
from .s2_scripted import VERDICTS, _expected

ID = "S6"
TITLE = "Model in the loop"
BLANK = "(no reply)"


@dataclass
class Chain:
    script: dict
    family: str
    arm: str
    sample: int
    replies: dict[int, str] = field(default_factory=dict)  # turn → the model's reply, as the history holds it
    summary: str = ""
    summaries: dict[int, str] = field(default_factory=lambda: {0: ""})  # the summary arm's, after turn j
    turns: list[dict] = field(default_factory=list)  # chain-row documents
    grades: list[dict] = field(default_factory=list)
    calls: list[tuple] = field(default_factory=list)  # (payload sha256, sample, Reply)
    halted: str | None = None
    excluded: int = 0  # probes left out after a halt that grades no overflow
    blank: int = 0

    @property
    def id(self) -> str:
        return f"{self.script['conversation_id']}/{self.arm}#{self.sample}"


def with_replies(chain: Chain) -> dict:
    """The script with the chain's replies in place of the scripted assistant turns they replace."""
    turns = [dict(t, assistant=chain.replies[t["turn"]]) if t["turn"] in chain.replies else t
             for t in chain.script["turns"]]
    return {**chain.script, "turns": turns}


class Loop:
    """One S6 run: its models, the gate's process pool, and what the chains share."""

    def __init__(self, ctx: SuiteContext, pool, report: Report):
        config = ctx.config
        self.ctx, self.pool, self.report = ctx, pool, report
        self.settings = config.s6
        self.budget = int(self.settings["tier"])
        self.model = Model({**config.model, "temperature": self.settings["temperature"]}, config.root,
                           config.model["mode"])
        self.producer = Model({**config.model, "max_tokens": config.model["producer_max_tokens"]}, config.root,
                              config.model["mode"])
        self.tally = defaultdict(lambda: [0, 0])
        self.lock = threading.Lock()

    def _summaries(self, chain: Chain, upto: int) -> None:
        """The chain's rolling summary through turn `upto`, each turn summarized with the chain's own reply."""
        while len(chain.summaries) - 1 < upto:
            turn = chain.script["turns"][len(chain.summaries) - 1]
            chain.summary, _, reply = producers_mod.summary_step(
                self.producer, chain.script, chain.summary, turn["user"], chain.replies[turn["turn"]],
                self.ctx.config.baseline.summary_words)
            chain.summaries[turn["turn"]] = chain.summary
            chain.calls.append((None, 0, reply))

    def payload(self, chain: Chain, point: Point) -> tuple:
        """(payload or None, budget, input tokens, fact present, outcome) for the chain at a point; the outcome is
        `answered` when the payload can be sent, else `overflow`, `refused` or `gate_failed`."""
        config = self.ctx.config
        script = with_replies(chain)
        needs = point.probe["needs"] if point.probe else None
        if chain.arm in baselines.ARMS:
            chained = None
            if chain.arm == "summary" and config.baseline.summarizer == "llm":
                history = point.turn if point.kind == "probe" else point.turn - 1
                self._summaries(chain, max(0, history - config.baseline.window_turns))
                chained = producers_mod.Produced(summaries=chain.summaries)
            built = baselines.at(chain.arm, script, point, self.budget, config.baseline, chained)
            present = all(fact_mod.by_trace(set(built.kept), built.carriers, needs)) if needs else None
            if built.outcome != "fits":
                return None, self.budget, built.input_tokens, present, "overflow"
            return built.payload, self.budget, built.input_tokens, present, "answered"
        arm = ARMS[chain.arm]
        if arm.selection is not None:  # the format control: the window baseline's selection, at its own full size
            built = baselines.at(arm.selection, script, point, self.budget, config.baseline)
            frozen = freeze(self.ctx.contract, script, arm, point, config.application, only=set(built.kept))
            budget = frozen.full
        else:
            frozen = freeze(self.ctx.contract, script, arm, point, config.application,
                            produced=produced(self.ctx, chain.script))
            budget = self.budget
        expected = frozen.expect(budget)
        data = frozen.at(budget)
        fact = {"needs": needs, "carriers": frozen.carriers, "evidence": frozen.evidence} if needs else None
        job = {"data": data, "adapters": list(self.ctx.adapters), "timeout": config.timeout_s,
               "cwd": str(config.root), "payload_source": config.adapters.payload_source, "fact": fact, "keep": False,
               "candidates": _candidates(frozen),
               "expected": {"outcome": expected.outcome, "refusal_reason": expected.refusal_reason,
                            "payload_hash": expected.payload_hash, "input_tokens": expected.input_tokens,
                            "included": sorted(expected.included)}}
        result = self.pool.submit(assemble, job).result()
        with self.lock:
            found = _judge(self.report, chain.arm, f"{chain.id}/{point.id}", expected, data, result, self.tally)
        if found:
            return None, budget, None, None, "gate_failed"
        if expected.outcome != "assembled":
            return None, budget, None, None, "refused"
        present = result["fact"]["present"] if result["fact"] is not None else None
        return expected.payload, budget, expected.input_tokens, present, "answered"

    def run(self, chain: Chain) -> Chain:
        ctx = self.ctx
        probes = defaultdict(list)
        for probe in chain.script["probes"]:
            probes[probe["after_turn"]].append(probe)
        for turn in chain.script["turns"]:
            k = turn["turn"]
            if chain.halted is not None:
                self._left_out(chain, probes[k])
                continue
            data, budget, tokens, _, outcome = self.payload(chain, Point("turn", k))
            row = {"$schema": output.schema_name("chain-row"), "run_id": ctx.run.run_id, "suite": ID,
                   "case_id": f"{chain.id}/t{k:03d}", "chain": chain.id,
                   "conversation": chain.script["conversation_id"], "family": chain.family, "arm": chain.arm,
                   "tier": self.settings["tier"], "budget_input": budget, "sample": chain.sample, "turn": k,
                   "outcome": outcome, "payload_sha256": hashlib.sha256(data).hexdigest() if data else None,
                   "request_sha256": None, "input_tokens": tokens, "reply": None, "blank": None,
                   "finish_reason": None, "completion_tokens": None, "prompt_tokens": None}
            if outcome == "answered":
                try:
                    reply = self.model.ask(data, chain.sample)
                except (CacheMiss, EndpointError) as error:
                    row["outcome"] = "call_failed"
                    chain.halted = f"turn {k}: {type(error).__name__}: {error}"
                else:
                    text = strip_reasoning(reply.text)
                    usage = reply.provenance.get("usage") or {}
                    chain.replies[k] = text if text.strip() else BLANK
                    chain.blank += not text.strip()
                    chain.calls.append((row["payload_sha256"], chain.sample, reply))
                    row.update(request_sha256=reply.request_sha256, reply=reply.text[:2000], blank=not text.strip(),
                               finish_reason=reply.provenance.get("finish_reason"),
                               completion_tokens=usage.get("completion_tokens"),
                               prompt_tokens=usage.get("prompt_tokens"))
            else:
                chain.halted = f"turn {k}: {outcome}"
            chain.turns.append(row)
            if chain.halted is not None:
                self._left_out(chain, probes[k], outcome)
                continue
            for probe in probes[k]:
                self._probe(chain, probe)
        return chain

    def _left_out(self, chain: Chain, probes: list[dict], outcome: str | None = None) -> None:
        """Probes after a halt: an overflow grades as in S2; any other halt leaves them out."""
        first = next((r["outcome"] for r in chain.turns if r["outcome"] != "answered"), outcome)
        for probe in probes:
            if first == "overflow":
                chain.grades.append(self._grade_row(chain, probe, self.budget, None, None, None, None))
            else:
                chain.excluded += 1

    def _probe(self, chain: Chain, probe: dict) -> None:
        data, budget, tokens, present, outcome = self.payload(chain, Point("probe", probe["after_turn"], probe))
        if outcome == "overflow":
            chain.grades.append(self._grade_row(chain, probe, budget, None, tokens, present, None))
            return
        if outcome != "answered":
            chain.excluded += 1
            return
        try:
            reply = self.model.ask(data, chain.sample)
        except (CacheMiss, EndpointError) as error:
            chain.halted = f"probe {probe['probe_id']}: {type(error).__name__}: {error}"
            chain.excluded += 1
            return
        chain.calls.append((hashlib.sha256(data).hexdigest(), chain.sample, reply))
        chain.grades.append(self._grade_row(chain, probe, budget, data, tokens, present, reply))

    def _grade_row(self, chain: Chain, probe: dict, budget, data, tokens, present, reply) -> dict:
        script = chain.script
        base = {"$schema": output.schema_name("grade-row"), "run_id": self.ctx.run.run_id, "suite": ID,
                "case_id": f"{chain.id}/{probe['probe_id']}", "conversation": script["conversation_id"],
                "family": chain.family, "task": script["ground_truth"]["task"], "turn_count": script["turn_count"],
                "arm": chain.arm, "probe_id": probe["probe_id"], "after_turn": probe["after_turn"],
                "tier": self.settings["tier"], "budget_input": budget, "sample": chain.sample,
                "payload_sha256": hashlib.sha256(data).hexdigest() if data else None, "input_tokens": tokens,
                "fact_present": present, "attributes": probe["attributes"], "expected": _expected(probe["answer"]),
                "rule": (script.get("rule") or {}).get("id")}
        if reply is None:
            return {**base, "request_sha256": None, "reply": None, "verdict": "overflow", "score": 0.0,
                    "answer": None, "detail": "the payload overflows the budget", "fields": None,
                    "finish_reason": None, "completion_tokens": None, "prompt_tokens": None, "compliant": None}
        graded = grade(probe["answer"], reply.text)
        usage = reply.provenance.get("usage") or {}
        return {**base, "request_sha256": reply.request_sha256, "reply": reply.text[:2000], **graded.as_json(),
                "finish_reason": reply.provenance.get("finish_reason"),
                "completion_tokens": usage.get("completion_tokens"), "prompt_tokens": usage.get("prompt_tokens"),
                "compliant": complies(base["rule"], reply.text) if base["rule"] else None}


def run(ctx: SuiteContext) -> SuiteResult:
    started = now()
    config = ctx.config
    settings = config.s6
    report = Report(ctx, ID)
    chains = [Chain(script, family, arm, sample)
              for family in settings["families"] for script in ctx.conversations.get(family, [])
              for arm in settings["arms"] for sample in range(settings["repeats"])]
    contract_args = (ctx.contract.path, config.contract_commit, config.allow_dirty)
    ctx.log(f"{ID}: {len(chains)} chains ({', '.join(settings['families'])} × {len(settings['arms'])} arms × "
            f"{settings['repeats']} samples) at {settings['tier']}, temperature {settings['temperature']}")
    with ProcessPoolExecutor(max_workers=config.concurrency, initializer=_init,
                             initargs=(ctx.adapters, contract_args)) as pool:
        loop = Loop(ctx, pool, report)
        ctx.log(f"{ID}: model {loop.model.client.model} at {loop.model.client.host}, mode {loop.model.mode}")
        done = []
        with ThreadPoolExecutor(max_workers=max(1, int(config.model.get("concurrency", 2)))) as threads:
            for chain in threads.map(loop.run, chains):
                done.append(chain)
                if len(done) % 10 == 0 or len(done) == len(chains):
                    ctx.log(f"{ID}: {len(done)}/{len(chains)} chains ({loop.model.calls + loop.producer.calls} "
                            f"calls to the endpoint)")

    turn_rows = [r for chain in done for r in chain.turns]
    grades = [g for chain in done for g in chain.grades]
    seen, call_rows = set(), []
    for chain in done:
        for sha, sample, reply in chain.calls:
            if (reply.request_sha256, sample) in seen:
                continue
            seen.add((reply.request_sha256, sample))
            call_rows.append({"$schema": output.schema_name("call-row"), "run_id": ctx.run.run_id, "suite": ID,
                              "key": reply.key, "request_sha256": reply.request_sha256,
                              "payload_sha256": sha or reply.provenance.get("payload_sha256"), "sample": sample,
                              "cache_hit": reply.cache_hit, "lookup_ms": reply.lookup_ms,
                              "provenance": reply.provenance})
    call_rows.sort(key=lambda r: (r["request_sha256"], r["sample"]))
    ctx.run.write_jsonl(f"suites/{ID}/turns.jsonl", turn_rows, "chain-row",
                        "One row per chain and turn: the payload sent, the model's reply that became the history, "
                        "or why the chain halted")
    ctx.run.write_jsonl(f"suites/{ID}/grades.jsonl", grades, "grade-row",
                        "One row per chain and probe: the reply and its grade, as S2's")
    ctx.run.write_jsonl("model/s6-calls.jsonl", call_rows, "call-row",
                        "Provenance of every model call S6 made or replayed, the summarizer's included: one row per "
                        "distinct request and sample")

    findings = list(report.found.values())
    failed = [c for c in done if c.halted and ("CacheMiss" in c.halted or "EndpointError" in c.halted)]
    if failed:
        findings.append(finding(ctx, ID, {"suite": ID, "check": "calls"}, case_id=failed[0].id, oracle="producer",
                                checks=["replay_miss" if "CacheMiss" in failed[0].halted else "endpoint"],
                                summary=f"{len(failed)} chain(s) halted on a model call; first: "
                                        f"{failed[0].halted[:300]}"))
    # S5 runs before S6, so S6 checks its own calls against their budgets: its histories hold the model's own text,
    # which the token estimate under-counts most (domain-2-plan.md, 17.5)
    overruns = [r for r in [*turn_rows, *grades] if r["budget_input"] is not None
                and isinstance(r["prompt_tokens"], int) and r["prompt_tokens"] > r["budget_input"]]
    if overruns:
        findings.append(finding(ctx, ID, {"suite": ID, "check": "budget_overrun"}, case_id=overruns[0]["case_id"],
                                oracle="producer", checks=["budget_overrun"], requirements=["R-16"],
                                summary=f"{len(overruns)} call(s) read more prompt tokens than their budget; first "
                                        f"{overruns[0]['case_id']}: {overruns[0]['prompt_tokens']} > "
                                        f"{overruns[0]['budget_input']}"))
    suite_metrics, by_arm = _measure(config, grades, turn_rows, ctx.shared)
    suite_metrics.insert(0, metrics.count("s6.budget_overruns", "Calls whose server prompt exceeds their budget",
                                          len(overruns), maximum=0, suite=ID, description="Turns and probes (R-16)"))
    tally = loop.tally
    for name, label in (("agreement", "Snapshots every adapter answered alike"),
                        ("audit", "Answers that pass the trace audit"),
                        ("prediction", "Answers equal to the prediction")):
        suite_metrics.append(metrics.rate(f"s6.{name}", label, *tally[name], suite=ID))
    halted = Counter(c.halted.split(": ", 1)[1].split(":")[0] if c.halted else "completed" for c in done)
    suite_metrics.insert(0, metrics.count("s6.call_errors", "Chains halted on a failed or missing model call",
                                          len(failed), maximum=0, suite=ID))
    suite_metrics.append(metrics.count("s6.blank_replies", "Blank replies stored as (no reply)",
                                       sum(c.blank for c in done), suite=ID))
    status = "fail" if findings else "pass"
    summary_path = f"suites/{ID}/summary.json"
    ctx.run.write_json(summary_path, {
        "$schema": output.schema_name("suite-summary"),
        "run_id": ctx.run.run_id, "suite": ID, "title": TITLE, "status": status, "started_at": started,
        "finished_at": now(), "requirements": [],
        "corpora": [{"id": f, "count": len(ctx.conversations.get(f, []))} for f in settings["families"]],
        "adapters": [], "metrics": suite_metrics,
        "files": {"results": f"suites/{ID}/grades.jsonl", "findings": "findings.jsonl"},
        "model": {"model": loop.model.client.model, "endpoint_host": loop.model.client.host,
                  "mode": loop.model.mode, "params": loop.model.params, "cache": str(loop.model.cache.path),
                  "cache_entries": loop.model.cache.entries(), "context_limit": config.model.get("context_limit"),
                  "server": loop.model.server(), "concurrency": max(1, int(config.model.get("concurrency", 2))),
                  "calls": len(call_rows), "endpoint_calls": loop.model.calls + loop.producer.calls,
                  "cache_hits": sum(r["cache_hit"] for r in call_rows),
                  "chains": {"total": len(done), "by_end": dict(sorted(halted.items())),
                             "probes_left_out": sum(c.excluded for c in done)}},
        "by_arm": by_arm,
    }, f"{ID}'s grades by arm, with intervals, paired differences and the scripted comparison")
    ctx.shared["s6_grades"], ctx.shared["s6_calls"] = grades, call_rows
    return SuiteResult(ID, TITLE, status, summary_path, suite_metrics, findings)


def _scripted(config, shared: dict) -> tuple[str | None, list[dict]]:
    """The scripted suite S6 compares with: S3 when it ran at S6's temperature, else S2 when it ran."""
    if shared.get("s3_grades") and config.s3["temperature"] == config.s6["temperature"]:
        return "S3", shared["s3_grades"]
    if shared.get("s2_grades"):
        return "S2", shared["s2_grades"]
    return None, []


def _measure(config, grades: list[dict], turn_rows: list[dict], shared: dict) -> tuple[list[dict], list[dict]]:
    settings = config.s6
    resamples, seed = settings["bootstrap_resamples"], settings["bootstrap_seed"]
    order = {arm: i for i, arm in enumerate(settings["arms"])}
    groups = defaultdict(list)
    for row in grades:
        groups[row["arm"]].append(row)
    reference = {(r["conversation"], r["probe_id"], r["sample"]): r["verdict"] == "correct"
                 for r in grades if r["arm"] == settings["reference"]}
    suite, scripted_rows = _scripted(config, shared)
    scripted = defaultdict(list)
    for r in scripted_rows:
        if r["tier"] == settings["tier"]:
            scripted[(r["arm"], r["conversation"], r["probe_id"])].append(r["verdict"] == "correct")
    replies = defaultdict(lambda: defaultdict(list))
    for r in turn_rows:
        if isinstance(r["completion_tokens"], int):
            replies[r["arm"]][(r["turn"] - 1) // 10 * 10 + 1].append(r["completion_tokens"])
    out, by_arm = [], []
    for arm, rows in sorted(groups.items(), key=lambda kv: order[kv[0]]):
        verdicts = Counter(r["verdict"] for r in rows)
        clusters = defaultdict(list)
        for r in rows:
            clusters[r["conversation"]].append(1.0 if r["verdict"] == "correct" else 0.0)
        interval = stats.bootstrap(clusters, resamples, seed)
        aptitude = metrics.rate("s6.aptitude", "Correct answers", verdicts["correct"], len(rows), target=None,
                                suite=ID, arm=arm, tier=settings["tier"],
                                description="All probes and chains; the interval is a cluster bootstrap over "
                                            "conversations")
        aptitude["interval"] = interval
        out.append(aptitude)
        out.append(metrics.rate("s6.stale_rate", "Answers giving an earlier value", verdicts["stale"], len(rows),
                                target=None, suite=ID, arm=arm, tier=settings["tier"]))
        paired = None
        if arm != settings["reference"]:
            differences = defaultdict(list)
            for r in rows:
                other = reference.get((r["conversation"], r["probe_id"], r["sample"]))
                if other is not None:
                    differences[r["conversation"]].append((r["verdict"] == "correct") - other)
            n = sum(len(v) for v in differences.values())
            if n:
                paired = {"reference": settings["reference"], "n": n,
                          "difference": round(sum(sum(v) for v in differences.values()) / n, 6),
                          "interval": stats.bootstrap(differences, resamples, seed)}
        against = None
        if suite is not None:  # per probe: the loop's share correct less the script's, over the samples of each
            mine = defaultdict(list)
            for r in rows:
                mine[(r["conversation"], r["probe_id"])].append(r["verdict"] == "correct")
            differences = defaultdict(list)
            for (conversation, probe), values in mine.items():
                theirs = scripted.get((arm, conversation, probe))
                if theirs:
                    differences[conversation].append(sum(values) / len(values) - sum(theirs) / len(theirs))
            n = sum(len(v) for v in differences.values())
            if n:
                difference = sum(sum(v) for v in differences.values()) / n
                against = {"suite": suite, "probes": n, "difference": round(difference, 6),
                           "interval": stats.bootstrap(differences, resamples, seed)}
                out.append(metrics.value("s6.against_scripted", f"Aptitude in the loop less {suite}'s scripted",
                                         round(difference, 6), "rate", suite=ID, arm=arm, tier=settings["tier"],
                                         description="Per probe, the share correct over the samples, averaged; "
                                                     "negative when the model's own replies hurt"))
        present = [r for r in rows if r["fact_present"]]
        absent = [r for r in rows if r["fact_present"] is False]
        by_arm.append({"arm": arm, "tier": settings["tier"], "n": len(rows),
                       "verdicts": {v: verdicts[v] for v in VERDICTS}, "aptitude": aptitude["value"],
                       "interval": interval,
                       "given": {"present": {"n": len(present), "correct": sum(r["verdict"] == "correct"
                                                                                 for r in present)},
                                 "absent": {"n": len(absent), "correct": sum(r["verdict"] == "correct"
                                                                               for r in absent)}},
                       "paired": paired, "against_scripted": against,
                       "reply_tokens_by_turn": {str(k): round(sum(v) / len(v), 1)
                                                for k, v in sorted(replies[arm].items())}})
    return out, by_arm
