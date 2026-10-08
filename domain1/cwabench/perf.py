"""Performance measurement (domain-1-plan.md, section 8): timed invocations with their peak memory, in-process timing
loops, sampling until the confidence interval is tight, and log-log scaling fits.

Times are taken three ways (8.3):
    e2e        the adapter started per snapshot, as an application spawning it pays
    net        e2e minus the adapter's median on a minimal snapshot on this host: its startup
    inprocess  each assembler's own timing loop, built from its checkout (adapters/timing/), with no process start
and a fourth series where the assembler reports its own stage timings in trace.timings.
"""
from __future__ import annotations

import math
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from .adapters import Adapter, Invocation

# Two-sided 95% Student t quantiles by degrees of freedom; 1.96 past the table.
_T95 = {1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365, 8: 2.306, 9: 2.262, 10: 2.228,
        11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145, 15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
        25: 2.060, 30: 2.042, 40: 2.021, 60: 2.000, 120: 1.980}


def t95(df: int) -> float:
    if df <= 0:
        return math.inf
    for bound in sorted(_T95):
        if df <= bound:
            return _T95[bound]
    return 1.96


@dataclass(frozen=True)
class Measured:
    invocation: Invocation
    rss_bytes: int | None  # the child's peak resident set, from wait4


def _rss_bytes(ru_maxrss: int) -> int:
    return ru_maxrss if os.uname().sysname == "Darwin" else ru_maxrss * 1024  # macOS reports bytes, Linux KiB


def invoke_measured(adapter: Adapter, snapshot: bytes, timeout: float, cwd, command: list[str] | None = None,
                    args: list[str] | None = None, env: dict[str, str] | None = None) -> Measured:
    """adapters.invoke, plus the child's own peak RSS: os.wait4 reports the rusage of exactly that process."""
    environment = {**os.environ, **(env if env is not None else adapter.env)}
    argv = [*(command or adapter.command), *(args or [])]
    started = time.perf_counter()
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   cwd=cwd, env=environment)
    except OSError as error:
        return Measured(Invocation(None, b"", str(error).encode(), (time.perf_counter() - started) * 1000, False), None)
    out, err = [], []

    def pump(stream, into):
        into.append(stream.read())
        stream.close()

    readers = [threading.Thread(target=pump, args=(process.stdout, out), daemon=True),
               threading.Thread(target=pump, args=(process.stderr, err), daemon=True)]
    for reader in readers:
        reader.start()
    timed_out = threading.Event()

    def kill():
        timed_out.set()
        process.kill()

    timer = threading.Timer(timeout, kill)
    timer.start()
    try:
        try:
            process.stdin.write(snapshot)
        except BrokenPipeError:
            pass
        process.stdin.close()
        _, status, usage = os.wait4(process.pid, 0)
    finally:
        timer.cancel()
    elapsed = (time.perf_counter() - started) * 1000
    process.returncode = os.waitstatus_to_exitcode(status)
    for reader in readers:
        reader.join()
    stdout, stderr = (out[0] if out else b""), (err[0] if err else b"")
    if timed_out.is_set():
        return Measured(Invocation(None, stdout, stderr, elapsed, True), None)
    code = process.returncode if process.returncode >= 0 else None
    if code is None:
        stderr += f"\nkilled by signal {-process.returncode}".encode()
    return Measured(Invocation(code, stdout, stderr, elapsed, False), _rss_bytes(usage.ru_maxrss))


@dataclass
class Stats:
    samples: list[float]  # milliseconds, in the order taken

    @property
    def n(self) -> int:
        return len(self.samples)

    @property
    def mean(self) -> float:
        return sum(self.samples) / self.n

    @property
    def stdev(self) -> float:
        if self.n < 2:
            return 0.0
        mean = self.mean
        return math.sqrt(sum((x - mean) ** 2 for x in self.samples) / (self.n - 1))

    @property
    def ci95_halfwidth(self) -> float | None:
        """Half the width of the 95% confidence interval of the mean."""
        return None if self.n < 2 else t95(self.n - 1) * self.stdev / math.sqrt(self.n)

    @property
    def ci95_rate(self) -> float | None:
        """The half-width as a fraction of the mean, the stopping rule's measure (8.4)."""
        half = self.ci95_halfwidth
        return None if half is None or self.mean <= 0 else half / self.mean

    def percentile(self, p: float) -> float:
        ordered = sorted(self.samples)
        index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * p / 100) - 1))
        return ordered[index]

    def as_json(self, ci_target: float) -> dict:
        rate = self.ci95_rate
        return {
            "samples": self.n, "p50_ms": round(self.percentile(50), 3), "p95_ms": round(self.percentile(95), 3),
            "p99_ms": round(self.percentile(99), 3), "max_ms": round(max(self.samples), 3),
            "mean_ms": round(self.mean, 3), "stdev_ms": round(self.stdev, 3),
            "ci95_halfwidth_ms": None if self.ci95_halfwidth is None else round(self.ci95_halfwidth, 3),
            "ci95_rate": None if rate is None else round(rate, 5),
            "ci_met": rate is not None and rate <= ci_target,
        }


@dataclass(frozen=True)
class Policy:
    """When to stop sampling a cell (8.4): at least `min_samples` (`min_slow` once a sample takes over `slow_ms`), then
    until the 95% CI half-width is under `ci_target` of the mean or `max_samples` are taken. `max_seconds` cuts both
    short, but never below `min_slow` samples, so every cell has a confidence interval."""

    warmup: int = 1
    min_samples: int = 10
    min_slow: int = 3
    slow_ms: float = 1000.0
    max_samples: int = 30
    max_seconds: float = 20.0
    ci_target: float = 0.05

    def done(self, stats: Stats, spent: float) -> bool:
        if stats.n >= self.max_samples:
            return True
        if stats.n < self.min_slow:
            return False
        if spent >= self.max_seconds:
            return True
        floor = self.min_slow if stats.mean >= self.slow_ms else self.min_samples
        if stats.n < floor:
            return False
        rate = stats.ci95_rate
        return rate is not None and rate <= self.ci_target


def sample(take: Callable[[], float | None], policy: Policy) -> tuple[Stats, bool]:
    """Run `take` (one timed run in ms, or None when it failed or timed out) under the policy. Returns the samples and
    whether every run succeeded; sampling stops at the first failure."""
    for _ in range(policy.warmup):
        if take() is None:
            return Stats([]), False
    stats, started = Stats([]), time.monotonic()
    while not policy.done(stats, time.monotonic() - started):
        value = take()
        if value is None:
            return stats, False
        stats.samples.append(value)
    return stats, True


@dataclass
class Fit:
    points: int
    coefficients: dict[str, float]  # name → exponent
    intercept: float
    r2: float | None
    residual_se: float | None
    standard_errors: dict[str, float] = field(default_factory=dict)

    def as_json(self) -> dict:
        return {"points": self.points, "exponents": {k: round(v, 4) for k, v in self.coefficients.items()},
                "standard_errors": {k: round(v, 4) for k, v in self.standard_errors.items()},
                "intercept": round(self.intercept, 4), "r2": None if self.r2 is None else round(self.r2, 4)}


def loglog(points: list[tuple[dict[str, float], float]], names: list[str]) -> Fit | None:
    """Ordinary least squares of log(time) on log of each named variable: the exponents of time ∝ Π xᵢ^bᵢ. Needs
    more points than coefficients and at least two distinct values of every variable."""
    rows = [([1.0] + [math.log(x[n]) for n in names], math.log(y)) for x, y in points if y > 0
            and all(x[n] > 0 for n in names)]
    k = len(names) + 1
    if len(rows) <= k or any(len({r[0][i] for r in rows}) < 2 for i in range(1, k)):
        return None
    # Normal equations, solved by Gauss-Jordan elimination: k is at most 3 here.
    xtx = [[sum(r[0][i] * r[0][j] for r in rows) for j in range(k)] for i in range(k)]
    xty = [sum(r[0][i] * r[1] for r in rows) for i in range(k)]
    inverse = _invert(xtx)
    if inverse is None:
        return None
    beta = [sum(inverse[i][j] * xty[j] for j in range(k)) for i in range(k)]
    predicted = [sum(b * x for b, x in zip(beta, r[0])) for r in rows]
    ys = [r[1] for r in rows]
    mean = sum(ys) / len(ys)
    ss_res = sum((y - p) ** 2 for y, p in zip(ys, predicted))
    ss_tot = sum((y - mean) ** 2 for y in ys)
    dof = len(rows) - k
    sigma2 = ss_res / dof if dof > 0 else None
    errors = {}
    if sigma2 is not None:
        errors = {n: math.sqrt(max(0.0, sigma2 * inverse[i + 1][i + 1])) for i, n in enumerate(names)}
    return Fit(len(rows), {n: beta[i + 1] for i, n in enumerate(names)}, beta[0],
               None if ss_tot == 0 else 1 - ss_res / ss_tot, None if sigma2 is None else math.sqrt(sigma2), errors)


def _invert(matrix: list[list[float]]) -> list[list[float]] | None:
    n = len(matrix)
    work = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(matrix)]
    for column in range(n):
        pivot = max(range(column, n), key=lambda r: abs(work[r][column]))
        if abs(work[pivot][column]) < 1e-12:
            return None
        work[column], work[pivot] = work[pivot], work[column]
        scale = work[column][column]
        work[column] = [v / scale for v in work[column]]
        for r in range(n):
            if r != column and work[r][column]:
                factor = work[r][column]
                work[r] = [a - factor * b for a, b in zip(work[r], work[column])]
    return [row[n:] for row in work]
