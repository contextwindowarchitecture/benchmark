# Domain 2 harness

Benchmarks long-horizon multi-turn stability: over a long conversation, does a model given CWA-assembled context keep doing the task where a model given conventionally assembled context degrades, with everything else held equal? Its working plan, `docs/plans/domain-2-plan.md`, is kept out of git; code comments cite it by section. This directory implements phases P0 to P6 of eight. S2 and S3 send every probe to a model, and the model-based producers run before them, all by default replaying the committed cache of the model's replies, so a run needs no model. S4, the long-context suite, and S6, the model in the loop, run when named (`--suites`) until their replies are cached.

- **Conversation scripts.** Seeded generators write whole conversations with ground truth known by construction (`cwabench2/conversations/`). Every name, figure and booking is fictional. A script fixes:
  - the governance instructions and the output contract;
  - every turn's user and assistant text;
  - the shards: the sentences that carry facts. Scripted assistant turns are as long as a working session's replies but never state a fact.

  A probe follows every tenth turn and the last. Its question is asked after turn t with turns 1 to t as history, and it never enters the history. Each probe carries what grading needs, the turns its answer needs (for the fact-in-payload oracle), and the single-turn texts of the FULL and CONCAT controls.
  - **VT, variable tracking.** Figures assigned and reassigned ("the Harwell account's credit limit is 4,200"), among distractor chains with near-miss names and filler. Every value in a conversation is distinct, so a wrong answer is classified exactly: an earlier value of the same figure is `stale`, another figure's value a `distractor`.
  - **FR, facts revealed over turns.** A booking record (text and number fields) or a supply order (lines to total), revealed one fact per turn. A record probe asks for JSON with null for what is not known yet; an order probe asks for the running total, worked step by step and ending with a `Total:` line.
  - **CC, corrections.** VT's figures, each stated once and later corrected by the user, with the retracted value named beside the new one ("is 4,350, not 4,200"). Only corrections change a figure, so every stale answer is a value the user took back.
  - **IP, instruction persistence.** A VT conversation whose instructions carry one rule: figures in square brackets, a closing "Kestrel desk", or capitals. Every answer is graded twice, for the figure and for the rule (`grading/compliance.py`).
- **LQ corpora, long-context questions** (`conversations/longcontext.py`). Not a conversation: a corpus of fictional site documents and questions about it, for S4.
  - **The corpus.** Each document describes one site ("the Harwell depot") and states four of six attributes, each in a sentence that names the site, among filler. Documents are added until the corpus reaches its tier, `ratio` × `[s4].budget` tokens (0.25×, 1×, 4× and 16× of 8,192), and chunked by Domain 1's chunker at sentence boundaries.
  - **The questions**, one of each kind and format by default: `single` (one site's attribute; a same-named twin site states it with another value, so reading the wrong document gives a `distractor`), `multi` (which of two sites in different documents has the larger value; the answer needs both chunks) and `none` (an attribute the site's document never states, answered NOT FOUND; its twin states one), each as a short answer or a four-option multiple choice whose last option is "The documents do not say".
  - **The retriever** (`application/retrieval.py`): BM25 over the chunks, relevance being a chunk's share of the best chunk's score. Every retrieval arm gets the same `[s4].candidates` chunks. Questions name their site, so retrieval finds the answer's chunk near the top; what differs between the retrieval arms is assembly and format, not retrieval.
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

  - **The model-based producers** (`application/producers.py`), run before any snapshot is frozen, through the same cache as S2:
    - the **extractor**, which after each user turn reads the task (the conversation's instructions), the state so far and the new message, and returns only what the message adds or changes; the application merges it into its state;
    - the **rolling summarizer**, which updates a summary, told the task and a word limit, as each turn leaves the window. It is the `summary` baseline's summary, the strongest conventional control.

  The CWA arms are a ladder:

  | Arm | Payload |
  | --- | --- |
  | `cwa-history` | every prior turn as history |
  | `cwa-state` | plus state, from the oracle state writer |
  | `cwa-state-x` | plus state from a model-based extractor instead: the realistic arm |
  | `cwa-memory` | plus memory |
  | `cwa-pipeline` | plus the route's supersession and exact deduplication on history |

  Their profile takes the placement of the spec's `policy-first-chat` example, checked against the pinned checkout, on a route that sets `parser: true` for the graders. A fifth CWA arm, `cwa-format`, is the format control: it takes the `window` baseline's exact selection at each budget and freezes it as a CWA snapshot, so it differs from that baseline in format alone.

  The LQ family has two CWA arms (`application/evidence.py`), on the rules of the spec's `long-context-qa` route for evidence (`min_relevance` 0.2, at least one chunk, exact deduplication, at most 8 chunks a document) with the retriever's candidates as `evidence.knowledge` items. `cwa-reinforced` takes the spec's one profile for that route, `long-context-reinforced`, which places the instructions twice, the second time just before the question; `cwa-evidence` is that placement with the repeat removed, so the two differ in the reinforcement alone.
- **The conventional baselines** (`cwabench2/baselines/`). Harness code that builds what a conventional application sends, from the same script, at the same points and budgets:

  | Baseline | Payload |
  | --- | --- |
  | `concat` | the system prompt and every prior turn; overflows when it does not fit |
  | `truncate` | oldest messages dropped first until it fits, the system prompt first among them |
  | `truncate-pinned` | the system prompt pinned, oldest turn messages dropped until it fits |
  | `window` | the last 10 turns, then as `truncate-pinned` |
  | `summary` | the system prompt, a rolling summary of the older turns, then the window; the summary is shed first, then the oldest window messages |

  The LQ family has three (`baselines/longcontext.py`): `control-full`, every chunk of the corpus with no budget, an `overflow` when it exceeds the model's context; `truncate-pinned`, the system prompt pinned and the earliest chunks dropped until it fits; and `rag`, the retriever's candidates, the lowest-ranked dropped until it fits, best first, with no relevance threshold.

  A baseline's payload has the shape `cwa-messages/v1` gives a request (system entries, then native `user` and `assistant` messages), so every arm will be handed to the model the same way. Its count is the same tokenizer's count of every text, with the same margin. The system prompt is the instructions and the output contract. The rolling summary is the model's (`[baselines].summarizer = "llm"`), or Domain 1's extractive stub applied to each older user turn (`stub`). An `overflow` (the messages a baseline never drops exceed the budget) reaches no model.
- **S0, self-check.**
  - The graders against 74 hand-planted replies, NOT FOUND and site names among them.
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
  - **LQ** (`suites/s1_longcontext.py`), when S4 runs: every question's snapshot in each LQ CWA arm, at `[s4].budget`, gated the same way against an exact prediction of what assembly keeps (admission, deduplication, the per-document cap, then the longest top-ranked run of evidence that fits, or an `evidence_required` refusal), and every LQ baseline built and recorded with the fact oracle. A corpus and arm passes only when every question does.
- **The model** (`cwabench2/model/`). Every arm's payload is handed to the model the same way: the system entries' texts, joined by a blank line, as one system message, then the payload's messages unchanged. Nothing is added, so the model reads what the payload's count describes.
  - **The request:** the client's body (model, messages, temperature, seed, max_tokens, the server's extra fields), hashed over RFC 8785. A change to the payload, the model or any parameter is another request.
  - **The cache** (`model-cache/`, committed): Domain 1's content-addressed cache, keyed by the request's hash and the sample index. Each entry keeps the reply and its provenance: model, endpoint, parameters, response id, finish reason, usage, cached prefix tokens, latency.
  - **Modes.** `replay` (the default) answers from the cache alone, and a miss is an error. `llm` calls the endpoint on a miss and fills the cache. Nothing else in Domain 2 calls a model.
- **S2, scripted conversations.** Every probe of every script is sent to the model in every arm, at each of `[s2].tiers`, `[s2].repeats` times at temperature 0, and each reply is graded.
  - **CWA arms** send only what passed S1's gate: the payload whose hash equals the payload source's answer.
  - **Baselines** send what S1 built; an `overflow` grades as `overflow` and reaches no model.
  - **The two controls,** `control-full` (the fully specified task) and `control-concat` (the fact sentences so far, as one message), carry the system prompt and no budget.

  A grade row holds nothing that depends on the mode (no latency, no cache hit), so a `replay` run writes the same grades as the `llm` run that filled the cache. Measured per arm and tier, never gated:
  - aptitude (the rate of correct answers), with a cluster-bootstrap interval over conversations;
  - the stale and unparsed rates;
  - accuracy with the needed facts in the payload and without;
  - results by family and by turn count;
  - on IP conversations, instruction persistence: the share of answers that follow the rule, by rule and turn count;
  - each arm's paired difference from `[s2].reference` (`truncate-pinned`) on the same probes and samples, with its interval.

  What fails the suite is the harness: a payload unequal to its gated bytes, or a call that failed or missed the cache.
- **S3, unreliability.** S2's payloads at `[s3].tiers`, asked `[s3].repeats` times at `[s3].temperature` (5 at 0.7). Per arm and tier it reports the study's aptitude (A90, the 90th percentile of a probe's sample scores) and unreliability (U90−10, the 90th minus the 10th), in points and averaged over probes, with cluster-bootstrap intervals.
- **S4, long context** (`suites/s4_longcontext.py`). Every LQ question in every arm of `[s4].arms`, `[s4].repeats` times at the model's temperature, one stream per corpus so a server's prefix cache answers the shared documents. CWA arms send only gated payloads; an assembly refusal grades `refused`, and `control-full`'s overflow grades `overflow`; neither reaches the model. Measured per arm and corpus tier, never gated: accuracy with a cluster-bootstrap interval over corpora, by question kind and format, with the needed chunks in the payload and without, the distractor rate, the rate of abstaining (NOT FOUND, or the option saying so) on a question the documents answer, and each arm's paired difference from `[s4].reference` (`rag`).
- **S6, model in the loop** (`suites/s6_loop.py`). The study's setup: the model's reply at each turn becomes the history of the next. A chain is one conversation of `[s6].families` (VT by default) in one arm of `[s6].arms`, for one sample; it walks the conversation turn by turn, sending the arm's payload with the user's turn as the query and its own earlier replies as the assistant turns, and asks each probe with that history (probes still never enter it). Samples fork: with `[model].seed_per_sample` each sends its own seed, which S6 requires. The application side reads only the user's turns, so the extractor's state is shared; each `summary` chain has its own rolling summary of its own replies. Every CWA payload is gated inline as S1 gates a snapshot, on every adapter, before it is sent; a gate failure, a refusal or an overflow halts the chain. Measured: S2's metrics per arm, the reply length by turn (the study's answer bloat), and, when S2 or S3 ran in the same run, each arm's aptitude against the scripted result on the same probes (S3's when it ran at S6's temperature).
- **S5, cost and latency,** from S2's records and, when it ran, S4's: prompt and completion tokens per answer and per correct answer, latency p50 and p95 (as recorded when the cache was filled), answer length by turn count, prefix-cache tokens, and the token estimator's under-count by prompt size. It fails on any call whose server prompt exceeds its budget (R-16), which is what the margin must prevent. Text the model wrote is under-counted most: up to 15.8% in the P4 pilot, and up to 22.5% for the extractor's state in one conversation at the recorded size. A margin of m covers an under-count u when m ≥ u ÷ (1 − u), so 22.5% needs 29%, and `[budgets].margin_percent` is 30; S5's summary gives the margin each prompt size needs.
- **S7, goldens.** Probe answers all four assemblers agreed on, as predicted and audited clean, become candidate goldens. `goldens accept` adopts them, and later runs report drift. `goldens/d2-goldens.json` holds the pilot's 6,048.

## Run it

The benchmark is a uv workspace, and Domain 2 runs through its one command, `cwabench --domain 2`, which finds this package by its `cwabench.domains` entry point. Run `uv sync` at the benchmark root, which installs every domain into one environment; a `uv sync` in `domain1/` installs Domain 1 alone. S1 assembles with Domain 1's adapters, so build them once with `uv run cwabench setup`.

```sh
uv sync                                         # at the benchmark root
uv run cwabench --domain 2 run                  # S0, S1, S2, S5, S7 at pilot size, replaying the model's cache
uv run cwabench --domain 2 run --model llm      # call the endpoint on a cache miss and fill model-cache/
uv run cwabench --domain 2 run --no-frames      # S1 assembles the probes only
uv run cwabench --domain 2 run --concurrency 32 # model calls in flight, overriding [model].concurrency
uv run cwabench --domain 2 run --base-url URL   # the endpoint, overriding [model].base_url
uv run cwabench --domain 2 run --size recorded  # the families' recorded sizes instead of their pilot sizes
uv run cwabench --domain 2 run --suites S0,S1,S4 --model llm   # the LQ family: S1 gates it, S4 asks it
uv run cwabench --domain 2 run --suites S0,S1,S2,S3,S6 --model llm   # the model in the loop, compared with S3
uv run cwabench --domain 2 run --workers 16      # snapshots assembled at once, overriding [run].concurrency
uv run cwabench --domain 2 validate             # re-check the latest run against its schemas and blob digests
uv run cwabench --domain 2 goldens accept       # adopt the latest run's candidate goldens (S7)
uv run pytest                                   # the harness's own tests, from domain2/; needs the spec checkout
CWA_BENCH_REFERENCE=1 uv run pytest tests/test_gate.py   # S1, S4, S6 and S7 on the reference assembler, a defect
```

A pilot run with `--no-frames` replays its model calls in a few minutes, most of it S1's assembly; with frames S1 takes much longer. The recorded run is made with `--model llm --size recorded` on one GPU host that runs vLLM and the harness together (`scripts/pod-setup.sh`), so it survives the machine that started it; an interrupted or partly failed `--model llm` run is resumed by running it again, which fills just what is missing. Only `--model llm` calls a model. `--concurrency` changes only how many calls are in flight. A run writes the same grades and producer rows at any concurrency, since the requests and cache keys do not depend on it and the rows are written in the plan's order. Each conversation's extractor and summarizer are separate chains, so a server that batches well (vLLM on a GPU) can take a concurrency well above the number of conversations. In `llm` mode a server can still answer differently under different loads, which the cache records; S2's and S3's `summary.json` record the concurrency and, in `llm` mode, what the endpoint lists under the model's name in `/models` (`model.server`). `run` builds the adapters first unless you pass `--no-build`. The exit code is 0 only when the run passes and its output validates.

## What it needs

- Python 3.12+ and [uv](https://docs.astral.sh/uv/).
- Domain 1's harness at `../domain1`, a fellow member of the benchmark's uv workspace. Domain 2 reuses its run directory, blob store, output validation, contract loader, renderer, adapters, trace auditor and differential oracle.
- Domain 1's four adapters, built under `../domain1/.build/` by `cwabench setup`, with the assembler checkouts `../domain1/README.md` lists. `[adapters]` names Domain 1's configuration, the adapters to use, and the payload source, whose bytes S2 will send to the model (the others must match them).
- For a run in `replay` mode, nothing more once `model-cache/` holds the run's replies (it is committed with the recorded run). For `--model llm`, an OpenAI-compatible endpoint at `[model].base_url` serving `[model].model` (the key, if any, in the environment variable `[model].api_key_env`).
- The specification checkout at `../../../contextwindowarchitecture`, at the commit `domain2.toml` pins. That is Domain 1's pin, and a test holds the two equal. Moving it is a deliberate change, made in its own commit.

## Configuration

`domain2.toml` holds these tables:

| Table | What it sets |
| --- | --- |
| `[contract]` | The pinned specification |
| `[run]` | Suites, families, `size`, the results directory, timeout, concurrency |
| `[adapters]` | Domain 1's configuration, the adapters to use, the payload source |
| `[arms]` | The CWA arms and the baselines |
| `[baselines]` | The window's length, the summarizer (`llm` or `stub`), the summary's word limit, the stub's extractive ratio |
| `[budgets]` | Absolute budgets and ratios, reserved output, margin, tokenizer, renderer |
| `[application]` | The scripted clock, the history window, memory's lifetime |
| `[turns]` | Turn counts and the probe interval |
| `[families.<id>]` | Each family's seed, `sizes` (conversations per turn count) and its generator's parameters; for `lq`, corpora per ratio and the chunk size |
| `[s1]` | Whether to assemble every turn as a frame, and whether to write timelines |
| `[model]` | The mode, the endpoint and model, the request parameters, concurrency, the cache, the context limit, the producers' token limit, and whether each sample sends its own seed (`seed_per_sample`) |
| `[s2]` | The tiers S2 sends at, samples per payload, the reference arm, the bootstrap |
| `[s3]` | The same for S3, and its sampling temperature |
| `[s4]` | S4's arms, the budget, the corpus ratios, the retriever's candidates, samples per payload, the reference arm, the bootstrap |
| `[s6]` | S6's families, arms, budget tier, chains per conversation and arm (`repeats`), temperature, the reference arm, the bootstrap |
| `[s7]` | The goldens file |
| `[findings]` | Where findings were reported upstream |

`domain2.toml` is the recorded run's: vLLM 0.31.0 serving `Qwen/Qwen3.6-35B-A3B-FP8` beside the harness (its launch command is in the file), every sampling parameter stated in the request, the producers' token limit at 2,048, and `seed_per_sample`, so a sampled suite's samples (S3, S6) are independent draws: with one seed for every sample, a server that honours seeds would answer every sample of a request alike. Add `--base-url` when the server is not beside the harness, for example a RunPod pod through its proxy (`https://<pod>-8000.proxy.runpod.net/v1`), with its key in `CWA_BENCH_MODEL_KEY`. The P3 and P4 pilots ran against a local omlx server (`Qwen3.6-35B-A3B-8bit`, MLX); their configuration and cache replay at commit `12cbe73`.

A server-specific configuration of your own, such as one for a single pod, can be named `<name>.local.toml`: git ignores it, and the run still keeps a copy as `config.toml`.

The pilot sizes give 2 conversations per turn count (10, 50 and 100 turns) for VT, FR and CC, and 3 for IP (one per rule): 27 scripts in all. The LQ pilot is 2 corpora per ratio, 8 in all, with 6 questions each; the 16× corpus is about 132,000 tokens. The tables the later phases read (thresholds, caps, CI profiles) are added with those phases.

## Output

A run directory follows Domain 1's conventions under the prefix `cwa-bench-d2`. Every document names its schema (`"$schema": "cwa-bench-d2/<kind>/v1"`) and is validated against `schemas/<kind>.v1.schema.json` before it is written.

- **Reused kinds.** `blob`, `contract`, `drift-row`, `finding`, `manifest`, `run-index`, `runs-index`, `timeline` and `upstream` are Domain 1's schemas with the prefix changed, and a test holds them equal.
- **New kinds.** `conversation`, `conversation-index`, `self-check`, `turn-row`, `baseline-row`, `grade-row`, `call-row`, `producer-row`, `goldens`, for LQ `lq-corpus`, `lq-index`, `lq-gate-row`, `lq-baseline-row` and `lq-grade-row`, and for S6 `chain-row`, are Domain 2's own.
- **Changed kinds.** `summary` and `suite-summary` carry Domain 2's metric shape, which has `arm`, `family` and `tier` where Domain 1's has `adapter`, and an optional `interval`.

```
results/d2/index.json                    # every run, newest first; results/d2/latest links the newest
results/d2/<run-id>/
  index.json  manifest.json  config.toml  contract.json  summary.json  findings.jsonl
  conversations/<family>/index.json      # the scripts this run generated: id, seed, turns, probes, script blob
  conversations/lq/index.json            # with S4: the LQ corpora, each a corpus blob
  suites/S0/summary.json  suites/S0/results.jsonl     # one self-check row per check
  suites/S1/summary.json                 # the gate's metrics, which conversations and arms passed, row counts
  suites/S1/turns.jsonl                  # one row per point, arm and budget: every answer, the prediction,
                                         #   the shedding record, and on probes the fact-in-payload oracle
  suites/S1/baselines.jsonl              # one row per point, baseline and budget: count, kept and dropped,
                                         #   the system prompt and summary, and on probes the fact oracle
  suites/S1/lq.jsonl  suites/S1/lq-baselines.jsonl   # with S4: the LQ gate rows and baseline rows
  suites/S2/grades.jsonl                 # one row per probe, arm, tier and sample: the reply and its grade
  suites/S2/summary.json                 # aptitude, intervals, paired differences, by arm and tier; the model,
                                         #   its mode, cache, concurrency and, in llm mode, the server's /models entry
  suites/S5/summary.json                 # tokens, latency and the estimator's under-count, by arm and tier
  model/calls.jsonl                      # S2: one row per distinct request and sample, cache key and provenance
  model/s3-calls.jsonl                   # the same for S3
  model/s4-calls.jsonl                   # and for S4
  suites/S4/grades.jsonl  suites/S4/summary.json      # S4's grades, accuracy by arm and corpus tier
  suites/S6/turns.jsonl                  # one row per chain and turn: the payload, the reply that became history
  suites/S6/grades.jsonl  suites/S6/summary.json      # S6's grades; aptitude by arm, against the scripted result
  model/s6-calls.jsonl                   # every S6 call, the chains' summarizer included
  producers/calls.jsonl                  # one row per producer call: the extractor's or summarizer's output
  suites/S3/grades.jsonl  suites/S3/summary.json      # S3's grades, A90 and U90−10 by arm and tier
  suites/S7/candidate-goldens.json  suites/S7/drift.jsonl  suites/S7/summary.json
  timelines/<snapshot sha256>/<adapter>.json   # each conversation's last probe, per arm and budget
  blobs/sha256/<ab>/<hex>.json           # scripts, each probe's frozen snapshot, findings' reproducers
```

A script names no run, so its blob digest is the same in every run with the same family, seed, turn count and parameters. A generator's `VERSION` moves when its output for the same inputs changes.

A probe's frozen snapshot is stored once per arm, its `budget.input` set to its full size. A row's exact bytes set `budget.input` to the row's `budget_input`, and `snapshot_sha256` is their digest. A frame's snapshot is not stored: it is rebuilt from the script, arm and settings. At pilot size a run with frames is about 91 MB, 58 MB of it the turn rows and 13 MB the baseline rows, so a recorded-size run is best made with `--no-frames`.

Ratio budgets are relative to each CWA ladder arm's own full size, and for the baselines and `cwa-format` to the conversation's full size in native chat. The memory arms keep a 10-turn window and are much smaller, so compare arms at the absolute budgets.
