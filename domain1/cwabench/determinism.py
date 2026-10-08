"""Running a corpus repeatedly under different environments and comparing every answer with a reference
(domain-1-plan.md, 7.2 and 7.10).

An answer's signature is its decision (outcome class and refusal reason), its payload hash and the hash of its
normalized trace. The reference for each snapshot and adapter is its first baseline run on the host. R-23 requires
every other run of the same snapshot to give the same signature.
"""
from __future__ import annotations

import hashlib
import platform
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import adapters as adapters_mod
from .adapters import FAULTS, Adapter, Invocation, Outcome
from .canon import jcs
from .corpora import Snapshot
from .traces import normalize

HOST_PLATFORM = f"{platform.system().lower()}/{'arm64' if platform.machine() in ('arm64', 'aarch64') else platform.machine()}"


@dataclass(frozen=True)
class Signature:
    outcome: str
    refusal_reason: str | None
    payload_hash: str | None
    trace_hash: str | None

    @property
    def decision(self) -> tuple[str, str | None]:
        return self.outcome, self.refusal_reason

    def compare(self, reference: "Signature") -> dict[str, bool | None]:
        """Per stage, whether this answer matches the reference; None where the reference has nothing to match."""
        return {
            "decision": self.decision == reference.decision,
            "payload": None if reference.payload_hash is None else self.payload_hash == reference.payload_hash,
            "trace": None if reference.trace_hash is None else self.trace_hash == reference.trace_hash,
        }


def signature(outcome: Outcome) -> Signature:
    trace_hash = None
    if isinstance(outcome.trace, dict):
        try:
            trace_hash = hashlib.sha256(jcs.serialize_bytes(normalize(outcome.trace))).hexdigest()
        except jcs.CanonicalizationError:
            trace_hash = "uncanonicalizable"
    return Signature(
        outcome.kind,
        outcome.refusal_reason,
        hashlib.sha256(outcome.payload).hexdigest() if outcome.payload is not None else None,
        trace_hash,
    )


@dataclass(frozen=True)
class Cell:
    """One environment an adapter runs in."""

    name: str
    platform: str = HOST_PLATFORM
    env: dict[str, str] = field(default_factory=dict)
    unset: tuple[str, ...] = ()
    clear_env: bool = False  # start from a minimal environment instead of this process's
    cwd: str | None = None  # "empty" for a fresh empty directory
    wrapper: tuple[str, ...] = ()
    concurrency: int | None = None
    description: str = ""


MINIMAL_ENV = {"PATH": "/usr/bin:/bin"}

_THREADS = ("GOMAXPROCS", "RAYON_NUM_THREADS", "UV_THREADPOOL_SIZE", "OMP_NUM_THREADS")
_NO_NETWORK = "(version 1)(allow default)(deny network*)"

HOST_CELLS: dict[str, Cell] = {
    "baseline": Cell("baseline", env={"PYTHONHASHSEED": "random"},
                     description="This process's environment; every repetition is a fresh process"),
    "tz:UTC": Cell("tz:UTC", env={"TZ": "UTC"}, description="Time zone UTC"),
    "tz:Pacific/Kiritimati": Cell("tz:Pacific/Kiritimati", env={"TZ": "Pacific/Kiritimati"},
                                  description="Time zone +14:00"),
    "tz:America/St_Johns": Cell("tz:America/St_Johns", env={"TZ": "America/St_Johns"},
                                description="Time zone −03:30 (−02:30 in summer)"),
    "tz:Asia/Kathmandu": Cell("tz:Asia/Kathmandu", env={"TZ": "Asia/Kathmandu"}, description="Time zone +05:45"),
    "locale:C": Cell("locale:C", env={"LANG": "C", "LC_ALL": "C"}, description="The C locale"),
    "locale:en_US.UTF-8": Cell("locale:en_US.UTF-8", env={"LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8"},
                               description="US English"),
    "locale:tr_TR.UTF-8": Cell("locale:tr_TR.UTF-8", env={"LANG": "tr_TR.UTF-8", "LC_ALL": "tr_TR.UTF-8"},
                               description="Turkish: dotted and dotless I under case mapping"),
    "locale:ja_JP.UTF-8": Cell("locale:ja_JP.UTF-8", env={"LANG": "ja_JP.UTF-8", "LC_ALL": "ja_JP.UTF-8"},
                               description="Japanese"),
    "threads:1": Cell("threads:1", env={k: "1" for k in _THREADS}, description="Runtimes limited to one thread"),
    "threads:16": Cell("threads:16", env={k: "16" for k in _THREADS}, description="Runtimes given sixteen threads"),
    "env:minimal": Cell("env:minimal", clear_env=True,
                        description="Only PATH=/usr/bin:/bin and the adapter's own variables"),
    "home:unset": Cell("home:unset", unset=("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"),
                       description="No HOME or XDG directories"),
    "cwd:empty": Cell("cwd:empty", cwd="empty", description="Started in a fresh empty directory"),
    "burst:32": Cell("burst:32", concurrency=32, description="32 invocations at once"),
    "sandbox:no-network": Cell("sandbox:no-network", wrapper=("/usr/bin/sandbox-exec", "-p", _NO_NETWORK),
                               description="macOS sandbox that denies all network access"),
}


@dataclass
class Answer:
    snapshot: Snapshot
    adapter: str
    cell: str
    platform: str
    repetition: int
    invocation: Invocation
    outcome: Outcome
    signature: Signature
    applied: bool = True
    not_applied_reason: str | None = None
    strace: Path | None = None  # the syscall log, in a traced cell


def run_host(adapters: dict[str, Adapter], corpus: list[Snapshot], cell: Cell, repetitions: int, timeout: float,
             concurrency: int, cwd: Path, progress=None) -> list[Answer]:
    """Run every snapshot through every adapter `repetitions` times in one host cell."""
    jobs = [(s, a, r) for r in range(repetitions) for a in adapters.values() for s in corpus]
    workdir = Path(tempfile.mkdtemp(prefix="cwabench-cwd-")) if cell.cwd == "empty" else cwd
    lock, done = threading.Lock(), [0]

    def work(job) -> Answer:
        snapshot, adapter, repetition = job
        invocation = adapters_mod.invoke(
            adapter, snapshot.data, timeout, workdir, env=cell.env, unset=cell.unset,
            base_env=MINIMAL_ENV if cell.clear_env else None, wrapper=list(cell.wrapper) or None)
        outcome = adapters_mod.classify(invocation)
        if progress:
            with lock:
                done[0] += 1
                if done[0] % 200 == 0 or done[0] == len(jobs):
                    progress(f"{cell.name}: {done[0]}/{len(jobs)}")
        return Answer(snapshot, adapter.name, cell.name, cell.platform, repetition, invocation, outcome,
                      signature(outcome))

    with ThreadPoolExecutor(max_workers=cell.concurrency or concurrency) as pool:
        return list(pool.map(work, jobs))


def references(answers: list[Answer]) -> dict[tuple[str, str], Signature]:
    """(snapshot digest, adapter) → the signature of its first baseline answer."""
    out = {}
    for answer in sorted(answers, key=lambda a: a.repetition):
        if answer.cell == "baseline" and answer.platform == HOST_PLATFORM:
            out.setdefault((answer.snapshot.digest, answer.adapter), answer.signature)
    return out


def is_fault(answer: Answer) -> bool:
    return answer.outcome.kind in FAULTS


# Rows, tallies and findings, shared by S2 and S10 -----------------------------------------------------------------------

STAGES = ("decision", "payload", "trace")


def row(ctx, suite: str, answer: Answer, reference: Signature | None) -> dict:
    """One determinism-row. Large artifacts are stored only when the answer is not the reference's."""
    from . import output

    blobs = ctx.run.blobs
    matches = answer.signature.compare(reference) if reference and answer.applied else None
    differs = bool(matches) and not all(v is not False for v in matches.values())
    keep = differs or is_fault(answer) or reference is None
    outcome = answer.outcome
    return {
        "$schema": output.schema_name("determinism-row"),
        "run_id": ctx.run.run_id,
        "suite": suite,
        "corpus": answer.snapshot.corpus,
        "case_id": answer.snapshot.case_id,
        "case_kind": answer.snapshot.kind,
        "snapshot": blobs.put(answer.snapshot.data, "application/json"),
        "adapter": answer.adapter,
        "env_cell": answer.cell,
        "platform": answer.platform,
        "repetition": answer.repetition,
        "applied": answer.applied,
        "not_applied_reason": answer.not_applied_reason,
        "outcome": outcome.kind,
        "exit_code": answer.invocation.exit_code,
        "refusal_reason": answer.signature.refusal_reason,
        "payload_hash": answer.signature.payload_hash,
        "trace_hash": answer.signature.trace_hash,
        "matches_reference": matches,
        "payload": blobs.put_text(outcome.payload) if keep and outcome.payload is not None else None,
        "trace": blobs.put_json(outcome.trace) if keep and outcome.trace is not None else None,
        "stderr": blobs.put_text(answer.invocation.stderr) if is_fault(answer) and answer.invocation.stderr else None,
        "wall_ms": round(answer.invocation.wall_ms, 3),
        "requirements": list(answer.snapshot.rules),
        "finding": None,
    }


def tally(rows: list[dict]) -> dict:
    """Invariance counts over rows: per stage, matching answers out of comparable ones."""
    out = {stage: [0, 0] for stage in STAGES}
    applied = [r for r in rows if r["applied"] and r["matches_reference"] is not None]
    for r in applied:
        for stage in STAGES:
            value = r["matches_reference"][stage]
            if value is not None:
                out[stage][1] += 1
                out[stage][0] += value
    return {
        "invocations": len(rows),
        "applied": len(applied),
        "not_applied": sum(not r["applied"] for r in rows),
        "faults": sum(r["outcome"] in FAULTS for r in rows),
        **{stage: {"numerator": n, "denominator": d} for stage, (n, d) in out.items()},
    }


def first_stage(row_: dict) -> str | None:
    matches = row_["matches_reference"] or {}
    return next((s for s in STAGES if matches.get(s) is False), None)


def findings(ctx, suite: str, rows: list[dict], what: str) -> list[dict]:
    """One finding per (adapter, cell, first differing stage), with every differing row linked to it."""
    from . import output

    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        stage = first_stage(r)
        if stage is None and r["outcome"] not in FAULTS:
            continue
        key = (r["adapter"], r["env_cell"], stage or "fault")
        groups.setdefault(key, []).append(r)
    out = []
    for (adapter, cell, stage), members in sorted(groups.items()):
        signature_ = {"suite": suite, "adapter": adapter, "cell": cell, "stage": stage}
        finding_id = hashlib.sha256(output.dumps(signature_, compact=True).encode()).hexdigest()[:12]
        for r in members:
            r["finding"] = finding_id
        cases = sorted({r["case_id"] for r in members})
        example = members[0]
        verb = "faults" if stage == "fault" else f"differs from its baseline in the {stage}"
        out.append({
            "$schema": output.schema_name("finding"),
            "finding_id": finding_id,
            "run_id": ctx.run.run_id,
            "suite": suite,
            "adapter": adapter,
            "case_id": example["case_id"],
            "oracle": "determinism",
            "checks": [f"{cell}:{stage}"],
            "severity": "error",
            "summary": (f"{adapter} {verb} in {what} cell {cell} on {len(cases)} snapshot(s), "
                        f"e.g. {', '.join(cases[:3])}")[:1000],
            "first_pointer": None,
            "requirements": ["R-23"],
            "occurrences": len(members),
            "reproducer": {"snapshot": example["snapshot"], "spec_path": None},
        })
    return out
