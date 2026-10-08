"""Corpora: named collections of snapshots the suites run (domain-1-plan.md, 4.1).

The conformance corpus, the labeled generators (P3), the fuzzer's fixed seed corpus (P4) and S11's frozen producer
output without a model (P6) are registered here, and every suite that takes `corpus = [...]` in domain1.toml picks
them up. S5 builds its steered corpora as it runs.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from ..contract import Contract


@dataclass(frozen=True)
class Snapshot:
    corpus: str
    case_id: str
    kind: str  # "case" or "rejection"
    rules: tuple[str, ...]
    data: bytes
    label: dict | None = field(default=None, compare=False)  # labeled corpora: every decision the trace must record

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.data).hexdigest()

    def label_reasons(self) -> list[tuple[str, str | None]]:
        """(reason, slot) pairs the label expects, for coverage: every exclusion it states, and its refusal."""
        if not self.label:
            return []
        out = []
        for fates in self.label["fates"].values():
            for fate in fates if isinstance(fates, list) else [fates]:
                if fate["fate"] == "excluded":
                    out.append((fate["reason"], fate.get("slot")))
        if self.label["refusal"]:
            out.append((self.label["refusal"], None))
        return out


def conformance(contract: Contract) -> list[Snapshot]:
    out = [Snapshot("conformance.cases", c.id, "case", tuple(c.rules), c.snapshot_bytes) for c in contract.cases]
    out += [Snapshot("conformance.rejections", c.id, "rejection", tuple(c.rules), c.snapshot_bytes)
            for c in contract.rejections]
    return out


def _labeled(name: str):
    def build(contract: Contract) -> list[Snapshot]:
        from importlib import import_module

        return import_module(f".labeled.{name}", __package__).build(contract)

    return build


def _fuzz_seeds(contract: Contract) -> list[Snapshot]:
    from .fuzz import seeds

    return seeds(contract)


def _producer(arm: str):
    def build(contract: Contract) -> list[Snapshot]:
        from ..suites.s11_summarizer import corpus

        return corpus(contract, arm)

    return build


LABELED = ("admission", "boundaries", "precedence", "pipeline", "conflicts", "refusal", "degradation", "fitting")
BUILDERS = {"conformance": conformance, **{f"labeled.{name}": _labeled(name) for name in LABELED},
            "fuzz.seeds": _fuzz_seeds, "producer.off": _producer("off"), "producer.stub": _producer("stub")}


def load(contract: Contract, names: list[str]) -> list[Snapshot]:
    unknown = [n for n in names if n not in BUILDERS]
    if unknown:
        raise ValueError(f"unknown corpora: {', '.join(unknown)} (available: {', '.join(BUILDERS)})")
    seen, out = set(), []
    for name in names:
        for snapshot in BUILDERS[name](contract):
            if snapshot.digest not in seen:  # identical bytes are one snapshot, whichever corpus lists it
                seen.add(snapshot.digest)
                out.append(snapshot)
    return out
