# Domain 2 harness

Benchmarks long-horizon multi-turn stability: over a long conversation, does a model given CWA-assembled context keep doing the task where a model given conventionally assembled context degrades, with everything else held equal? Its working plan, `docs/plans/domain-2-plan.md`, is kept out of git; code comments cite it by section. This directory implements phases P0 to P2 of eight. No model is called yet.

- **Conversation scripts.** Seeded generators write whole conversations with ground truth known by construction (`cwabench2/conversations/`). Every name, figure and booking is fictional. A script fixes:
  - the governance instructions and the output contract;
  - every turn's user and assistant text;
  - the shards: the sentences that carry facts. Scripted assistant turns are as long as a working session's replies but never state a fact.

  A probe follows every tenth turn and the last. Its question is asked after turn t with turns 1 to t as history, and it never enters the history. Each probe carries what grading needs, the turns its answer needs (for the fact-in-payload oracle), and the single-turn texts of the FULL and CONCAT controls.
  - **VT, variable tracking.** Figures assigned and reassigned ("the Harwell account's credit limit is 4,200"), among distractor chains with near-miss names and filler. Every value in a conversation is distinct, so a wrong answer is classified exactly: an earlier value of the same figure is `stale`, another figure's value a `distractor`.
  - **FR, facts revealed over turns.** A booking record (text and number fields) or a supply order (lines to total), revealed one fact per turn. A record probe asks for JSON with null for what is not known yet; an order probe asks for the running total.
- **Graders** (`cwabench2/grading/`). Deterministic, with no judge model:
  - numbers by value, whatever their grouping;
  - text by a normalized key;
  - multiple-choice letters;
  - JSON records field by field.

  Each grade is `correct`, `stale`, `distractor`, `wrong` or `unparsed`. A reply with no answer, or with two answers, is `unparsed`, never `wrong`.
- **The application side** (`cwabench2/application/`). The harness does a CWA application's duties outside the model.
  - **History.** Every user and assistant message is a history item on a scripted clock.
  - **Oracle state writer.** Puts every fact stated so far in `state.task`, at its current value. It holds the facts, never the answer.
  - **Memory producer.** Keeps the last 10 turns verbatim and compacts older fact-carrying turns into `interaction.memory`, with a turn source and an expiry. A memory whose fact a later turn replaced is reported as `revoked`, and an old one as `expired`, instead of being emitted.

  The CWA arms are a ladder:

  | Arm | Payload |
  | --- | --- |
  | `cwa-history` | every prior turn as history |
  | `cwa-state` | plus state |
  | `cwa-memory` | plus memory |
  | `cwa-pipeline` | plus the route's supersession and exact deduplication on history |

  Their profile takes the placement of the spec's `policy-first-chat` example, checked against the pinned checkout, on a route that sets `parser: true` for the graders. A fifth CWA arm, `cwa-format`, is the format control: it takes the `window` baseline's exact selection at each budget and freezes it as a CWA snapshot, so it differs from that baseline in format alone.
- **The conventional baselines** (`cwabench2/baselines/`). Harness code that builds what a conventional application sends, from the same script, at the same points and budgets:

  | Baseline | Payload |
  | --- | --- |
  | `concat` | the system prompt and every prior turn; overflows when it does not fit |
  | `truncate` | oldest messages dropped first until it fits, the system prompt first among them |
  | `truncate-pinned` | the system prompt pinned, oldest turn messages dropped until it fits |
  | `window` | the last 10 turns, then as `truncate-pinned` |
  | `summary` | the system prompt, a rolling summary of the older turns, then the window; the summary is shed first, then the oldest window messages |

  A baseline's payload has the shape `cwa-messages/v1` gives a request (system entries, then native `user` and `assistant` messages), so every arm will be handed to the model the same way. Its count is the same tokenizer's count of every text, with the same margin. The system prompt is the instructions and the output contract. The rolling summary is, for now, Domain 1's extractive stub applied to each older user turn. The cached LLM summarizer, the strongest control, arrives with the model client at P3. An `overflow` (the messages a baseline never drops exceed the budget) reaches no model.
- **S0, self-check.**
  - The graders against 57 hand-planted replies.
  - Plants made from every probe of the run's own scripts: the expected answer written several ways, each stale and distractor value, an unrelated value, and a record with a field changed or dropped.
  - Every script: it validates, it regenerates byte for byte from its seed, it has the shared shape, and its text says what its ground truth says. The last is an independent replay (`conversations/check.py`).
  - The fact-in-payload oracle against planted omissions.
  - The baselines against a three-turn conversation counted by hand: each baseline's outcome, count, kept messages, first kept message and system prompt at three or more budgets, and two payloads byte for byte.
- **S1, the assembly gate.**
  - **What it assembles.** Every probe and every turn (a frame) of every script, in every CWA arm. Each is assembled at the absolute budgets (8k, 16k, 32k) and at 1.0, 0.5, 0.25 and 0.1 of the snapshot's full size, by each of Domain 1's four assemblers.
  - **How each answer is judged.**
    - Against the prediction. Every candidate is admissible by construction, so fitting alone decides what is kept, and the harness computes the outcome, the included items, the token count and the payload bytes exactly.
    - By Domain 1's trace auditor (A1–A16).
    - By four-way agreement.
  - **On probes**, the fact-in-payload oracle decides by the trace and by the payload's text whether the answer's facts were included, and the two must agree.
  - **Rows** record what assembly kept and shed per slot, so a conversation plays as frames.
  - **The gate.** A conversation and arm passes only when every one of its rows does. S2 to S4 will use only those that pass.
  - **Baselines.** S1 also builds every baseline at the same points, at the absolute budgets and each ratio of the conversation's full size in native chat. Each row records the payload's hash and count, what it kept and dropped, whether the system prompt and the summary survived, and on probes the fact-in-payload oracle by its record and by its text, which must agree.
- **S7, goldens.** Probe answers all four assemblers agreed on, as predicted and audited clean, become candidate goldens. `goldens accept` adopts them, and later runs report drift. `goldens/d2-goldens.json` holds the pilot's 2,240.

## Run it

The benchmark is a uv workspace, and Domain 2 runs through its one command, `cwabench --domain 2`, which finds this package by its `cwabench.domains` entry point. Run `uv sync` at the benchmark root, which installs every domain into one environment; a `uv sync` in `domain1/` installs Domain 1 alone. S1 assembles with Domain 1's adapters, so build them once with `uv run cwabench setup`.

```sh
uv sync                                         # at the benchmark root
uv run cwabench --domain 2 run                  # S0, S1, S7 at pilot size; writes results/d2/<run-id>/
uv run cwabench --domain 2 run --no-frames      # S1 assembles the probes only
uv run cwabench --domain 2 run --size recorded  # the families' recorded sizes instead of their pilot sizes
uv run cwabench --domain 2 validate             # re-check the latest run against its schemas and blob digests
uv run cwabench --domain 2 goldens accept       # adopt the latest run's candidate goldens (S7)
uv run pytest                                   # the harness's own tests, from domain2/; needs the spec checkout
CWA_BENCH_REFERENCE=1 uv run pytest tests/test_gate.py   # S1 and S7 on the reference assembler, and a planted defect
```

On this machine, a pilot run takes about 14 minutes with frames (24,640 snapshots, 98,560 answers, and 24,640 baseline payloads) and about 90 seconds with `--no-frames` (2,240 snapshots). `run` builds the adapters first unless you pass `--no-build`. The exit code is 0 only when the run passes and its output validates.

## What it needs

- Python 3.12+ and [uv](https://docs.astral.sh/uv/).
- Domain 1's harness at `../domain1`, a fellow member of the benchmark's uv workspace. Domain 2 reuses its run directory, blob store, output validation, contract loader, renderer, adapters, trace auditor and differential oracle.
- Domain 1's four adapters, built under `../domain1/.build/` by `cwabench setup`, with the assembler checkouts `../domain1/README.md` lists. `[adapters]` names Domain 1's configuration, the adapters to use, and the payload source, whose bytes S2 will send to the model (the others must match them).
- The specification checkout at `../../../contextwindowarchitecture`, at the commit `domain2.toml` pins. That is Domain 1's pin, and a test holds the two equal. Moving it is a deliberate change, made in its own commit.

## Configuration

`domain2.toml` holds these tables:

| Table | What it sets |
| --- | --- |
| `[contract]` | The pinned specification |
| `[run]` | Suites, families, `size`, the results directory, timeout, concurrency |
| `[adapters]` | Domain 1's configuration, the adapters to use, the payload source |
| `[arms]` | The CWA arms and the baselines |
| `[baselines]` | The window's length, the summarizer, the stub's extractive ratio |
| `[budgets]` | Absolute budgets and ratios, reserved output, margin, tokenizer, renderer |
| `[application]` | The scripted clock, the history window, memory's lifetime |
| `[turns]` | Turn counts and the probe interval |
| `[families.<id>]` | Each family's seed, `sizes` (conversations per turn count) and its generator's parameters |
| `[s1]` | Whether to assemble every turn as a frame, and whether to write timelines |
| `[s7]` | The goldens file |
| `[findings]` | Where findings were reported upstream |

The pilot sizes give 2 conversations per turn count (10, 50 and 100 turns), 12 scripts in all. The tables the later phases read (model, repeats, thresholds, caps, CI profiles) are added with those phases.

## Output

A run directory follows Domain 1's conventions under the prefix `cwa-bench-d2`. Every document names its schema (`"$schema": "cwa-bench-d2/<kind>/v1"`) and is validated against `schemas/<kind>.v1.schema.json` before it is written.

- **Reused kinds.** `blob`, `contract`, `drift-row`, `finding`, `manifest`, `run-index`, `runs-index`, `timeline` and `upstream` are Domain 1's schemas with the prefix changed, and a test holds them equal.
- **New kinds.** `conversation`, `conversation-index`, `self-check`, `turn-row`, `baseline-row` and `goldens` are Domain 2's own.
- **Changed kinds.** `summary` and `suite-summary` carry Domain 2's metric shape, which has `arm`, `family` and `tier` where Domain 1's has `adapter`.

```
results/d2/index.json                    # every run, newest first; results/d2/latest links the newest
results/d2/<run-id>/
  index.json  manifest.json  config.toml  contract.json  summary.json  findings.jsonl
  conversations/<family>/index.json      # the scripts this run generated: id, seed, turns, probes, script blob
  suites/S0/summary.json  suites/S0/results.jsonl     # one self-check row per check
  suites/S1/summary.json                 # the gate's metrics, which conversations and arms passed, row counts
  suites/S1/turns.jsonl                  # one row per point, arm and budget: every answer, the prediction,
                                         #   the shedding record, and on probes the fact-in-payload oracle
  suites/S1/baselines.jsonl              # one row per point, baseline and budget: count, kept and dropped,
                                         #   the system prompt and summary, and on probes the fact oracle
  suites/S7/candidate-goldens.json  suites/S7/drift.jsonl  suites/S7/summary.json
  timelines/<snapshot sha256>/<adapter>.json   # each conversation's last probe, per arm and budget
  blobs/sha256/<ab>/<hex>.json           # scripts, each probe's frozen snapshot, findings' reproducers
```

A script names no run, so its blob digest is the same in every run with the same family, seed, turn count and parameters. A generator's `VERSION` moves when its output for the same inputs changes.

A probe's frozen snapshot is stored once per arm, its `budget.input` set to its full size. A row's exact bytes set `budget.input` to the row's `budget_input`, and `snapshot_sha256` is their digest. A frame's snapshot is not stored: it is rebuilt from the script, arm and settings. At pilot size a run with frames is about 91 MB, 58 MB of it the turn rows and 13 MB the baseline rows, so a recorded-size run is best made with `--no-frames`.

Ratio budgets are relative to each CWA ladder arm's own full size, and for the baselines and `cwa-format` to the conversation's full size in native chat. The memory arms keep a 10-turn window and are much smaller, so compare arms at the absolute budgets.
