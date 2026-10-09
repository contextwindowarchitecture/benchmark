"""The run configuration, domain2.toml (domain-2-plan.md, 12). This build reads the tables P0 needs; the others
(adapters, model, arms, budgets, repeats, thresholds, caps, CI profiles) arrive with the phases that use them."""
from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .conversations import FAMILIES

SUITES = ("S0",)  # the suites this build implements; S1 to S7 arrive in later phases (domain-2-plan.md, 14)
SIZES = ("pilot", "recorded")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class FamilyConfig:
    name: str
    seed: int
    sizes: dict[str, int]  # conversations per turn count, by size
    parameters: dict  # the generator's parameters (conversations/<family>.py)


@dataclass(frozen=True)
class Config:
    path: Path
    sha256: str
    root: Path
    contract_path: Path
    contract_commit: str
    allow_dirty: bool
    suites: list[str]
    size: str  # which of each family's sizes a run generates
    turn_counts: list[int]
    checkpoint_every: int
    families: dict[str, FamilyConfig]  # the ones this run generates, in order
    results_dir: Path
    upstream_path: Path | None = None
    timeout_s: float = 60.0
    concurrency: int = 1
    settings: dict[str, dict] = field(default_factory=dict)

    def section(self, name: str) -> dict:
        return self.settings.get(name, {})


def _table(data: dict, name: str) -> dict:
    table = data.get(name) or {}
    if not isinstance(table, dict):
        raise ConfigError(f"[{name}] must be a table")
    return table


def _positive_ints(value, where: str) -> list[int]:
    if not isinstance(value, list) or not value or not all(isinstance(v, int) and v > 0 for v in value):
        raise ConfigError(f"{where} must be a non-empty list of positive integers")
    return value


def load(path: str | Path) -> Config:
    path = Path(path).resolve()
    raw = path.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"{path}: {error}") from None
    root = path.parent

    contract, run, turns = _table(data, "contract"), _table(data, "run"), _table(data, "turns")
    if "path" not in contract or "commit" not in contract:
        raise ConfigError("[contract] needs path and commit")

    suites = run.get("suites", list(SUITES))
    if not isinstance(suites, list) or not all(isinstance(s, str) for s in suites):
        raise ConfigError("[run].suites must be a list of strings")
    unknown = [s for s in suites if s not in SUITES]
    if unknown:
        raise ConfigError(f"suites not implemented yet: {', '.join(unknown)} (available: {', '.join(SUITES)})")
    size = run.get("size", "pilot")
    if size not in SIZES:
        raise ConfigError(f"[run].size must be one of {', '.join(SIZES)}")

    turn_counts = _positive_ints(turns.get("counts", [10, 50, 100]), "[turns].counts")
    every = turns.get("checkpoint_every", 10)
    if not isinstance(every, int) or every < 1:
        raise ConfigError("[turns].checkpoint_every must be a positive integer")

    families: dict[str, FamilyConfig] = {}
    for name, table in _table(data, "families").items():
        where = f"[families.{name}]"
        if name not in FAMILIES:
            raise ConfigError(f"{where}: no such family (available: {', '.join(FAMILIES)})")
        table = dict(table)
        seed, sizes = table.pop("seed", None), table.pop("sizes", None)
        if not isinstance(seed, int):
            raise ConfigError(f"{where}.seed must be an integer")
        if not isinstance(sizes, dict) or set(sizes) != set(SIZES) or not all(
                isinstance(v, int) and v >= 0 for v in sizes.values()):
            raise ConfigError(f"{where}.sizes must give {' and '.join(SIZES)} as counts")
        families[name] = FamilyConfig(name, seed, dict(sizes), table)
    selected = run.get("families", list(families))
    missing = [f for f in selected if f not in families]
    if missing:
        raise ConfigError(f"[run].families names families with no [families.*] table: {', '.join(missing)}")

    findings = _table(data, "findings")
    upstream_path = (root / str(findings.get("upstream", "findings/upstream.json"))).resolve()
    if not upstream_path.is_file():
        if "upstream" in findings:
            raise ConfigError(f"[findings].upstream names {upstream_path}, which does not exist")
        upstream_path = None  # the default file is optional: no file, no links

    return Config(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        root=root,
        contract_path=(root / contract["path"]).resolve(),
        contract_commit=str(contract["commit"]),
        allow_dirty=bool(contract.get("allow_dirty", False)),
        suites=suites,
        size=size,
        turn_counts=turn_counts,
        checkpoint_every=every,
        families={name: families[name] for name in selected},
        results_dir=(root / run.get("results_dir", "results/d2")).resolve(),
        upstream_path=upstream_path,
        timeout_s=float(run.get("timeout_s", 60)),
        concurrency=max(1, int(run.get("concurrency", 1))),
        settings={},
    )
