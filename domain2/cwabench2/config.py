"""The run configuration, domain2.toml (domain-2-plan.md, 12). This build reads the tables P0 to P6 need; the others
(thresholds, caps, CI profiles) arrive with the phases that use them."""
from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import baselines as baselines_mod
from .application.snapshots import ARMS, Settings
from .conversations import FAMILIES

SUITES = ("S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7")
MODEL_MODES = ("llm", "replay")
MODEL_DEFAULTS = {"base_url": "http://127.0.0.1:8000/v1", "model": "Qwen3.6-35B-A3B-8bit",
                  "api_key_env": "CWA_BENCH_MODEL_KEY", "temperature": 0, "seed": 7, "max_tokens": 512,
                  "extra_body": {}, "request_timeout_s": 300, "retries": 2, "concurrency": 2, "mode": "replay",
                  "cache": "model-cache", "context_limit": None, "producer_max_tokens": 1024,
                  "seed_per_sample": False}
S2_DEFAULTS = {"arms": None, "tiers": ["8192"], "repeats": 3, "reference": "truncate-pinned",
               "bootstrap_resamples": 2000, "bootstrap_seed": 20261009}
S3_DEFAULTS = {**S2_DEFAULTS, "repeats": 5, "temperature": 0.7}
S4_DEFAULTS = {"arms": ["control-full", "truncate-pinned", "rag", "cwa-evidence", "cwa-reinforced"], "budget": 8192,
               "ratios": [0.25, 1.0, 4.0, 16.0], "candidates": 64, "repeats": 3, "reference": "rag",
               "bootstrap_resamples": 2000, "bootstrap_seed": 20261009}
S6_DEFAULTS = {"families": ["vt"], "arms": ["truncate-pinned", "window", "summary", "cwa-history", "cwa-state",
                                             "cwa-memory"],
               "tier": "8192", "repeats": 3, "temperature": 0.7, "reference": "truncate-pinned",
               "bootstrap_resamples": 2000, "bootstrap_seed": 20261009}
LQ = "lq"  # the long-context family (conversations/longcontext.py): corpora, not conversations, and only S4 asks them
ADAPTER_SUITES = ("S1", "S6")  # suites that assemble, and so set up the adapters
SIZES = ("pilot", "recorded")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class FamilyConfig:
    name: str
    seed: int
    sizes: dict[str, int]  # conversations per turn count, by size
    parameters: dict  # the generator's parameters (conversations/<family>.py)


@dataclass(frozen=True)
class AdaptersConfig:
    """Domain 1's adapters, which Domain 2 assembles with: their tables in Domain 1's configuration, and its builds."""

    config: Path  # Domain 1's configuration file
    use: list[str]
    payload_source: str  # whose payload goes to the model; the others must match it byte for byte


@dataclass(frozen=True)
class Budgets:
    input: list[int]  # absolute budget.input values
    ratios: list[float]  # budgets as a fraction of each snapshot's full charged count

    def of(self, full: int) -> list[tuple[str, int]]:
        """(tier, budget) for a snapshot whose full charged count is `full`: the absolute budgets, then the ratios."""
        tiers = [(str(b), b) for b in self.input]
        tiers += [(f"r{r:.2f}", max(1, -(-full * round(r * 10000) // 10000))) for r in self.ratios]
        return tiers


@dataclass(frozen=True)
class Config:
    path: Path
    sha256: str
    root: Path
    contract_path: Path
    contract_commit: str
    allow_dirty: bool
    suites: list[str]
    size: str  # which of each family's sizes a run generates
    turn_counts: list[int]
    checkpoint_every: int
    families: dict[str, FamilyConfig]  # the ones this run generates, in order
    results_dir: Path
    upstream_path: Path | None = None
    timeout_s: float = 60.0
    concurrency: int = 1
    settings: dict[str, dict] = field(default_factory=dict)
    adapters: AdaptersConfig | None = None
    arms: list[str] = field(default_factory=lambda: list(ARMS))
    budgets: Budgets = field(default_factory=lambda: Budgets([8192, 16384, 32768], [1.0, 0.5, 0.25, 0.1]))
    application: Settings = field(default_factory=Settings)
    frames: bool = True  # S1 assembles every turn, not only the probes
    timelines: bool = True  # S1 writes the timelines of each conversation's last probe
    goldens: Path | None = None  # S7's adopted goldens
    baselines: list[str] = field(default_factory=lambda: list(baselines_mod.ARMS))
    model: dict = field(default_factory=lambda: dict(MODEL_DEFAULTS))
    s2: dict = field(default_factory=lambda: dict(S2_DEFAULTS))
    s3: dict = field(default_factory=lambda: dict(S3_DEFAULTS))
    s4: dict = field(default_factory=lambda: dict(S4_DEFAULTS))
    s6: dict = field(default_factory=lambda: dict(S6_DEFAULTS))
    lq: FamilyConfig | None = None  # [families.lq]: the corpora S4 asks about
    baseline: baselines_mod.Settings = field(default_factory=baselines_mod.Settings)

    def section(self, name: str) -> dict:
        return self.settings.get(name, {})


def requirements(suites: list[str], adapters: AdaptersConfig | None, lq: FamilyConfig | None,
                 s6: dict | None = None, model: dict | None = None) -> None:
    """What the chosen suites need of each other and of the configuration; checked again when `run --suites` chooses
    other suites than the file's."""
    if "S1" in suites and adapters is None:
        raise ConfigError("S1 assembles, so it needs [adapters]")
    s6 = s6 or S6_DEFAULTS
    if "S6" in suites and adapters is None and any(a in ARMS for a in s6["arms"]):
        raise ConfigError("S6 assembles its CWA arms' snapshots, so it needs [adapters]")
    if "S6" in suites and s6["repeats"] > 1 and s6["temperature"] > 0 and not (model or {}).get("seed_per_sample"):
        raise ConfigError("S6 forks a chain per sample, so it needs [model].seed_per_sample: with one seed for every "
                          "sample, a server that honours it answers every chain alike")
    for suite in ("S2", "S3", "S4", "S7"):
        if suite in suites and "S1" not in suites:
            raise ConfigError(f"{suite} uses what S1 gated or built, so it needs S1")
    if "S4" in suites and lq is None:
        raise ConfigError("S4 asks the LQ family's questions, so it needs [families.lq]")
    if "S5" in suites and "S2" not in suites:
        raise ConfigError("S5 reads S2's records, so it needs S2")


def _table(data: dict, name: str) -> dict:
    table = data.get(name) or {}
    if not isinstance(table, dict):
        raise ConfigError(f"[{name}] must be a table")
    return table


def _positive_ints(value, where: str) -> list[int]:
    if not isinstance(value, list) or not value or not all(isinstance(v, int) and v > 0 for v in value):
        raise ConfigError(f"{where} must be a non-empty list of positive integers")
    return value


def load(path: str | Path) -> Config:
    path = Path(path).resolve()
    raw = path.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"{path}: {error}") from None
    root = path.parent

    contract, run, turns = _table(data, "contract"), _table(data, "run"), _table(data, "turns")
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

    turn_counts = _positive_ints(turns.get("counts", [10, 50, 100]), "[turns].counts")
    every = turns.get("checkpoint_every", 10)
    if not isinstance(every, int) or every < 1:
        raise ConfigError("[turns].checkpoint_every must be a positive integer")

    families: dict[str, FamilyConfig] = {}
    lq = None
    for name, table in _table(data, "families").items():
        where = f"[families.{name}]"
        if name not in FAMILIES and name != LQ:
            raise ConfigError(f"{where}: no such family (available: {', '.join([*FAMILIES, LQ])})")
        table = dict(table)
        seed, sizes = table.pop("seed", None), table.pop("sizes", None)
        if not isinstance(seed, int):
            raise ConfigError(f"{where}.seed must be an integer")
        if not isinstance(sizes, dict) or set(sizes) != set(SIZES) or not all(
                isinstance(v, int) and v >= 0 for v in sizes.values()):
            raise ConfigError(f"{where}.sizes must give {' and '.join(SIZES)} as counts")
        if name == LQ:
            lq = FamilyConfig(name, seed, dict(sizes), table)
            continue
        families[name] = FamilyConfig(name, seed, dict(sizes), table)
    selected = run.get("families", list(families))
    missing = [f for f in selected if f not in families]
    if missing:
        raise ConfigError(f"[run].families names families with no [families.*] table: {', '.join(missing)}")

    adapters = None
    table = _table(data, "adapters")
    if table:
        if "config" not in table:
            raise ConfigError("[adapters] needs config: Domain 1's configuration, whose adapter tables Domain 2 uses")
        use = table.get("use", [])
        if not isinstance(use, list) or not use or not all(isinstance(u, str) for u in use):
            raise ConfigError("[adapters].use must be a non-empty list of adapter names")
        source = table.get("payload_source", use[0])
        if source not in use:
            raise ConfigError(f"[adapters].payload_source {source!r} is not in [adapters].use")
        adapters = AdaptersConfig((root / table["config"]).resolve(), list(use), source)

    arms = _table(data, "arms").get("cwa", list(ARMS))
    unknown_arms = [a for a in arms if a not in ARMS]
    if unknown_arms:
        raise ConfigError(f"[arms].cwa names arms this build does not have: {', '.join(unknown_arms)} "
                          f"(available: {', '.join(ARMS)})")
    chosen = _table(data, "arms").get("baselines", list(baselines_mod.ARMS))
    unknown_baselines = [a for a in chosen if a not in baselines_mod.ARMS]
    if unknown_baselines:
        raise ConfigError(f"[arms].baselines names baselines this build does not have: {', '.join(unknown_baselines)} "
                          f"(available: {', '.join(baselines_mod.ARMS)})")
    budgets_table = _table(data, "budgets")
    ratios = budgets_table.get("ratios", [1.0, 0.5, 0.25, 0.1])
    if not isinstance(ratios, list) or not all(isinstance(r, (int, float)) and 0 < r <= 1 for r in ratios):
        raise ConfigError("[budgets].ratios must be fractions in (0, 1]")
    budgets = Budgets(_positive_ints(budgets_table.get("input", [8192, 16384, 32768]), "[budgets].input"),
                      [float(r) for r in ratios])
    application = _table(data, "application")
    known = {"turn_seconds", "history_turns", "memory_ttl_seconds"}
    unknown_keys = sorted(set(application) - known)
    if unknown_keys:
        raise ConfigError(f"[application] has unknown keys: {', '.join(unknown_keys)}")
    settings = Settings(
        **{k: int(v) for k, v in application.items()},
        **{k: budgets_table[k] for k in ("reserved_output", "margin_percent", "tokenizer", "renderer")
           if k in budgets_table})
    b = _table(data, "baselines")
    unknown_keys = sorted(set(b) - {"window_turns", "summarizer", "extractive_ratio", "summary_words"})
    if unknown_keys:
        raise ConfigError(f"[baselines] has unknown keys: {', '.join(unknown_keys)}")
    if b.get("summarizer", "stub") not in ("stub", "llm"):
        raise ConfigError("[baselines].summarizer must be stub or llm")
    baseline = baselines_mod.Settings(window_turns=int(b.get("window_turns", 10)),
                                      summarizer=b.get("summarizer", "stub"),
                                      extractive_ratio=float(b.get("extractive_ratio", 0.4)),
                                      summary_words=int(b.get("summary_words", 150)),
                                      margin_percent=settings.margin_percent, tokenizer=settings.tokenizer)
    model = {**MODEL_DEFAULTS, **_table(data, "model")}
    unknown_keys = sorted(set(model) - set(MODEL_DEFAULTS))
    if unknown_keys:
        raise ConfigError(f"[model] has unknown keys: {', '.join(unknown_keys)}")
    if model["mode"] not in MODEL_MODES:
        raise ConfigError(f"[model].mode must be one of {', '.join(MODEL_MODES)}")
    from .suites.s2_scripted import CONTROLS
    absolute = {str(b) for b in budgets.input}
    asked = {}
    for name, defaults in (("s2", S2_DEFAULTS), ("s3", S3_DEFAULTS)):
        table = {**defaults, **_table(data, name)}
        unknown_keys = sorted(set(table) - set(defaults))
        if unknown_keys:
            raise ConfigError(f"[{name}] has unknown keys: {', '.join(unknown_keys)}")
        if table["arms"] is None:
            table["arms"] = [*CONTROLS, *chosen, *arms]
        stray = [a for a in table["arms"] if a not in {*CONTROLS, *chosen, *arms}]
        if stray:
            raise ConfigError(f"[{name}].arms names arms this run does not build: {', '.join(stray)}")
        stray = [t for t in table["tiers"] if t not in absolute]
        if stray and name.upper() in suites:
            raise ConfigError(f"[{name}].tiers must be absolute budgets of [budgets].input; not {', '.join(stray)}")
        if table["reference"] not in table["arms"]:
            raise ConfigError(f"[{name}].reference {table['reference']!r} is not one of [{name}].arms")
        if not isinstance(table["repeats"], int) or table["repeats"] < 1:
            raise ConfigError(f"[{name}].repeats must be a positive integer")
        asked[name] = table
    s2, s3 = asked["s2"], asked["s3"]
    s4 = {**S4_DEFAULTS, **_table(data, "s4")}
    unknown_keys = sorted(set(s4) - set(S4_DEFAULTS))
    if unknown_keys:
        raise ConfigError(f"[s4] has unknown keys: {', '.join(unknown_keys)}")
    from .application.evidence import ARMS as LQ_CWA
    from .baselines.longcontext import ARMS as LQ_BASELINES

    stray = [a for a in s4["arms"] if a not in (*LQ_BASELINES, *LQ_CWA)]
    if stray:
        raise ConfigError(f"[s4].arms names arms the LQ family does not have: {', '.join(stray)} "
                          f"(available: {', '.join([*LQ_BASELINES, *LQ_CWA])})")
    if s4["reference"] not in s4["arms"]:
        raise ConfigError(f"[s4].reference {s4['reference']!r} is not one of [s4].arms")
    if not isinstance(s4["budget"], int) or s4["budget"] < 1:
        raise ConfigError("[s4].budget must be a positive integer")
    if not isinstance(s4["ratios"], list) or not s4["ratios"] or not all(
            isinstance(r, (int, float)) and r > 0 for r in s4["ratios"]):
        raise ConfigError("[s4].ratios must be a non-empty list of positive numbers")
    for key in ("candidates", "repeats"):
        if not isinstance(s4[key], int) or s4[key] < 1:
            raise ConfigError(f"[s4].{key} must be a positive integer")
    s4["ratios"] = [float(r) for r in s4["ratios"]]
    s6 = {**S6_DEFAULTS, **_table(data, "s6")}
    unknown_keys = sorted(set(s6) - set(S6_DEFAULTS))
    if unknown_keys:
        raise ConfigError(f"[s6] has unknown keys: {', '.join(unknown_keys)}")
    stray = [a for a in s6["arms"] if a not in (*chosen, *arms) or a in CONTROLS]
    if stray:
        raise ConfigError(f"[s6].arms names arms this run does not build, or a control: {', '.join(stray)}")
    if s6["reference"] not in s6["arms"]:
        raise ConfigError(f"[s6].reference {s6['reference']!r} is not one of [s6].arms")
    stray = [f for f in s6["families"] if f not in families]
    if stray and "S6" in suites:
        raise ConfigError(f"[s6].families names families this run does not generate: {', '.join(stray)}")
    if s6["tier"] not in absolute and "S6" in suites:
        raise ConfigError(f"[s6].tier must be an absolute budget of [budgets].input; not {s6['tier']}")
    if not isinstance(s6["repeats"], int) or s6["repeats"] < 1:
        raise ConfigError("[s6].repeats must be a positive integer")
    requirements(suites, adapters, lq, s6, model)
    s1 = _table(data, "s1")
    s7 = _table(data, "s7")

    findings = _table(data, "findings")
    upstream_path = (root / str(findings.get("upstream", "findings/upstream.json"))).resolve()
    if not upstream_path.is_file():
        if "upstream" in findings:
            raise ConfigError(f"[findings].upstream names {upstream_path}, which does not exist")
        upstream_path = None  # the default file is optional: no file, no links

    return Config(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        root=root,
        contract_path=(root / contract["path"]).resolve(),
        contract_commit=str(contract["commit"]),
        allow_dirty=bool(contract.get("allow_dirty", False)),
        suites=suites,
        size=size,
        turn_counts=turn_counts,
        checkpoint_every=every,
        families={name: families[name] for name in selected},
        results_dir=(root / run.get("results_dir", "results/d2")).resolve(),
        upstream_path=upstream_path,
        timeout_s=float(run.get("timeout_s", 60)),
        concurrency=max(1, int(run.get("concurrency", 1))),
        settings={},
        adapters=adapters,
        arms=list(arms),
        budgets=budgets,
        application=settings,
        frames=bool(s1.get("frames", True)),
        timelines=bool(s1.get("timelines", True)),
        goldens=(root / s7.get("goldens", "goldens/d2-goldens.json")).resolve(),
        baselines=list(chosen),
        baseline=baseline,
        model=model,
        s2=s2,
        s3=s3,
        s4=s4,
        s6=s6,
        lq=lq,
    )
