"""cwabench2: run Domain 2's suites and check run directories."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

from cwabench.contract import ContractError
from cwabench.validate import validate_run

from . import config as config_mod
from . import output  # noqa: F401  (registers Domain 2's prefix, so validate_run reads its documents)
from .runner import run

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "domain2.toml"


def _load(args) -> config_mod.Config:
    config = config_mod.load(args.config)
    if getattr(args, "suites", None):
        suites = [s.strip() for s in args.suites.split(",") if s.strip()]
        unknown = [s for s in suites if s not in config_mod.SUITES]
        if unknown:
            raise config_mod.ConfigError(f"suites not implemented yet: {', '.join(unknown)}")
        config = replace(config, suites=suites)
    if getattr(args, "size", None):
        config = replace(config, size=args.size)
    return config


def _print_summary(run_dir: Path) -> None:
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    print(f"\nrun {summary['run_id']}: {summary['status']}")
    for suite in summary["suites"]:
        print(f"  {suite['id']} {suite['title']}: {suite['status']}")
    print()
    width = max((len(m["label"]) for m in summary["metrics"]), default=0)
    for metric in summary["metrics"]:
        if metric["unit"] == "rate":
            value = ("n/a" if metric["value"] is None
                     else f"{metric['value']:.1%} ({metric['numerator']}/{metric['denominator']})")
        else:
            value = str(metric["value"])
        print(f"  {metric['status']:<4}  {metric['label']:<{width}}  {value}")
    print(f"\nfindings: {summary['findings']['total']}  →  {run_dir}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cwabench2", description=__doc__)
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="run configuration (default: domain2.toml)")
    commands = parser.add_subparsers(dest="command", required=True)

    run_cmd = commands.add_parser("run", help="run suites and write a run directory")
    run_cmd.add_argument("--suites", help="comma-separated subset of the configured suites")
    run_cmd.add_argument("--size", choices=config_mod.SIZES, help="the families' size, overriding [run].size")

    validate_cmd = commands.add_parser("validate", help="check a run directory against its schemas and blob digests")
    validate_cmd.add_argument("run_dir", nargs="?", help="default: the latest run")

    args = parser.parse_args(argv)
    try:
        config = _load(args)
        if args.command == "run":
            run_dir, status = run(config)
            _print_summary(run_dir)
            problems = validate_run(run_dir)
            for problem in problems:
                print(f"invalid output: {problem}", file=sys.stderr)
            return 0 if status == "pass" and not problems else 1
        if args.command == "validate":
            run_dir = Path(args.run_dir) if args.run_dir else config.results_dir / "latest"
            problems = validate_run(run_dir.resolve())
            for problem in problems:
                print(problem)
            print(f"{run_dir}: {'valid' if not problems else f'{len(problems)} problem(s)'}")
            return 1 if problems else 0
    except (config_mod.ConfigError, ContractError) as error:
        print(f"cwabench2: {error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
