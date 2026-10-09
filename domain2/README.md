# Domain 2 harness

Benchmarks long-horizon multi-turn stability: over a long conversation, does a model given CWA-assembled context keep doing the task where a model given conventionally assembled context degrades, with everything else held equal? Its working plan, `docs/plans/domain-2-plan.md`, is kept out of git; code comments cite it by section. This directory implements phase P0 of eight:

- **Conversation scripts.** Seeded generators write whole conversations with ground truth known by construction (`cwabench2/conversations/`). Every name, figure and booking is fictional. A script fixes the governance instructions, every turn's user and assistant text, and the shards (the sentences that carry facts). A probe follows every tenth turn and the last: its question is asked after turn t with turns 1 to t as history, and it never enters the history. Each probe carries what grading needs, the turns its answer needs (for the fact-in-payload oracle) and the single-turn texts of the FULL and CONCAT controls.
  - **VT, variable tracking.** Figures assigned and reassigned ("the Harwell account's credit limit is 4,200"), among distractor chains with near-miss names and filler. Every value in a conversation is distinct, so a wrong answer is classified exactly: an earlier value of the same figure is `stale`, another figure's value a `distractor`.
  - **FR, facts revealed over turns.** A booking record (text and number fields) or a supply order (lines to total) revealed one fact per turn. A record probe asks for JSON with null for what is not known yet; an order probe asks for the running total.
- **Graders** (`cwabench2/grading/`). Deterministic, with no judge model: numbers by value with their groupings read, text by a normalized key, multiple-choice letters, and JSON records field by field. Each grade is `correct`, `stale`, `distractor`, `wrong` or `unparsed`. A reply with no answer or two answers to read is `unparsed`, never `wrong`.
- **S0, self-check.** The graders against 57 hand-planted replies. Then plants made from every probe of the run's own scripts: the expected answer written several ways, each stale and distractor value, an unrelated value, and a record with a field changed or dropped. Then every script: it validates, it regenerates byte for byte from its seed, it has the shared shape (no digit outside a shard, every shard in its turn, probes where they belong), and its text says what its ground truth says. That last check is an independent replay (`conversations/check.py`) that re-derives every probe's answer and needed turns from the text alone.

No model is called. The suites that call one (S2 to S4 and S6, from P3) will default to replaying a committed cache.

## Run it

Domain 2 runs through the benchmark's one command, `cwabench --domain 2`, which finds this package by its `cwabench.domains` entry point. Install it with `uv sync` at the benchmark root, which installs every domain into the workspace's one environment (a `uv sync` in `domain1/` installs Domain 1 alone).

```sh
uv sync                                         # at the benchmark root
uv run cwabench --domain 2 run                  # generate the conversations, run S0; writes results/d2/<run-id>/
uv run cwabench --domain 2 run --size recorded  # the families' recorded sizes instead of their pilot sizes
uv run cwabench --domain 2 validate             # re-check the latest run against its schemas and blob digests
uv run pytest                                   # the harness's own tests, from domain2/; needs the spec checkout
```

A pilot run takes a few seconds. The exit code is 0 only when the run passes and its output validates.

## What it needs

- Python 3.12+ and [uv](https://docs.astral.sh/uv/).
- Domain 1's harness at `../domain1`, a fellow member of the benchmark's uv workspace. Domain 2 reuses its run directory, blob store, output validation and contract loader. From P1 on it also reuses Domain 1's adapters, canon and oracles.
- The specification checkout at `../../../contextwindowarchitecture`, at the commit `domain2.toml` pins. That is Domain 1's pin, and a test holds the two equal. Moving it is a deliberate change, made in its own commit.

## Configuration

`domain2.toml` holds the tables this build reads: `[contract]`, `[run]` (suites, families, `size`, results directory), `[turns]` (turn counts and the probe interval), one `[families.<id>]` per family (its seed, `sizes` as conversations per turn count, and its generator's parameters) and `[findings]`. The pilot sizes give 2 conversations per turn count (10, 50 and 100 turns), 12 scripts in all. The tables the later phases read (adapters, model, arms, budgets, repeats, thresholds, caps, CI profiles) are added with those phases.

## Output

A run directory follows Domain 1's conventions under the prefix `cwa-bench-d2`. Every document names its schema (`"$schema": "cwa-bench-d2/<kind>/v1"`) and is validated against `schemas/<kind>.v1.schema.json` before it is written. The kinds Domain 2 reuses (`blob`, `contract`, `finding`, `manifest`, `run-index`, `runs-index`, `upstream`) are Domain 1's schemas with the prefix changed, and a test holds them equal. `summary` and `suite-summary` carry Domain 2's metric shape, which has `arm`, `family` and `tier` where Domain 1's has `adapter`.

```
results/d2/index.json                    # every run, newest first; results/d2/latest links the newest
results/d2/<run-id>/
  index.json  manifest.json  config.toml  contract.json  summary.json  findings.jsonl
  conversations/<family>/index.json      # the scripts this run generated: id, seed, turns, probes, script blob
  suites/S0/summary.json                 # S0's metrics and its tallies per check
  suites/S0/results.jsonl                # one self-check row per check
  blobs/sha256/<ab>/<hex>.json           # each script (kind `conversation`), content-addressed
```

A script names no run, so its blob digest is the same in every run with the same family, seed, turn count and parameters. A generator's `VERSION` moves when its output for the same inputs changes.
