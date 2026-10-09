"""S3 · Unreliability (domain-2-plan.md, 8 and 9): S2's payloads asked again at a sampling temperature.

Every probe, in every arm of `[s3].arms` at each of `[s3].tiers`, is asked `[s3].repeats` times at
`[s3].temperature` (the same payloads S2 sends, through S2's plan; the temperature makes them other requests, so other
cache entries). Each reply is graded as in S2 (`suites/S3/grades.jsonl`).

The study's measures ("LLMs Get Lost in Multi-Turn Conversation"), per probe over its K samples, then averaged over
probes, are reported per arm and tier, in points (0 to 100):

- **aptitude**, the 90th percentile of a probe's sample scores;
- **unreliability**, the 90th minus the 10th percentile: how far apart the same conversation's best and worst runs
  are. A probe answered alike in every sample scores 0, whether right or wrong.

Percentiles are linear between order statistics. Each mean comes with a cluster-bootstrap interval over conversations.
A probe's score is its grade's score, so a JSON record scores the share of its fields that are right. Like S2,
nothing here gates but the harness: every payload equal to its gated bytes, and every call answered.
"""
from __future__ import annotations

from collections import defaultdict

from .. import metrics, stats
from . import SuiteContext, SuiteResult
from .s2_scripted import execute

ID = "S3"
TITLE = "Unreliability"


def percentile(values: list[float], p: float) -> float:
    """The p-th percentile, linear between order statistics (numpy's default)."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * p / 100
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def spread(grades: list[dict]) -> dict[tuple[str, str], list[tuple[str, float, float]]]:
    """Per arm and tier: per probe, (conversation, A90, U90−10) in points. An overflow scores 0 in every sample."""
    by_probe = defaultdict(list)
    for row in grades:
        by_probe[(row["arm"], row["tier"], row["conversation"], row["probe_id"])].append(100 * row["score"])
    out = defaultdict(list)
    for (arm, tier, conversation, _), points in by_probe.items():
        out[(arm, tier)].append((conversation, percentile(points, 90), percentile(points, 90) - percentile(points, 10)))
    return out


def run(ctx: SuiteContext) -> SuiteResult:
    config, settings = ctx.config, ctx.config.s3
    model_settings = {**config.model, "temperature": settings["temperature"]}
    resamples, seed = settings["bootstrap_resamples"], settings["bootstrap_seed"]

    def unreliability(grades: list[dict]) -> list[dict]:
        found = []
        for (arm, tier), probes in sorted(spread(grades).items()):
            for name, label, index in (("aptitude_p90", "Aptitude (A90), points", 1),
                                       ("unreliability", "Unreliability (U90−10), points", 2)):
                clusters = defaultdict(list)
                for probe in probes:
                    clusters[probe[0]].append(probe[index])
                value = stats.mean([p[index] for p in probes])
                metric = metrics.value(f"s3.{name}", label, None if value is None else round(value, 3), "points",
                                       suite=ID, arm=arm, tier=tier,
                                       description=f"mean over {len(probes)} probes of their samples' percentiles")
                metric["interval"] = stats.bootstrap(clusters, resamples, seed)
                found.append(metric)
        return found

    return execute(ctx, ID, TITLE, settings, model_settings, "model/s3-calls.jsonl", unreliability)[0]
