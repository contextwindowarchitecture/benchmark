"""The run configuration, domain5.toml (domain-5-plan.md, 12). This build reads the tables P0 and P1 need: the
contract pin, the run, the task families, the attacks, Domain 1's adapters, the budgets' settings, the arms, the grid,
S1's tokenizer check and S7's goldens; the others (models, axes, external sets, thresholds, CI profiles) arrive with
the phases that use them."""
from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from cwabench2.application.snapshots import Settings
from cwabench2.config import MODEL_DEFAULTS

from .arms import ARMS
from .attacks import FAMILIES as ATTACK_FAMILIES
from .attacks.carriers import MARKERS
from .tasks import FAMILIES as TASK_FAMILIES

SUITES = ("S0", "S1", "S2", "S7")
SIZES = ("pilot", "recorded")
ADAPTER_SUITES = ("S1",)  # suites that assemble, and so set up the adapters
TOKENIZE = ("off", "replay", "server")
MODEL_MODES = ("llm", "replay")
S2_DEFAULTS = {"arms": None, "attacked_samples": 1, "clean_samples": 3, "sensitivity_every": 10,
               "sensitivity_samples": 5, "sensitivity_temperature": 0.7, "bootstrap_resamples": 2000,
               "bootstrap_seed": 20261013,
               "pairs": [["naive", "xml-unescaped"], ["xml-unescaped", "cwa"], ["naive", "cwa"],
                         ["reminder", "cwa-boundary"], ["cwa-boundary", "cwa-reinforced"],
                         ["tool-role", "cwa-observation"]]}


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
class AdaptersConfig:
    """Domain 1's adapters, which Domain 5 assembles with: their tables in Domain 1's configuration, and its builds."""

    config: Path  # Domain 1's configuration file
    use: list[str]
    payload_source: str  # whose payload the model is sent; the others must match it byte for byte


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
    timeout_s: float = 60.0  # per assembly
    concurrency: int = 1  # snapshots in flight, each on every adapter at once
    settings: dict[str, dict] = field(default_factory=dict)
    adapters: AdaptersConfig | None = None
    application: Settings = field(default_factory=Settings)  # reserved output, margin, tokenizer, renderer
    arms: list[str] = field(default_factory=lambda: list(ARMS))
    html_templates: int = 10  # the representation sweep's test templates (grid.py)
    lengths: list[int] = field(default_factory=lambda: [1000, 4000, 16000, 32000])  # the length sweep's (grid.py)
    length_templates: int = 10
    tokenize: str = "off"  # S1's tokenizer check (oracles/tokens.py)
    tokenize_base_url: str = "http://127.0.0.1:8000/v1"
    tokenize_model: str = "qwen3.6-35b-a3b-fp8"
    tokenize_cells: int = 2  # marker cells per task family
    goldens: Path | None = None  # S7's adopted goldens
    model: dict = field(default_factory=lambda: dict(MODEL_DEFAULTS))  # Domain 2's [model] keys
    s2: dict = field(default_factory=lambda: dict(S2_DEFAULTS))
    checkouts: Path | None = None  # CI: the spec and Domain 1's assemblers read from here, by directory name


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

    adapters = None
    table = _table(data, "adapters")
    if table:
        if "config" not in table:
            raise ConfigError("[adapters] needs config: Domain 1's configuration, whose adapter tables Domain 5 uses")
        use = table.get("use", [])
        if not isinstance(use, list) or not use or not all(isinstance(u, str) for u in use):
            raise ConfigError("[adapters].use must be a non-empty list of adapter names")
        source = table.get("payload_source", use[0])
        if source not in use:
            raise ConfigError(f"[adapters].payload_source {source!r} is not in [adapters].use")
        adapters = AdaptersConfig((root / table["config"]).resolve(), list(use), source)
    if any(s in ADAPTER_SUITES for s in suites) and adapters is None:
        raise ConfigError("S1 assembles, so it needs [adapters]")
    if "S7" in suites and "S1" not in suites:
        raise ConfigError("S7 reads S1's rows, so it needs S1")

    budgets = _table(data, "budgets")
    unknown_keys = sorted(set(budgets) - {"reserved_output", "margin_percent", "tokenizer", "renderer"})
    if unknown_keys:
        raise ConfigError(f"[budgets] has unknown keys: {', '.join(unknown_keys)}")
    application = Settings(**budgets)
    arms = _table(data, "arms").get("use", list(ARMS))
    stray = [a for a in arms if a not in ARMS]
    if stray:
        raise ConfigError(f"[arms].use names arms this build does not have: {', '.join(stray)}")
    if "xml-unescaped" in arms and "cwa" not in arms:
        raise ConfigError("xml-unescaped is the cwa arm's payload unescaped, so it needs cwa")
    grid = _table(data, "grid")
    s1 = _table(data, "s1")
    tokenize = s1.get("tokenize", "off")
    if tokenize not in TOKENIZE:
        raise ConfigError(f"[s1].tokenize must be one of {', '.join(TOKENIZE)}")
    s7 = _table(data, "s7")

    model = {**MODEL_DEFAULTS, **_table(data, "model")}
    unknown_keys = sorted(set(model) - set(MODEL_DEFAULTS))
    if unknown_keys:
        raise ConfigError(f"[model] has unknown keys: {', '.join(unknown_keys)}")
    if model["mode"] not in MODEL_MODES:
        raise ConfigError(f"[model].mode must be one of {', '.join(MODEL_MODES)}")
    s2 = {**S2_DEFAULTS, **_table(data, "s2")}
    unknown_keys = sorted(set(s2) - set(S2_DEFAULTS))
    if unknown_keys:
        raise ConfigError(f"[s2] has unknown keys: {', '.join(unknown_keys)}")
    if s2["arms"] is None:
        s2["arms"] = list(arms)
    stray = [a for a in s2["arms"] if a not in arms]
    if stray:
        raise ConfigError(f"[s2].arms names arms this run does not build: {', '.join(stray)}")
    stray = [p for p in s2["pairs"] if len(p) != 2 or any(a not in s2["arms"] for a in p)]
    if stray and "S2" in suites:
        raise ConfigError(f"[s2].pairs names arms S2 does not send: {stray}")
    if "S2" in suites and "S1" not in suites:
        raise ConfigError("S2 sends what S1 gated, so it needs S1")

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
        adapters=adapters,
        application=application,
        arms=list(arms),
        html_templates=int(grid.get("html_templates", 10)),
        lengths=[int(n) for n in grid.get("lengths", [1000, 4000, 16000, 32000])],
        length_templates=int(grid.get("length_templates", 10)),
        tokenize=tokenize,
        tokenize_base_url=str(s1.get("tokenize_base_url", "http://127.0.0.1:8000/v1")),
        tokenize_model=str(s1.get("tokenize_model", "qwen3.6-35b-a3b-fp8")),
        tokenize_cells=int(s1.get("tokenize_cells", 2)),
        goldens=(root / s7.get("goldens", "goldens/d5-goldens.json")).resolve(),
        model=model,
        s2=s2,
    )
