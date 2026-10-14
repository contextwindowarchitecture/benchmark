"""Continuous runs (domain-5-plan.md, 12, P7): Domain 5's CI profiles, replaying the committed model cache.

Domain 1's machinery (cwabench/ci.py) with Domain 5's run, as Domain 2's CI has it. A profile (`[ci.profiles.<name>]`
in domain5.toml) names its suites and size and always replays: a CI run never calls a model, so a reply the cache
lacks fails the run, which is how a changed payload or a changed assembler answer shows. `cwabench --domain 5 ci
<profile>` runs it, writes the run's ci.json comparing it with the profile's previous run (bumps, suites, metrics,
findings, and S7's golden drift classes) and publishes it as results/d5/<profile>.

Where the spec and Domain 1's assemblers come from, as in Domains 1 and 2's CI: the checkouts the configs name, by
default; `--fetch`, Domain 1's mirrors (each assembler at its default branch, the spec at the pinned commit);
`--checkouts DIR`, checkouts prepared in DIR under the same directory names. With either of the last two, the adapters
are built under domain1/.build/ci/build.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from cwabench import ci as d1_ci
from cwabench2.ci import s7_goldens

from .config import SIZES, SUITES, Config, ConfigError

__all__ = ["Profile", "apply", "profile", "run", "s7_goldens", "with_checkouts"]


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    suites: list[str]
    size: str


def profile(config: Config, name: str) -> Profile:
    profiles = config.settings.get("ci", {}).get("profiles", {})
    table = profiles.get(name)
    if not isinstance(table, dict):
        raise ConfigError(f"no [ci.profiles.{name}] in {config.path.name} (profiles: {', '.join(profiles) or 'none'})")
    suites = list(table.get("suites", config.suites))
    unknown = [s for s in suites if s not in SUITES]
    if unknown:
        raise ConfigError(f"[ci.profiles.{name}].suites names unknown suites: {', '.join(unknown)}")
    needs = {"S2": "S1", "S3": "S1", "S7": "S1"}
    missing = sorted({f"{s} needs {n}" for s, n in needs.items() if s in suites and n not in suites})
    if missing:
        raise ConfigError(f"[ci.profiles.{name}].suites: {'; '.join(missing)}")
    size = table.get("size", config.size)
    if size not in SIZES:
        raise ConfigError(f"[ci.profiles.{name}].size must be one of {', '.join(SIZES)}")
    return Profile(name, str(table.get("description", "")), suites, size)


def apply(config: Config, chosen: Profile) -> Config:
    """The config a profile runs: its suites and size, the model always replayed, and S1's tokenizer check read from
    its committed cache, never a server."""
    tokenize = "replay" if config.tokenize == "server" else config.tokenize
    return replace(config, suites=chosen.suites, size=chosen.size, tokenize=tokenize,
                   model={**config.model, "mode": "replay"})


def with_checkouts(config: Config, directory: Path) -> Config:
    """The config with the spec and Domain 1's assemblers read from `directory`, under the same directory names."""
    return replace(config, checkouts=directory, contract_path=directory / config.contract_path.name)


def run(config: Config, name: str, *, fetch_upstream: bool = False, checkouts: Path | None = None, build: bool = True,
        log: Callable[[str], None]) -> tuple[Path, str, dict]:
    from .runner import adapters_config
    from .runner import run as run_suites

    chosen = profile(config, name)
    mirrors, source = None, "checkouts"
    if fetch_upstream:
        directory, mirrors = d1_ci.fetch(adapters_config(config), log)
        config, source = with_checkouts(config, directory), "mirrors"
    elif checkouts is not None:
        config, source = with_checkouts(config, checkouts.resolve()), "provided"
    config = apply(config, chosen)
    log(f"ci: profile {name}: suites {', '.join(config.suites)}; size {config.size}; model replay")
    ci = {"profile": name, "source": source, "mirrors": mirrors}
    run_dir, status = run_suites(config, build=build, log=log, ci=ci)
    d1_ci.publish(config.results_dir, name, run_dir.name)
    try:
        document = json.loads((run_dir / "ci.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        document = {}
    return run_dir, status, document
