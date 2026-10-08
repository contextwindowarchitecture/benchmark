"""Running assemblers through the adapter protocol (assembler-template/PORTING.md; domain-1-plan.md, section 4.2).

An adapter takes one snapshot's bytes on stdin and answers by exit code: 0 with {"payload": base64 | null,
"trace": {...}} on stdout when it assembled or refused, 2 when it rejected the snapshot before assembly, 3 when the
snapshot names a tokenizer or renderer it does not provide. Every invocation gets exactly one outcome class.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import gitinfo
from .config import AdapterConfig, Config

OUTCOMES = ("assembled", "refused", "rejected", "unsupported", "crashed", "timeout", "invalid_output")
# Outcomes that mean the adapter itself misbehaved, whatever the snapshot was.
FAULTS = ("crashed", "timeout", "invalid_output")

_UNSUPPORTED_LINE = re.compile(r"^\s*(tokenizer|renderer) (\S+) is not provided\s*$")


class SetupError(Exception):
    pass


@dataclass
class Adapter:
    """One adapter, set up and ready to invoke."""

    config: AdapterConfig
    command: list[str]
    env: dict[str, str]
    checkout: gitinfo.Checkout
    toolchain: str | None
    implementation: dict | None  # from the checkout's own conformance-report.json
    committed_report: dict | None
    stale_artifacts: list[str] = field(default_factory=list)
    build_log: list[dict] = field(default_factory=list)
    timing_command: list[str] | None = None  # the in-process timing loop, when built (S7)
    timing_env: dict[str, str] = field(default_factory=dict)
    timing_error: str | None = None  # why the timing loop is not available, when it is configured

    @property
    def name(self) -> str:
        return self.config.name

    def manifest_entry(self) -> dict:
        return {
            "language": self.config.language,
            "implementation": self.implementation,
            "checkout": str(self.config.checkout),
            **self.checkout.as_json(),
            "command": self.command,
            "toolchain": self.toolchain,
            "stale_artifacts": self.stale_artifacts,
            "build": self.build_log,
            "timing": {"command": self.timing_command, "error": self.timing_error}
            if self.config.timing is not None else None,
        }


@dataclass(frozen=True)
class Invocation:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    wall_ms: float
    timed_out: bool


@dataclass(frozen=True)
class Outcome:
    kind: str  # one of OUTCOMES
    payload: bytes | None = None
    trace: dict | None = None
    problem: str | None = None  # why the output is invalid, or what crashed
    unsupported: tuple[tuple[str, str], ...] = ()  # (kind, id) pairs an exit 3 named

    @property
    def refusal_reason(self) -> str | None:
        if self.kind != "refused" or self.trace is None:
            return None
        refused = self.trace.get("refused")
        return refused.get("reason") if isinstance(refused, dict) else None


def _run_logged(argv: list[str], cwd: Path, env: dict[str, str], timeout: float = 1800) -> dict:
    started = time.monotonic()
    try:
        done = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        raise SetupError(f"{argv[0]} is not installed or not on PATH") from None
    except subprocess.TimeoutExpired:
        raise SetupError(f"{' '.join(argv)} did not finish in {timeout:.0f} s") from None
    entry = {
        "argv": argv,
        "cwd": str(cwd),
        "exit_code": done.returncode,
        "seconds": round(time.monotonic() - started, 3),
    }
    if done.returncode != 0:
        tail = (done.stderr or done.stdout).strip().splitlines()[-20:]
        raise SetupError(f"{' '.join(argv)} exited {done.returncode}:\n" + "\n".join(tail))
    return entry


def _committed_report(checkout: Path) -> dict | None:
    path = checkout / "conformance-report.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def setup(config: Config, adapter: AdapterConfig, build: bool = True) -> Adapter:
    """Build the adapter if it has build steps, check what it requires, and describe the checkout."""
    if not adapter.checkout.is_dir():
        raise SetupError(f"checkout {adapter.checkout} does not exist")
    config.build_dir.mkdir(parents=True, exist_ok=True)

    for required in adapter.requires:
        path = Path(config.expand(required, adapter))
        if not path.exists():
            raise SetupError(f"{path} is missing; prepare the checkout first (see domain1.toml)")

    for link, target in adapter.links.items():
        link_path, target_path = Path(config.expand(link, adapter)), Path(config.expand(target, adapter))
        if not target_path.exists():
            raise SetupError(f"{target_path} is missing, so {link_path} cannot link to it")
        link_path.parent.mkdir(parents=True, exist_ok=True)
        if link_path.is_symlink() and link_path.resolve() == target_path.resolve():
            continue
        if link_path.is_symlink() or link_path.exists():
            if not link_path.is_symlink():
                raise SetupError(f"{link_path} exists and is not a symlink; remove it")
            link_path.unlink()
        link_path.symlink_to(target_path, target_is_directory=target_path.is_dir())

    log = []
    if build:
        env = {**os.environ, **{k: config.expand(v, adapter) for k, v in adapter.build_env.items()}}
        for step in adapter.build:
            argv = [config.expand(part, adapter) for part in step]
            log.append(_run_logged(argv, adapter.checkout, env))

    command = [config.expand(part, adapter) for part in adapter.command]
    executable = Path(command[0])
    if executable.is_absolute() and not executable.exists():
        raise SetupError(f"{executable} does not exist; run setup with builds enabled")
    if not executable.is_absolute():
        # Resolved now, so an environment cell that clears PATH still starts the same program.
        found = shutil.which(command[0])
        if found is None:
            raise SetupError(f"{command[0]} is not on PATH")
        command[0] = found

    checkout = gitinfo.inspect(adapter.checkout)
    stale = []
    if checkout.commit_time is not None:
        for artifact in adapter.artifacts:
            path = Path(config.expand(artifact, adapter))
            if path.exists() and path.stat().st_mtime < checkout.commit_time:
                stale.append(str(path))

    toolchain = None
    if adapter.toolchain:
        argv = [config.expand(part, adapter) for part in adapter.toolchain]
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
            toolchain = (done.stdout or done.stderr).strip().splitlines()[0] if done.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired, IndexError):
            toolchain = None

    timing_command, timing_env, timing_error = None, {}, None
    if adapter.timing is not None and "S7" in config.suites:
        try:
            timing_command, timing_env = setup_timing(config, adapter, build)
        except SetupError as error:
            timing_error = str(error)

    report = _committed_report(adapter.checkout)
    return Adapter(
        config=adapter,
        command=command,
        env={k: config.expand(v, adapter) for k, v in adapter.env.items()},
        checkout=checkout,
        toolchain=toolchain,
        implementation=report.get("implementation") if isinstance(report, dict) else None,
        committed_report=report if isinstance(report, dict) else None,
        stale_artifacts=stale,
        build_log=log,
        timing_command=timing_command,
        timing_env=timing_env,
        timing_error=timing_error,
    )


def setup_timing(config: Config, adapter: AdapterConfig, build: bool) -> tuple[list[str], dict[str, str]]:
    """Write the timing loop's generated files, build it into {build}, and return its command and environment. A
    failure leaves the adapter usable; S7 then reports its in-process method as unavailable for that adapter."""
    timing = adapter.timing
    for target, template in timing.templates.items():
        path = Path(config.expand(target, adapter))
        path.parent.mkdir(parents=True, exist_ok=True)
        text = Path(config.expand(template, adapter)).read_text(encoding="utf-8")
        for name, value in (("root", config.root), ("build", config.build_dir), ("checkout", adapter.checkout)):
            text = text.replace("{" + name + "}", str(value))  # templates hold JSON and TOML braces of their own
        path.write_text(text, encoding="utf-8")
    for target, source in timing.copies.items():
        path = Path(config.expand(target, adapter))
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(config.expand(source, adapter), path)
    if build:
        env = {**os.environ, **{k: config.expand(v, adapter) for k, v in timing.build_env.items()}}
        for step in timing.build:
            _run_logged([config.expand(part, adapter) for part in step], adapter.checkout, env)
    command = [config.expand(part, adapter) for part in timing.command]
    executable = Path(command[0])
    if executable.is_absolute() and not executable.exists():
        raise SetupError(f"timing loop {executable} does not exist; run setup with builds enabled")
    if not executable.is_absolute():
        found = shutil.which(command[0])
        if found is None:
            raise SetupError(f"{command[0]} is not on PATH")
        command[0] = found
    return command, {k: config.expand(v, adapter) for k, v in timing.env.items()}


def invoke(
    adapter: Adapter,
    snapshot: bytes,
    timeout: float,
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
    unset: tuple[str, ...] = (),
    base_env: dict[str, str] | None = None,
    wrapper: list[str] | None = None,
) -> Invocation:
    """Start the adapter once with the snapshot's bytes, as written, on stdin (PORTING.md: give it the raw bytes).

    The environment is `base_env` (default: this process's) without `unset`, plus `env`, plus the adapter's own.
    `wrapper` is put before the command, as a sandbox or tracer would be."""
    environment = dict(os.environ if base_env is None else base_env)
    for name in unset:
        environment.pop(name, None)
    environment.update(env or {})
    environment.update(adapter.env)
    argv = [*(wrapper or []), *adapter.command]
    started = time.perf_counter()
    try:
        done = subprocess.run(argv, input=snapshot, capture_output=True, timeout=timeout, cwd=cwd, env=environment)
    except subprocess.TimeoutExpired as expired:
        return Invocation(None, expired.stdout or b"", expired.stderr or b"", (time.perf_counter() - started) * 1000, True)
    except OSError as error:
        return Invocation(None, b"", str(error).encode(), (time.perf_counter() - started) * 1000, False)
    return Invocation(done.returncode, done.stdout, done.stderr, (time.perf_counter() - started) * 1000, False)


def unsupported_components(stderr: bytes) -> tuple[tuple[str, str], ...]:
    lines = stderr.decode("utf-8", "replace").splitlines()
    return tuple((m.group(1), m.group(2)) for m in map(_UNSUPPORTED_LINE.match, lines) if m)


def classify(invocation: Invocation) -> Outcome:
    if invocation.timed_out:
        return Outcome("timeout", problem="no answer before the timeout")
    if invocation.exit_code is None:
        return Outcome("crashed", problem=invocation.stderr.decode("utf-8", "replace") or "could not start")
    if invocation.exit_code == 2:
        return Outcome("rejected")
    if invocation.exit_code == 3:
        return Outcome("unsupported", unsupported=unsupported_components(invocation.stderr))
    if invocation.exit_code != 0:
        tail = invocation.stderr.decode("utf-8", "replace").strip()[-2000:]
        return Outcome("crashed", problem=f"exit {invocation.exit_code}: {tail}")

    try:
        answer = json.loads(invocation.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        return Outcome("invalid_output", problem=f"stdout is not a JSON document: {error}")
    if not isinstance(answer, dict) or set(answer) != {"payload", "trace"}:
        return Outcome("invalid_output", problem="stdout is not an object with exactly payload and trace")
    trace = answer["trace"]
    if not isinstance(trace, dict):
        return Outcome("invalid_output", problem="trace is not an object")

    payload = answer["payload"]
    if payload is not None:
        if not isinstance(payload, str):
            return Outcome("invalid_output", trace=trace, problem="payload is neither base64 text nor null")
        try:
            payload = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            return Outcome("invalid_output", trace=trace, problem="payload is not valid base64")

    refused = trace.get("refused")
    if not isinstance(refused, dict) or not isinstance(refused.get("bool"), bool):
        return Outcome("invalid_output", payload=payload, trace=trace, problem="trace.refused.bool is not a boolean")
    return Outcome("refused" if refused["bool"] else "assembled", payload=payload, trace=trace)
