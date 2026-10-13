"""The run configuration, domain5.toml (domain-5-plan.md, 12). This build reads the tables P0 needs: the contract pin,
the run, the task families and the attacks; the others (models, arms, axes, external sets, thresholds, CI profiles)
arrive with the phases that use them."""
from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .attacks import FAMILIES as ATTACK_FAMILIES
from .attacks.carriers import MARKERS
from .tasks import FAMILIES as TASK_FAMILIES

SUITES = ("S0",)
SIZES = ("pilot", "recorded")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class TaskConfig:
    name: str
    seed: int
    sizes: dict[str, int]  # instances of each variant, by size
    parameters: dict  # the generator's parameters (tasks/<family>.py)


@dataclass(frozen=True)
class AttackConfig:
    seed: int  # every probe's own seed is drawn from it (attacks/compose.py)
    families: list[str]
    chat_template: str  # the template the marker carrier forges


@dataclass(frozen=True)
class Config:
    path: Path
    sha256: str
    root: Path
    contract_path: Path
    contract_commit: str
    allow_dirty: bool
    suites: list[str]
    size: str
    tasks: dict[str, TaskConfig]  # the families this run generates, in order
    attacks: AttackConfig
    results_dir: Path
    upstream_path: Path | None = None
    timeout_s: float = 60.0  # per assembly, from P1
    concurrency: int = 1  # snapshots in flight, from P1
    settings: dict[str, dict] = field(default_factory=dict)


def _table(data: dict, name: str) -> dict:
    table = data.get(name) or {}
    if not isinstance(table, dict):
        raise ConfigError(f"[{name}] must be a table")
    return table


def load(path: str | Path) -> Config:
    path = Path(path).resolve()
    raw = path.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"{path}: {error}") from None
    root = path.parent

    contract, run = _table(data, "contract"), _table(data, "run")
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

    families: dict[str, TaskConfig] = {}
    for name, table in _table(data, "tasks").items():
        where = f"[tasks.{name}]"
        if name not in TASK_FAMILIES:
            raise ConfigError(f"{where}: no such family (available: {', '.join(TASK_FAMILIES)})")
        table = dict(table)
        seed, sizes = table.pop("seed", None), table.pop("sizes", None)
        if not isinstance(seed, int):
            raise ConfigError(f"{where}.seed must be an integer")
        if not isinstance(sizes, dict) or set(sizes) != set(SIZES) or not all(
                isinstance(v, int) and v >= 0 for v in sizes.values()):
            raise ConfigError(f"{where}.sizes must give {' and '.join(SIZES)} as counts")
        families[name] = TaskConfig(name, seed, dict(sizes), table)
    selected = run.get("tasks", list(families))
    missing = [f for f in selected if f not in families]
    if missing:
        raise ConfigError(f"[run].tasks names families with no [tasks.*] table: {', '.join(missing)}")

    table = _table(data, "attacks")
    seed = table.get("seed")
    if not isinstance(seed, int):
        raise ConfigError("[attacks].seed must be an integer")
    chosen = table.get("families", list(ATTACK_FAMILIES))
    stray = [f for f in chosen if f not in ATTACK_FAMILIES]
    if stray:
        raise ConfigError(f"[attacks].families names families this build does not have: {', '.join(stray)}")
    template = table.get("chat_template", "qwen")
    if template not in MARKERS:
        raise ConfigError(f"[attacks].chat_template must be one of {', '.join(MARKERS)}")
    unknown_keys = sorted(set(table) - {"seed", "families", "chat_template"})
    if unknown_keys:
        raise ConfigError(f"[attacks] has unknown keys: {', '.join(unknown_keys)}")

    findings = _table(data, "findings")
    upstream_path = (root / str(findings.get("upstream", "findings/upstream.json"))).resolve()
    if not upstream_path.is_file():
        if "upstream" in findings:
            raise ConfigError(f"[findings].upstream names {upstream_path}, which does not exist")
        upstream_path = None

    return Config(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        root=root,
        contract_path=(root / contract["path"]).resolve(),
        contract_commit=str(contract["commit"]),
        allow_dirty=bool(contract.get("allow_dirty", False)),
        suites=suites,
        size=size,
        tasks={name: families[name] for name in selected},
        attacks=AttackConfig(seed, list(chosen), template),
        results_dir=(root / run.get("results_dir", "results/d5")).resolve(),
        upstream_path=upstream_path,
        timeout_s=float(run.get("timeout_s", 60)),
        concurrency=max(1, int(run.get("concurrency", 1))),
        settings={"ci": _table(data, "ci")},
    )
