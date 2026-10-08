"""The run configuration, domain1.toml (domain-1-plan.md, section 13)."""
from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SUITES = ("S0", "S1", "S2", "S4", "S5", "S6", "S7", "S8", "S9", "S10", "S11", "S12")  # suites this build implements; the others arrive in later phases
SETTINGS = ("s2", "s4", "s5", "s6", "s7", "s8", "s9", "s10", "s12", "summarizer", "ci", "container")  # per-suite tables read by the suites themselves


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class TimingConfig:
    """An adapter's in-process timing loop (domain-1-plan.md, 8.3, method 3), built only when S7 runs. `templates` maps
    a file to write → the template it is expanded from, with the same placeholders as commands."""

    command: list[str]
    build: list[list[str]] = field(default_factory=list)
    build_env: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    templates: dict[str, str] = field(default_factory=dict)
    copies: dict[str, str] = field(default_factory=dict)  # file to write → file copied into it


@dataclass(frozen=True)
class AdapterConfig:
    name: str
    language: str
    checkout: Path
    command: list[str]
    build: list[list[str]] = field(default_factory=list)
    build_env: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    requires: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)  # symlink path → target, made before building
    toolchain: list[str] = field(default_factory=list)
    timing: TimingConfig | None = None


@dataclass(frozen=True)
class Config:
    path: Path
    sha256: str
    root: Path
    build_dir: Path
    contract_path: Path
    contract_commit: str
    allow_dirty: bool
    suites: list[str]
    adapters: dict[str, AdapterConfig]  # the ones this run uses, in order
    timeout_s: float
    concurrency: int
    results_dir: Path
    all_adapters: dict[str, AdapterConfig] = field(default_factory=dict)  # every configured adapter
    settings: dict[str, dict] = field(default_factory=dict)  # [s2], [s10], [s12], [container]

    def section(self, name: str) -> dict:
        return self.settings.get(name, {})

    def expand(self, text: str, adapter: AdapterConfig | None = None) -> str:
        """Replace {root}, {build} and {checkout}."""
        values = {"root": str(self.root), "build": str(self.build_dir)}
        if adapter is not None:
            values["checkout"] = str(adapter.checkout)
        try:
            return text.format(**values)
        except KeyError as error:
            raise ConfigError(f"unknown placeholder {error} in {text!r}") from None


def _strings(value, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where} must be a list of strings")
    return value


def _string_table(value, where: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
        raise ConfigError(f"{where} must be a table of strings")
    return dict(value)


def load(path: str | Path) -> Config:
    path = Path(path).resolve()
    raw = path.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"{path}: {error}") from None
    root = path.parent

    contract = data.get("contract") or {}
    run = data.get("run") or {}
    if "path" not in contract or "commit" not in contract:
        raise ConfigError("[contract] needs path and commit")

    adapters: dict[str, AdapterConfig] = {}
    for name, table in (data.get("adapters") or {}).items():
        where = f"[adapters.{name}]"
        if "checkout" not in table or "command" not in table:
            raise ConfigError(f"{where} needs checkout and command")
        build = table.get("build", [])
        if not isinstance(build, list) or not all(isinstance(step, list) for step in build):
            raise ConfigError(f"{where}.build must be a list of commands")
        timing = None
        if "timing" in table:
            t, tw = table["timing"], f"{where}.timing"
            if not isinstance(t, dict) or "command" not in t:
                raise ConfigError(f"{tw} needs command")
            timing_build = t.get("build", [])
            if not isinstance(timing_build, list) or not all(isinstance(step, list) for step in timing_build):
                raise ConfigError(f"{tw}.build must be a list of commands")
            timing = TimingConfig(
                command=_strings(t["command"], f"{tw}.command"),
                build=[_strings(step, f"{tw}.build") for step in timing_build],
                build_env=_string_table(t.get("build_env", {}), f"{tw}.build_env"),
                env=_string_table(t.get("env", {}), f"{tw}.env"),
                templates=_string_table(t.get("templates", {}), f"{tw}.templates"),
                copies=_string_table(t.get("copies", {}), f"{tw}.copies"),
            )
        adapters[name] = AdapterConfig(
            name=name,
            language=str(table.get("language", name)),
            checkout=(root / table["checkout"]).resolve(),
            command=_strings(table["command"], f"{where}.command"),
            build=[_strings(step, f"{where}.build") for step in build],
            build_env=_string_table(table.get("build_env", {}), f"{where}.build_env"),
            env=_string_table(table.get("env", {}), f"{where}.env"),
            requires=_strings(table.get("requires", []), f"{where}.requires"),
            artifacts=_strings(table.get("artifacts", []), f"{where}.artifacts"),
            links=_string_table(table.get("links", {}), f"{where}.links"),
            toolchain=_strings(table.get("toolchain", []), f"{where}.toolchain"),
            timing=timing,
        )

    suites = _strings(run.get("suites", list(SUITES)), "[run].suites")
    unknown = [s for s in suites if s not in SUITES]
    if unknown:
        raise ConfigError(f"suites not implemented yet: {', '.join(unknown)} (available: {', '.join(SUITES)})")
    selected = _strings(run.get("adapters", list(adapters)), "[run].adapters")
    missing = [a for a in selected if a not in adapters]
    if missing:
        raise ConfigError(f"[run].adapters names adapters with no [adapters.*] table: {', '.join(missing)}")

    settings = {}
    for name in SETTINGS:
        table = data.get(name, {})
        if not isinstance(table, dict):
            raise ConfigError(f"[{name}] must be a table")
        settings[name] = table
    if "S12" in suites and not {"S1", "S6", "S8", "S9"} & set(suites):
        raise ConfigError("S12 compares the answers of S1, S6, S8 or S9 with the goldens, so it needs one of them")

    return Config(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        root=root,
        build_dir=root / ".build",
        contract_path=(root / contract["path"]).resolve(),
        contract_commit=str(contract["commit"]),
        allow_dirty=bool(contract.get("allow_dirty", False)),
        suites=suites,
        adapters={name: adapters[name] for name in selected},
        timeout_s=float(run.get("timeout_s", 60)),
        concurrency=max(1, int(run.get("concurrency", 8))),
        results_dir=(root / run.get("results_dir", "results/d1")).resolve(),
        all_adapters=adapters,
        settings=settings,
    )
