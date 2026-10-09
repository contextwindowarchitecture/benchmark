"""Continuous runs (domain-1-plan.md, P7): named profiles, upstream mirrors, the drift report and publishing.

A profile (`[ci.profiles.<name>]` in domain1.toml) is a run configuration: its suites, S2's platform and toolchain
matrix, and S11's summarizer mode. `cwabench ci <profile>` runs it and then compares the run with the previous run of
the same profile:

- **bumps**: what it was made from that changed: the pinned contract, each assembler's commit and toolchain, the
  harness's own source, the config, and the host it ran on (its system, release, machine, CPU count and platform
  string), since S7's timings are only comparable within one machine;
- **suites, metrics and findings** that changed: suite statuses, metric values and statuses, findings new since the
  previous run and findings it had that are gone. Timings (S7's milliseconds and exponents) are listed when they
  move, but move every run, so they alone never make a run `changed`;
- **goldens**: S12's drift classes, so a contract bump that invalidated goldens (spec_change) reads differently from
  an assembler bump that changed answers with no spec change (regression).

The verdict is `baseline` (no previous run), `unchanged`, `changed` (bumps or values moved, nothing worse) or
`regressed` (a new error finding, a suite or metric that got worse, or a golden regression). The report is the run's
`ci.json`; the run is published as `results/d1/<profile>` beside `results/d1/latest`.

Where the assemblers come from:

- by default, the checkouts domain1.toml names, as they are;
- `--fetch`: clones of the same repositories under `.build/ci/checkouts`, each assembler at its origin's default
  branch and the spec at the pinned commit, so a nightly sees upstream bumps without touching the checkouts;
- `--checkouts DIR`: checkouts already prepared in DIR under the same directory names (what a CI service clones).

With either of the last two, builds go to `.build/ci/build`, so they never replace the builds of an ordinary run.
"""
from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from . import gitinfo, output
from .config import SUITES, Config, ConfigError
from .rundir import STATUS_ORDER


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    suites: list[str]
    matrix: list[str]
    summarizer: str | None


def profile(config: Config, name: str) -> Profile:
    table = config.section("ci").get("profiles", {}).get(name)
    if not isinstance(table, dict):
        known = ", ".join(config.section("ci").get("profiles", {})) or "none"
        raise ConfigError(f"no [ci.profiles.{name}] in {config.path.name} (profiles: {known})")
    suites = list(table.get("suites", config.suites))
    unknown = [s for s in suites if s not in SUITES]
    if unknown:
        raise ConfigError(f"[ci.profiles.{name}].suites names unknown suites: {', '.join(unknown)}")
    return Profile(name, str(table.get("description", "")), suites, list(table.get("s2_matrix", [])),
                   table.get("summarizer"))


def apply(config: Config, chosen: Profile) -> Config:
    settings = dict(config.settings)
    settings["s2"] = {**config.section("s2"), "matrix": chosen.matrix}
    if chosen.summarizer:
        settings["summarizer"] = {**config.section("summarizer"), "mode": chosen.summarizer}
    return replace(config, suites=chosen.suites, settings=settings)


# Checkouts ------------------------------------------------------------------------------------------------------------

def _git(path: Path, *args: str, timeout: float = 600) -> str:
    done = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True, timeout=timeout)
    if done.returncode != 0:
        raise ConfigError(f"git {' '.join(args)} in {path} failed: {done.stderr.strip()[-500:]}")
    return done.stdout.strip()


def _sources(config: Config) -> dict[str, tuple[Path, str | None]]:
    """Each repository a run reads, by role: "contract" and each adapter, with the commit to check out (None for the
    origin's default branch)."""
    out = {"contract": (config.contract_path, config.contract_commit)}
    for name, adapter in config.all_adapters.items():
        out[name] = (adapter.checkout, None)
    return out


def with_checkouts(config: Config, directory: Path) -> Config:
    """The config with every repository read from `directory`, under the same directory names, and builds in
    .build/ci/build."""
    adapters = {n: replace(a, checkout=directory / a.checkout.name) for n, a in config.all_adapters.items()}
    return replace(config, contract_path=directory / config.contract_path.name,
                   build_dir=config.root / ".build/ci/build", adapters={n: adapters[n] for n in config.adapters},
                   all_adapters=adapters)


def fetch(config: Config, log: Callable[[str], None]) -> tuple[Path, dict]:
    """Clone or update a mirror of every repository under .build/ci/checkouts and check out what the run needs: the
    pinned contract commit, and each assembler's default branch. Then run the profile's prepare steps (`[ci.prepare]`,
    e.g. installing the TypeScript checkout's dependencies). Returns the directory and what each mirror is at."""
    directory = config.root / ".build" / "ci" / "checkouts"
    directory.mkdir(parents=True, exist_ok=True)
    prepare = config.section("ci").get("prepare", {})
    mirrors = {}
    for role, (source, commit) in _sources(config).items():
        url = gitinfo.inspect(source).remote if source.is_dir() else None
        url = config.section("ci").get("remotes", {}).get(role, url)
        if not url:
            raise ConfigError(f"no origin to mirror for {role}: {source} has no remote and [ci.remotes] names none")
        mirror = directory / source.name
        if not (mirror / ".git").is_dir():
            log(f"ci: cloning {url} → {mirror}")
            subprocess.run(["git", "clone", "--quiet", url, str(mirror)], check=True, timeout=1800)
        _git(mirror, "fetch", "--quiet", "--prune", "origin")
        target = commit or _git(mirror, "rev-parse", "refs/remotes/origin/HEAD")
        _git(mirror, "checkout", "--quiet", "--force", "--detach", target)
        _git(mirror, "clean", "-fdq")  # untracked files go; ignored ones (node_modules) stay
        for step in prepare.get(role, []):
            subprocess.run(step, cwd=mirror, check=True, capture_output=True, timeout=1800)
        mirrors[role] = {"url": url, "path": str(mirror), "ref": commit or _git(mirror, "rev-parse", "--abbrev-ref",
                                                                                   "refs/remotes/origin/HEAD"),
                         "commit": _git(mirror, "rev-parse", "HEAD")}
        log(f"ci: {role} at {mirrors[role]['commit'][:12]} ({mirrors[role]['ref']})")
    return directory, mirrors


# The drift report -----------------------------------------------------------------------------------------------------

def previous_run(results_dir: Path, name: str, before: str) -> Path | None:
    """The newest finished run of the same profile that started before `before`."""
    found = []
    for path in results_dir.glob("*/manifest.json"):
        if path.parent.is_symlink():
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        ci = manifest.get("ci") or {}
        if ci.get("profile") == name and manifest["run_id"] < before and manifest.get("status") != "running":
            found.append(manifest["run_id"])
    return results_dir / max(found) if found else None


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _jsonl(path: Path) -> list[dict]:
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def _bumps(old: dict, new: dict) -> list[dict]:
    out = []

    def changed(what: str, a, b) -> None:
        if a != b:
            out.append({"what": what, "from": a, "to": b})

    changed("contract", old["contract"].get("commit"), new["contract"].get("commit"))
    for name in sorted(set(old["adapters"]) | set(new["adapters"])):
        a, b = old["adapters"].get(name, {}), new["adapters"].get(name, {})
        changed(f"adapter:{name}", a.get("commit"), b.get("commit"))
        changed(f"adapter:{name}:dirty", a.get("dirty"), b.get("dirty"))
        changed(f"toolchain:{name}", a.get("toolchain"), b.get("toolchain"))
        changed(f"available:{name}", a.get("available"), b.get("available"))
    changed("harness", old["harness"].get("source_digest"), new["harness"].get("source_digest"))
    changed("config", old["config"].get("sha256"), new["config"].get("sha256"))
    # The host, field by field (system, release, machine, cpus, platform): S7's timings are only comparable within
    # one machine, so a run made elsewhere must say so. A run that recorded no host reads as None.
    old_host, new_host = old.get("host") or {}, new.get("host") or {}
    for key in sorted(set(old_host) | set(new_host)):
        changed(f"host:{key}", old_host.get(key), new_host.get(key))
    return out


def _goldens(summary: dict | None) -> dict | None:
    if not summary or "goldens" not in summary:
        return None
    counts: dict[str, int] = {}
    for entry in summary["goldens"].get("drift", []):
        counts[entry["drift"]] = counts.get(entry["drift"], 0) + entry["count"]
    return {"adopted": summary["goldens"].get("adopted") is not None, "drift": dict(sorted(counts.items())),
            "removed": len(summary["goldens"].get("removed") or [])}


def _metric_key(m: dict) -> tuple:
    return m["id"], m["adapter"], m["label"]


def compare(run_path: Path, manifest: dict, summary: dict, findings: list[dict], previous: Path | None) -> dict:
    """The drift report's comparison, from the run's own documents and the previous run's directory."""
    goldens_now = _goldens(_load(run_path / "suites" / "S12" / "summary.json"))
    if previous is None:
        return {"previous": None, "verdict": "baseline", "bumps": [], "suites": [], "metrics": [],
                "findings": {"new": [], "resolved": [], "persisting": 0}, "goldens": goldens_now}
    old_manifest = _load(previous / "manifest.json") or {}
    old_summary = _load(previous / "summary.json") or {"suites": [], "metrics": []}
    old_findings = _jsonl(previous / "findings.jsonl")
    bumps = _bumps(old_manifest, manifest) if old_manifest else []

    old_suites = {s["id"]: s["status"] for s in old_summary.get("suites", [])}
    suites = [{"id": s["id"], "status": s["status"], "previous": old_suites.get(s["id"])} for s in summary["suites"]]
    worse_suites = [s for s in suites if s["previous"] and STATUS_ORDER.index(s["status"])
                    > STATUS_ORDER.index(s["previous"])]

    old_metrics = {_metric_key(m): m for m in old_summary.get("metrics", [])}
    metrics = []
    for m in summary["metrics"]:
        before = old_metrics.get(_metric_key(m))
        if before is None or (before["value"], before["status"]) != (m["value"], m["status"]):
            metrics.append({"id": m["id"], "label": m["label"], "suite": m["suite"], "adapter": m["adapter"],
                            "unit": m["unit"], "value": m["value"], "status": m["status"],
                            "previous": None if before is None else before["value"],
                            "previous_status": None if before is None else before["status"]})
    worse_metrics = [m for m in metrics if m["previous_status"] == "pass" and m["status"] == "fail"]

    old_ids = {f["finding_id"] for f in old_findings}
    new_ids = {f["finding_id"] for f in findings}

    def brief(f: dict) -> dict:
        return {"finding_id": f["finding_id"], "suite": f["suite"], "adapter": f["adapter"],
                "severity": f["severity"], "summary": f["summary"][:300]}

    new = [brief(f) for f in findings if f["finding_id"] not in old_ids]
    resolved = [brief(f) for f in old_findings if f["finding_id"] not in new_ids]
    regressions = (goldens_now or {}).get("drift", {}).get("regression", 0)
    if any(f["severity"] == "error" for f in new) or worse_suites or worse_metrics or regressions:
        verdict = "regressed"
    elif bumps or new or resolved or any(m["unit"] in ("rate", "count") or m["status"] != m["previous_status"]
                                         for m in metrics):  # timings move every run; alone they change nothing
        verdict = "changed"
    else:
        verdict = "unchanged"
    return {
        "previous": {"run_id": old_manifest.get("run_id", previous.name), "status": old_manifest.get("status"),
                     "started_at": old_manifest.get("started_at")},
        "verdict": verdict,
        "bumps": bumps,
        "suites": suites,
        "metrics": metrics,
        "findings": {"new": new, "resolved": resolved, "persisting": len(new_ids & old_ids)},
        "goldens": goldens_now,
    }


def report(run, manifest: dict, summary: dict, findings: list[dict], ci: dict) -> dict:
    """Write the run's ci.json: the profile, where the code came from, and the comparison with the previous run."""
    previous = previous_run(run.config.results_dir, ci["profile"], run.run_id)
    document = {
        "$schema": output.schema_name("ci-report"),
        "run_id": run.run_id,
        "profile": ci["profile"],
        "status": summary["status"],
        "source": ci["source"],
        "mirrors": ci.get("mirrors"),
        **compare(run.path, manifest, summary, findings, previous),
    }
    run.write_json("ci.json", document, f"CI ({ci['profile']}): what changed since the previous {ci['profile']} run")
    return document


def publish(results_dir: Path, name: str, run_id: str) -> None:
    """Point results/d1/<profile> at the run, as results/d1/latest points at the newest run of any kind."""
    import os

    link = results_dir / name
    if link.exists() and not link.is_symlink():
        raise ConfigError(f"{link} exists and is not a link; a CI profile cannot be named after it")
    temporary = results_dir / f".{name}.tmp"
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(run_id, target_is_directory=True)
    os.replace(temporary, link)


def run(config: Config, name: str, *, fetch_upstream: bool = False, checkouts: Path | None = None, build: bool = True,
        log: Callable[[str], None]) -> tuple[Path, str, dict]:
    from .runner import run as run_suites

    chosen = profile(config, name)
    mirrors, source = None, "checkouts"
    if fetch_upstream:
        directory, mirrors = fetch(config, log)
        config, source = with_checkouts(config, directory), "mirrors"
    elif checkouts is not None:
        config, source = with_checkouts(config, checkouts.resolve()), "provided"
    config = apply(config, chosen)
    log(f"ci: profile {name}: suites {', '.join(config.suites)}; S2 matrix {', '.join(chosen.matrix) or 'none'}")
    ci = {"profile": name, "source": source, "mirrors": mirrors}
    run_dir, status = run_suites(config, build=build, log=log, ci=ci)
    publish(config.results_dir, name, run_dir.name)
    document = _load(run_dir / "ci.json") or {}
    return run_dir, status, document
