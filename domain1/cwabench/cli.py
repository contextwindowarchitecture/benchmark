"""cwabench: set up the adapters, run suites, and check run directories.

The command serves every domain. Plain `cwabench` is Domain 1; `cwabench --domain <n> …` runs another domain's
command, which its package registers as an entry point in the `cwabench.domains` group. This module names no domain
but its own, so a new domain is added without changing it.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path

from . import config as config_mod
from .contract import ContractError
from .runner import run, setup_adapters
from .validate import validate_run

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "domain1.toml"
DOMAIN_GROUP = "cwabench.domains"


def domains() -> dict[str, EntryPoint]:
    """Every installed domain's command, by domain name."""
    return {ep.name: ep for ep in entry_points(group=DOMAIN_GROUP)}


def load_domains() -> None:
    """Import every installed domain, which registers its output schemas, so any domain's run directory validates."""
    for ep in domains().values():
        ep.load()


def _load(args) -> config_mod.Config:
    config = config_mod.load(args.config)
    if getattr(args, "adapters", None):
        names = [n.strip() for n in args.adapters.split(",") if n.strip()]
        missing = [n for n in names if n not in config.adapters]
        if missing:
            raise config_mod.ConfigError(f"unknown adapters: {', '.join(missing)}")
        config = replace(config, adapters={n: config.adapters[n] for n in names})
    if getattr(args, "suites", None):
        suites = [s.strip() for s in args.suites.split(",") if s.strip()]
        unknown = [s for s in suites if s not in config_mod.SUITES]
        if unknown:
            raise config_mod.ConfigError(f"suites not implemented yet: {', '.join(unknown)}")
        config = replace(config, suites=suites)
    if getattr(args, "summarizer", None):
        summarizer = {**config.section("summarizer"), "mode": args.summarizer}
        config = replace(config, settings={**config.settings, "summarizer": summarizer})
    return config


def _print_summary(run_dir: Path) -> None:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    print(f"\nrun {summary['run_id']}: {summary['status']}")
    for suite in summary["suites"]:
        print(f"  {suite['id']} {suite['title']}: {suite['status']}")
    for adapter in summary["adapters"]:
        if not adapter["available"]:
            print(f"  {adapter['adapter']}: unavailable ({adapter['error'].splitlines()[0]})")
    print()
    width = max((len(m["label"]) for m in summary["metrics"]), default=0)
    for metric in summary["metrics"]:
        if metric["unit"] == "rate":
            value = "n/a" if metric["value"] is None else f"{metric['value']:.1%} ({metric['numerator']}/{metric['denominator']})"
        else:
            value = str(metric["value"])
        who = metric["adapter"] or "all"
        print(f"  {metric['status']:<4}  {metric['label']:<{width}}  {who:<10}  {value}")
    print(f"\nfindings: {summary['findings']['total']}  →  {run_dir}")


def _print_drift(report: dict) -> None:
    if not report:
        return
    previous = report["previous"]["run_id"] if report["previous"] else "none"
    print(f"\nci {report['profile']}: {report['verdict']} (previous {report['profile']} run: {previous})")
    for bump in report["bumps"]:
        print(f"  bump  {bump['what']}: {bump['from']} → {bump['to']}")
    for suite in report["suites"]:
        if suite["previous"] and suite["previous"] != suite["status"]:
            print(f"  suite {suite['id']}: {suite['previous']} → {suite['status']}")
    for metric in report["metrics"]:
        if metric["previous_status"] != metric["status"]:
            print(f"  metric {metric['label']} ({metric['adapter'] or 'all'}): {metric['previous_status']} → "
                  f"{metric['status']}")
    found = report["findings"]
    print(f"  findings: {len(found['new'])} new, {len(found['resolved'])} resolved, {found['persisting']} persisting")
    if report["goldens"]:
        print(f"  goldens: {report['goldens']['drift'] or 'none compared'}")


def main(argv: list[str] | None = None) -> int:
    """Run Domain 1, or the domain `--domain` names."""
    argv = list(sys.argv[1:] if argv is None else argv)
    chooser = argparse.ArgumentParser(add_help=False)
    chooser.add_argument("--domain", default="1")
    chosen, rest = chooser.parse_known_args(argv)
    if chosen.domain == "1":
        return run_domain1(rest)
    found = domains()
    if chosen.domain not in found:
        installed = ", ".join(sorted(found)) or "none"
        print(f"cwabench: no domain {chosen.domain!r} is installed (installed: {installed})", file=sys.stderr)
        return 2
    return found[chosen.domain].load()(rest)


def run_domain1(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cwabench", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--domain", default="1",
                        help="the domain to run: 1 (this harness, the default) or another installed domain")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="run configuration (default: domain1.toml)")
    commands = parser.add_subparsers(dest="command", required=True)

    setup_cmd = commands.add_parser("setup", help="build and check the adapters, without running anything")
    setup_cmd.add_argument("--adapters", help="comma-separated subset of the configured adapters")
    setup_cmd.add_argument("--no-build", action="store_true", help="skip build steps; only check")

    run_cmd = commands.add_parser("run", help="run suites and write a run directory")
    run_cmd.add_argument("--adapters", help="comma-separated subset of the configured adapters")
    run_cmd.add_argument("--suites", help="comma-separated subset of the configured suites")
    run_cmd.add_argument("--no-build", action="store_true", help="skip build steps; use what is already built")
    run_cmd.add_argument("--summarizer", choices=("off", "stub", "llm", "replay"),
                         help="S11's variant mode, overriding [summarizer].mode")

    ci_cmd = commands.add_parser("ci", help="run a CI profile ([ci.profiles.*]), report drift, and publish it as "
                                           "results/d1/<profile>")
    ci_cmd.add_argument("profile", help="a profile in [ci.profiles], e.g. nightly or weekly")
    ci_cmd.add_argument("--adapters", help="comma-separated subset of the configured adapters")
    ci_cmd.add_argument("--no-build", action="store_true", help="skip build steps; use what is already built")
    where = ci_cmd.add_mutually_exclusive_group()
    where.add_argument("--fetch", action="store_true",
                       help="test upstream: mirror every repository under .build/ci/checkouts, assemblers at their "
                            "default branch and the spec at the pinned commit")
    where.add_argument("--checkouts", help="read every repository from this directory, under the same names")

    validate_cmd = commands.add_parser("validate", help="check a run directory against its schemas and blob digests")
    validate_cmd.add_argument("run_dir", nargs="?", help="default: the latest run")

    goldens_cmd = commands.add_parser("goldens", help="adopt a run's consensus goldens (S12)")
    goldens_sub = goldens_cmd.add_subparsers(dest="goldens_command", required=True)
    accept_cmd = goldens_sub.add_parser("accept", help="adopt a run's candidate goldens as the baseline")
    accept_cmd.add_argument("run_dir", nargs="?", help="default: the latest run")

    fixture_cmd = commands.add_parser("fixture", help="write a run trimmed to a sample of its rows, frames and blobs, "
                                                      "for consumers' tests")
    fixture_cmd.add_argument("run_dir", nargs="?",
                             help="a run directory, or a run id under the results (default: the latest run)")
    fixture_cmd.add_argument("--out", help="the fixtures directory (default: fixtures/runs beside the config)")
    fixture_cmd.add_argument("--rows", type=int, default=12, help="rows kept per JSONL file; findings keep every row")
    fixture_cmd.add_argument("--frames", type=int, default=16, help="frames kept per adapter in each kept sweep")
    fixture_cmd.add_argument("--sweeps", type=int, default=2, help="sweep cells kept, with their timelines")

    args = parser.parse_args(argv)
    try:
        config = _load(args)
        if args.command == "setup":
            _, unavailable = setup_adapters(config, build=not args.no_build)
            return 1 if unavailable else 0
        if args.command == "run":
            run_dir, status = run(config, build=not args.no_build)
            _print_summary(run_dir)
            problems = validate_run(run_dir)
            for problem in problems:
                print(f"invalid output: {problem}", file=sys.stderr)
            return 0 if status == "pass" and not problems else 1
        if args.command == "ci":
            from . import ci

            run_dir, status, report = ci.run(config, args.profile, fetch_upstream=args.fetch,
                                             checkouts=Path(args.checkouts) if args.checkouts else None,
                                             build=not args.no_build,
                                             log=lambda m: print(m, file=sys.stderr, flush=True))
            _print_summary(run_dir)
            _print_drift(report)
            problems = validate_run(run_dir)
            for problem in problems:
                print(f"invalid output: {problem}", file=sys.stderr)
            return 0 if status == "pass" and not problems else 1
        if args.command == "goldens":
            from .suites.s12_goldens import accept

            run_dir = Path(args.run_dir) if args.run_dir else config.results_dir / "latest"
            target, count = accept(config, run_dir.resolve())
            print(f"adopted {count} golden(s) from {run_dir.resolve().name} → {target}")
            return 0
        if args.command == "fixture":
            from .fixture import write_fixture
            from .rundir import load_upstream
            from .validate import CONTRACT_UNAVAILABLE

            given = args.run_dir
            run_dir = Path(given) if given and Path(given).is_dir() else config.results_dir / (given or "latest")
            out = Path(args.out).resolve() if args.out else config.root / "fixtures" / "runs"
            target, problems = write_fixture(run_dir.resolve(), out, rows=args.rows, frames=args.frames,
                                             sweeps=args.sweeps, upstream=load_upstream(config.upstream_path),
                                             log=lambda m: print(m, file=sys.stderr, flush=True))
            # A run made at another contract commit than the checkout's cannot have its spec-format drafts checked
            # here; the fixture is still whole, so that is a warning, not a problem with it.
            errors = [p for p in problems if not p.startswith(CONTRACT_UNAVAILABLE)]
            for problem in problems:
                print(f"{'invalid fixture' if problem in errors else 'warning'}: {problem}", file=sys.stderr)
            print(f"{target}: {'valid' if not errors else f'{len(errors)} problem(s)'}")
            return 1 if errors else 0
        if args.command == "validate":
            run_dir = Path(args.run_dir) if args.run_dir else config.results_dir / "latest"
            load_domains()  # a run directory of any installed domain validates here
            problems = validate_run(run_dir.resolve())
            for problem in problems:
                print(problem)
            print(f"{run_dir}: {'valid' if not problems else f'{len(problems)} problem(s)'}")
            return 1 if problems else 0
    except (config_mod.ConfigError, ContractError) as error:
        print(f"cwabench: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
