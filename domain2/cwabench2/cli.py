"""cwabench --domain 2: run Domain 2's suites and check run directories.

Domain 1's `cwabench` command calls `main` through this package's `cwabench.domains` entry point."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from cwabench.config import ConfigError as D1ConfigError
from cwabench.contract import ContractError
from cwabench.validate import validate_run

from . import config as config_mod
from . import output  # registers Domain 2's prefix, so validate_run reads its documents
from .runner import ProducerError, run

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "domain2.toml"


def _load(args) -> config_mod.Config:
    config = config_mod.load(args.config)
    if getattr(args, "suites", None):
        suites = [s.strip() for s in args.suites.split(",") if s.strip()]
        unknown = [s for s in suites if s not in config_mod.SUITES]
        if unknown:
            raise config_mod.ConfigError(f"suites not implemented yet: {', '.join(unknown)}")
        config_mod.requirements(suites, config.adapters, config.lq, config.s6, config.model)
        config = replace(config, suites=suites)
    if getattr(args, "size", None):
        config = replace(config, size=args.size)
    if getattr(args, "no_frames", False):
        config = replace(config, frames=False)
    if getattr(args, "model", None):
        config = replace(config, model={**config.model, "mode": args.model})
    if getattr(args, "concurrency", None) is not None:
        config = replace(config, model={**config.model, "concurrency": args.concurrency})
    if getattr(args, "base_url", None):
        config = replace(config, model={**config.model, "base_url": args.base_url.rstrip("/")})
    if getattr(args, "workers", None) is not None:
        config = replace(config, concurrency=args.workers)
    return config


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _print_summary(run_dir: Path) -> None:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    print(f"\nrun {summary['run_id']}: {summary['status']}")
    for suite in summary["suites"]:
        print(f"  {suite['id']} {suite['title']}: {suite['status']}")
    print()
    width = max((len(m["label"]) for m in summary["metrics"]), default=0)
    for metric in summary["metrics"]:
        if metric["unit"] == "rate":
            counted = f" ({metric['numerator']}/{metric['denominator']})" if metric["denominator"] is not None else ""
            value = "n/a" if metric["value"] is None else f"{metric['value']:.1%}{counted}"
            if metric.get("interval"):
                value += f"  [{metric['interval']['low']:.1%}, {metric['interval']['high']:.1%}]"
        else:
            value = str(metric["value"])
        where = " ".join(x for x in (metric["arm"], metric["family"], metric["tier"]) if x) or "all"
        print(f"  {metric['status']:<4}  {metric['label']:<{width}}  {where:<22}  {value}")
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
        where = " ".join(x for x in (metric.get("arm"), metric.get("family"), metric.get("tier")) if x) or "all"
        print(f"  metric {metric['label']} ({where}): {metric['previous']} → {metric['value']}")
    found = report["findings"]
    print(f"  findings: {len(found['new'])} new, {len(found['resolved'])} resolved, {found['persisting']} persisting")
    if report["goldens"]:
        print(f"  goldens: {report['goldens']['drift'] or 'none compared'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cwabench --domain 2", description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="run configuration (default: domain2.toml)")
    commands = parser.add_subparsers(dest="command", required=True)

    run_cmd = commands.add_parser("run", help="run suites and write a run directory")
    run_cmd.add_argument("--suites", help="comma-separated subset of the configured suites")
    run_cmd.add_argument("--size", choices=config_mod.SIZES, help="the families' size, overriding [run].size")
    run_cmd.add_argument("--no-build", action="store_true", help="skip Domain 1's adapter builds; use what is built")
    run_cmd.add_argument("--no-frames", action="store_true", help="S1 assembles the probes only, not every turn")
    run_cmd.add_argument("--model", choices=("llm", "replay"),
                         help="S2's model mode, overriding [model].mode: llm calls the endpoint on a cache miss")
    run_cmd.add_argument("--concurrency", type=_positive, metavar="N",
                         help="model calls in flight, overriding [model].concurrency; what is sent and the results "
                              "written do not depend on it")
    run_cmd.add_argument("--workers", type=_positive, metavar="N",
                         help="snapshots assembled at once (S1, S6), overriding [run].concurrency; a machine's cores")
    run_cmd.add_argument("--base-url", metavar="URL",
                         help="the endpoint, overriding [model].base_url, for a server whose address changes (a rented "
                              "pod); not part of any request, so no cache key depends on it")

    ci_cmd = commands.add_parser("ci", help="run a CI profile ([ci.profiles.*]) replaying the cache, report drift, "
                                           "and publish it as results/d2/<profile>")
    ci_cmd.add_argument("profile", help="a profile in [ci.profiles], e.g. nightly or weekly")
    ci_cmd.add_argument("--no-build", action="store_true", help="skip Domain 1's adapter builds; use what is built")
    ci_cmd.add_argument("--workers", type=_positive, metavar="N",
                        help="snapshots assembled at once (S1, S6), overriding [run].concurrency; a machine's cores")
    where = ci_cmd.add_mutually_exclusive_group()
    where.add_argument("--fetch", action="store_true",
                       help="test upstream: Domain 1's mirrors under .build/ci/checkouts, assemblers at their default "
                            "branch and the spec at the pinned commit")
    where.add_argument("--checkouts",
                       help="read the spec and every assembler from this directory, under the same names")

    goldens_cmd = commands.add_parser("goldens", help="adopt a run's candidate goldens (S7)")
    goldens_sub = goldens_cmd.add_subparsers(dest="goldens_command", required=True)
    accept_cmd = goldens_sub.add_parser("accept", help="adopt a run's candidate goldens as the baseline")
    accept_cmd.add_argument("run_dir", nargs="?", help="default: the latest run")

    validate_cmd = commands.add_parser("validate", help="check a run directory against its schemas and blob digests")
    validate_cmd.add_argument("run_dir", nargs="?", help="default: the latest run")

    fixture_cmd = commands.add_parser("fixture", help="write a run trimmed to a sample of its rows and the blobs they "
                                                      "name, for consumers' tests")
    fixture_cmd.add_argument("run_dir", nargs="?",
                             help="a run directory, or a run id or profile under the results (default: the latest run)")
    fixture_cmd.add_argument("--out", help="the fixtures directory (default: fixtures/runs beside the config)")
    fixture_cmd.add_argument("--rows", type=_positive, default=12,
                             help="rows kept per JSONL file; findings keep every row")

    args = parser.parse_args(argv)
    try:
        config = _load(args)
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
            from .suites.s7_goldens import accept

            run_dir = Path(args.run_dir) if args.run_dir else config.results_dir / "latest"
            target, count = accept(config, run_dir.resolve())
            print(f"adopted {count} golden(s) from {run_dir.resolve().name} → {target}")
            return 0
        if args.command == "fixture":
            from cwabench.fixture import write_fixture
            from cwabench.rundir import load_upstream

            given = args.run_dir
            run_dir = Path(given) if given and Path(given).is_dir() else config.results_dir / (given or "latest")
            out = Path(args.out).resolve() if args.out else config.root / "fixtures" / "runs"
            target, problems = write_fixture(run_dir.resolve(), out, rows=args.rows,
                                             upstream=load_upstream(config.upstream_path), domain=output.D2,
                                             log=lambda m: print(m, file=sys.stderr, flush=True))
            for problem in problems:
                print(f"invalid fixture: {problem}", file=sys.stderr)
            print(f"{target}: {'valid' if not problems else f'{len(problems)} problem(s)'}")
            return 1 if problems else 0
        if args.command == "validate":
            run_dir = Path(args.run_dir) if args.run_dir else config.results_dir / "latest"
            problems = validate_run(run_dir.resolve())
            for problem in problems:
                print(problem)
            print(f"{run_dir}: {'valid' if not problems else f'{len(problems)} problem(s)'}")
            return 1 if problems else 0
    except (config_mod.ConfigError, D1ConfigError, ContractError, ProducerError) as error:
        print(f"cwabench --domain 2: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
