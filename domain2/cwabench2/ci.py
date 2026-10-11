"""Continuous runs (domain-2-plan.md, P7): Domain 2's CI profiles, replaying the committed model cache.

Domain 1's machinery (cwabench/ci.py) with Domain 2's run. A profile (`[ci.profiles.<name>]` in domain2.toml) names
its suites, the families' size and whether S1 assembles every turn (`frames`), and always replays: a CI run never
calls a model, so a reply the cache lacks fails the run, which is how a changed payload or a changed assembler answer
shows. `cwabench --domain 2 ci <profile>` runs it, writes the run's ci.json comparing it with the profile's previous
run (bumps, suites, metrics, findings, and S7's golden drift classes) and publishes it as results/d2/<profile>.

Where the spec and Domain 1's assemblers come from, as in Domain 1's CI:

- by default, the checkouts the configs name, as they are;
- `--fetch`: Domain 1's mirrors under domain1/.build/ci/checkouts, each assembler at its origin's default branch and
  the spec at the pinned commit;
- `--checkouts DIR`: checkouts already prepared in DIR under the same directory names (what a CI service clones).

With either of the last two, the adapters are built under domain1/.build/ci/build.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from cwabench import ci as d1_ci

from .config import SIZES, SUITES, Config, ConfigError, requirements


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    suites: list[str]
    size: str
    frames: bool


def profile(config: Config, name: str) -> Profile:
    profiles = config.section("ci").get("profiles", {})
    table = profiles.get(name)
    if not isinstance(table, dict):
        raise ConfigError(f"no [ci.profiles.{name}] in {config.path.name} (profiles: {', '.join(profiles) or 'none'})")
    suites = list(table.get("suites", config.suites))
    unknown = [s for s in suites if s not in SUITES]
    if unknown:
        raise ConfigError(f"[ci.profiles.{name}].suites names unknown suites: {', '.join(unknown)}")
    size = table.get("size", config.size)
    if size not in SIZES:
        raise ConfigError(f"[ci.profiles.{name}].size must be one of {', '.join(SIZES)}")
    frames = table.get("frames", False)
    if not isinstance(frames, bool):
        raise ConfigError(f"[ci.profiles.{name}].frames must be true or false")
    requirements(suites, config.adapters, config.lq, config.s6, config.model)
    return Profile(name, str(table.get("description", "")), suites, size, frames)


def apply(config: Config, chosen: Profile) -> Config:
    """The config a profile runs: its suites, size and frames, and the model always replayed."""
    return replace(config, suites=chosen.suites, size=chosen.size, frames=chosen.frames,
                   model={**config.model, "mode": "replay"})


def with_checkouts(config: Config, directory: Path) -> Config:
    """The config with the spec and Domain 1's assemblers read from `directory`, under the same directory names."""
    return replace(config, checkouts=directory, contract_path=directory / config.contract_path.name)


def s7_goldens(run_path: Path) -> dict | None:
    """S7's golden drift classes in a run, in the report's shape: whether goldens were adopted, the count of each
    class that occurred, and how many goldens the run did not make."""
    try:
        summary = json.loads((run_path / "suites" / "S7" / "summary.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    goldens = summary.get("goldens")
    if not goldens:
        return None
    drift = goldens.get("drift") or {}
    return {"adopted": goldens.get("path") is not None,
            "drift": {k: n for k, n in sorted(drift.items()) if k != "removed" and n},
            "removed": int(drift.get("removed") or 0)}


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
    log(f"ci: profile {name}: suites {', '.join(config.suites)}; size {config.size}; frames {config.frames}; "
        f"model replay")
    ci = {"profile": name, "source": source, "mirrors": mirrors}
    run_dir, status = run_suites(config, build=build, log=log, ci=ci)
    d1_ci.publish(config.results_dir, name, run_dir.name)
    try:
        document = json.loads((run_dir / "ci.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        document = {}
    return run_dir, status, document
