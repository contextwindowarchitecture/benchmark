"""The assembly gate for payloads a suite makes itself (domain-5-plan.md, 3, 8): S4's searched attacks and S5's pasted
material, which S1 cannot build ahead of the run.

Each CWA snapshot is assembled by every adapter and judged as S1 judges its own (Domain 2's `assemble`): the
prediction, Domain 1's trace audit, agreement between adapters; then the structure oracle reads the payload back, and
the attack-in-payload oracle reads it by record and by text, which must agree. A payload passes when all of that
holds, and only a passed payload is sent. Failures become findings, one per signature (suite, oracle, check, adapter,
arm), with their occurrences.
"""
from __future__ import annotations

from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field

from cwabench2.suites import s1_gate as d2_gate

from ..oracles import presence, structure
from . import SuiteContext, finding


@dataclass
class Entry:
    case_id: str
    arm: str
    frozen: object  # application.snapshot.Frozen
    attack_text: str | None = None
    item_id: str | None = None  # the probe item the attack was written into


@dataclass
class Verdict:
    ok: bool
    findings: list[str]  # the finding ids this payload raised
    answers: list[dict]
    agree: bool
    structure: dict  # {"ok", "problems"}, as a gate row records it
    attack: dict | None


@dataclass
class Gate:
    ctx: SuiteContext
    suite: str
    findings: dict = field(default_factory=dict)
    tally: dict = field(default_factory=lambda: defaultdict(lambda: [0, 0]))

    def __post_init__(self):
        config = self.ctx.config
        self.pool = ProcessPoolExecutor(max_workers=config.concurrency, initializer=d2_gate._init,
                                        initargs=(self.ctx.adapters, (self.ctx.contract.path, config.contract_commit,
                                                                      config.allow_dirty)))

    def close(self) -> None:
        self.pool.shutdown()

    def _add(self, oracle: str, checks: list[str], adapter: str | None, arm: str, case_id: str, summary: str,
             data: bytes) -> str:
        signature = {"suite": self.suite, "oracle": oracle, "checks": checks, "adapter": adapter, "arm": arm}
        row = finding(self.ctx, self.suite, signature, case_id=case_id, oracle=oracle, checks=checks,
                      summary=summary[:500])
        known = self.findings.get(row["finding_id"])
        if known is not None:
            known["occurrences"] += 1
        else:
            row["adapter"] = adapter
            row["reproducer"] = {"snapshot": self.ctx.run.blobs.put(data, "application/json"), "spec_path": None}
            self.findings[row["finding_id"]] = row
        return row["finding_id"]

    def check(self, entries: list[Entry]) -> list[Verdict]:
        config = self.ctx.config
        names = list(self.ctx.adapters)
        jobs = []
        for entry in entries:
            counts = defaultdict(int)
            for batch in entry.frozen.snapshot["batches"]:
                for item in batch["items"]:
                    counts[item["slot"]] += 1
            jobs.append({"data": entry.frozen.at(), "adapters": names, "timeout": config.timeout_s,
                         "cwd": str(config.root), "payload_source": config.adapters.payload_source, "fact": None,
                         "keep": False, "candidates": dict(counts),
                         "expected": {"outcome": "assembled", "refusal_reason": None,
                                      "payload_hash": entry.frozen.payload_hash,
                                      "input_tokens": entry.frozen.input_tokens, "included": entry.frozen.included}})
        verdicts = []
        for entry, job, result in zip(entries, jobs, self.pool.map(d2_gate.assemble, jobs)):
            data, frozen, problems = job["data"], entry.frozen, []
            for answer in result["answers"]:
                adapter = answer["adapter"]
                if answer["outcome"] in d2_gate.FAULTS:
                    problems.append(self._add("fault", [answer["outcome"]], adapter, entry.arm, entry.case_id,
                                              f"{adapter} {answer['outcome']} on {entry.case_id}", data))
                    continue
                self.tally["audit"][0] += not answer["audit_failed"]
                self.tally["audit"][1] += 1
                self.tally["prediction"][0] += answer["matches_prediction"]
                self.tally["prediction"][1] += 1
                if answer["audit_failed"]:
                    problems.append(self._add("auditor", sorted(answer["audit_failed"]), adapter, entry.arm,
                                              entry.case_id, f"{adapter} on {entry.case_id} breaks "
                                              f"{', '.join(answer['audit_failed'])}", data))
                if not answer["matches_prediction"]:
                    problems.append(self._add("expected", ["prediction"], adapter, entry.arm, entry.case_id,
                                              f"{adapter} on {entry.case_id}: {answer['outcome']}, "
                                              f"{answer['input_tokens']} tokens", data))
            self.tally["agreement"][0] += result["agree"]
            self.tally["agreement"][1] += 1
            if not result["agree"]:
                problems.append(self._add("differential", ["agreement"], None, entry.arm, entry.case_id,
                                          f"adapters disagree on {entry.case_id}", data))
            read = structure.cwa(frozen.payload, frozen.snapshot, frozen.items)
            self.tally["structure"][0] += read.ok
            self.tally["structure"][1] += 1
            if not read.ok:
                problems.append(self._add("expected", ["structure"], None, entry.arm, entry.case_id,
                                          f"{entry.case_id}: {'; '.join(read.problems)}", data))
            attack = None
            if entry.attack_text is not None:
                content_id = frozen.content.get(entry.item_id)
                by_record = presence.by_record_cwa(set(frozen.included), content_id, read.bodies, entry.attack_text)
                by_text = presence.by_text(entry.arm, frozen.payload, entry.attack_text)
                self.tally["presence"][0] += by_record == by_text
                self.tally["presence"][1] += 1
                if by_record != by_text:
                    problems.append(self._add("self-check", ["presence"], None, entry.arm, entry.case_id,
                                              f"{entry.case_id}: present by record {by_record}, by text {by_text}",
                                              data))
                attack = {"item": content_id, "by_record": by_record, "by_text": by_text}
            verdicts.append(Verdict(not problems, sorted(set(problems)),
                                    [{k: v for k, v in a.items() if not k.startswith("_")} for a in result["answers"]],
                                    result["agree"], {"ok": read.ok, "problems": read.problems[:5]}, attack))
        return verdicts

