# Domain 1 harness

Benchmarks CWA assemblers for assembly determinism, budgeting and traceability. What it found is written up in [../docs/domain1.md](../docs/domain1.md). Its working plan, `docs/plans/domain-1-plan.md`, is kept out of git; code comments cite it by section. This directory implements phases P0 to P7:

- **S0, oracle self-check.** The independent primitives reproduce the spec's published values, the independent renderer writes every published payload byte for byte from its trace, the trace auditor passes every expected output, and its mutation kill rate is measured.
- **S1, conformance replay.** Every case and rejection runs through every assembler and is judged three ways: against the expected output, by the auditor (A1–A16), and by four-way differential agreement.
- **S2, repeatability across environments.** Every snapshot runs repeatedly on the host and in cells that change one thing each (time zone, locale, threads, an empty environment, no HOME, the working directory, a concurrent burst), and on Linux with the wall clock shifted ±30 years. With `[s2] matrix` (as the CI profiles set it), also on Linux x86_64 (emulated on an arm64 host) and with each toolchain at the floor its assembler declares: Python 3.11–3.13, Node 22, Go 1.26, Rust 1.80. Every answer must equal the snapshot's first baseline answer.
- **S4, metamorphic relations.** MR1–MR14 turn seeds from the labeled corpora and 500 fixed fuzz seeds into pairs of snapshots whose answers must relate in a known way: permutations and JSON spellings that change nothing, equivalent timestamps, inert additions, same-count and injection-worded bodies, renamings that keep or reverse id order, a moved producer exclusion, a raised budget or margin. Each adapter is judged against its own answer on the base; MR13 (budget monotonicity) is triaged rather than failed.
- **S5, generative fuzzing.** 10,000 valid snapshots generated from a seed in rounds, steered between rounds toward coverage tags no trace has shown yet, and 2,000 mutants that each break exactly one snapshot check. Valid snapshots must assemble or refuse, audit clean and agree; mutants must be rejected; nothing may crash or hang.
- **S7, budget pressure at scale.** Eight shapes (droppable-heavy; compressible with 0, 1 or 3 variants; slot floors; slot caps; a route `fitting_order`; surfaced conflicts) from 10 to 10,000 candidates and 10k to 2M candidate tokens, at five budget ratios, one token below the protected threshold, and fixed windows; plus every tokenizer × renderer at one size. The harness renders each snapshot itself (`canon/render.py`), so it knows every budget's outcome and refusal code before any adapter runs. Each answer is judged against that prediction, for protected preservation, by the auditor and by agreement. A binary search per adapter finds the smallest budget that assembles, which must equal the computed threshold. Budget sweeps record the shedding curve frame by frame, refined to single-token boundaries, with assembly timelines. Then, serially, performance: startup, end-to-end, net and in-process times (each assembler's own timing loop, `adapters/timing/`), peak RSS, throughput, time to refusal, and log-log scaling exponents per shape.
- **S6, S8 and S9, labeled corpora.** 1,262 generated snapshots (`cwabench/corpora/labeled/`), each carrying a label that states every decision its trace must record. S6 covers admission, boundaries, the precedence of every pair of codes, and the pipeline's order. S8 covers every combination of refusal conditions, evidence degradation curves and exact fitting decisions. S9 covers conflict groups. Each answer is judged by its label, by the auditor and by agreement among the assemblers.
- **S10, purity and isolation.** Every snapshot runs with no network (a macOS sandbox, and a Linux container with no network namespace and a read-only root), once under `strace`. Any IP network syscall fails the run.
- **S11, producer pipeline and the optional LLM summarizer.** A synthetic corpus about fictional companies is chunked into evidence items and frozen three ways: with no variants (`off`), with deterministic stub variants (`stub`: lead-N, first sentence, extractive), and in `llm` or `replay` mode with an OpenAI-compatible model's summaries. Every proposed variant passes R-18's rules (shorter than its parent, a content-derived id) and deterministic fidelity checks (no number, date, id, URL or name the parent lacks; no new imperative or role marker; a length band) or is dropped. Each arm's snapshot is swept from its full size to refusal on every adapter and judged against the predicted outcome, by the auditor (A8: no synthesized text), by R-18's method check and by agreement, and repeated at four budgets. The summarizer itself is measured, never gating: repeat stability over 5 samples per chunk, the decision flip rate between samples, cache hit rate and latency, token cost, and how many evidence items each arm keeps across the sweep.
- **S12, consensus goldens.** Answers all four assemblers agree on, and the auditor passes, become candidate goldens. `cwabench goldens accept` adopts them, and later runs report drift against them.

Every failure in S4 and S5 is one finding per signature, however many snapshots show it. Its smallest reproducer is minimized by delta debugging and written to `minimized/<finding-id>/` as a conformance-case draft (`case.json`, `snapshot.json`, and the expected trace and payload when at least three adapters agree and audit clean), so a defect the benchmark finds can become a spec case.

No model is called during assembly. The only model call in the harness is S11's summarizer in `llm` mode, before any snapshot is frozen; the default (`stub`) and CI (`replay`) need no model.

## Run it

```sh
uv sync
uv run cwabench setup          # build and check the four adapters into .build/
uv run cwabench run            # run the configured suites; writes results/d1/<run-id>/
uv run cwabench validate       # re-check the latest run against its schemas and blob digests
uv run cwabench fixture <run>  # write fixtures/runs/<run-id>: the run trimmed to a sample, for consumers' tests
uv run cwabench goldens accept # adopt the latest run's consensus answers as S12's goldens
uv run cwabench run --suites S11 --summarizer llm     # call the configured model and fill summarizer-cache/
uv run cwabench run --suites S11 --summarizer replay  # the same LLM corpus from the cache alone; a miss is an error
uv run pytest                  # the harness's own tests; needs the spec checkout, no assembler
CWA_BENCH_CONTAINER=1 uv run pytest tests/test_container.py   # also build and run the real container
CWA_BENCH_REFERENCE=1 uv run pytest tests/test_labeled.py      # labels against the reference assembler
CWA_BENCH_REFERENCE=1 uv run pytest tests/test_generative.py   # S4 on the reference assembler with a planted defect
CWA_BENCH_REFERENCE=1 uv run pytest tests/test_scale.py        # S7 on the reference assembler with planted defects
CWA_BENCH_REFERENCE=1 uv run pytest tests/test_producers.py    # S11 on it with planted defects, a fake model, replay
```

A full run of all twelve suites takes about 85 minutes here. S7 takes about 70 of them: its grid about 30, since fitting slows sharply under budget pressure and large cells run up to the 30 s timeout, and the serial performance phase about 30. S5 takes 8 minutes, S4 3 and S11 (stub) under 10 seconds. `--suites` skips S7, and `[s7]` in `domain1.toml` shrinks it. `run` builds the adapters first unless you pass `--no-build`. `--adapters go,rust` and `--suites S1` select subsets. The exit code is 0 only when the run passes and its output validates.

## CI

```sh
uv run cwabench ci nightly --fetch          # every suite but S7, the full S2 matrix, S11 replayed
uv run cwabench ci weekly --fetch           # the same with S7
uv run cwabench ci nightly --checkouts DIR  # repositories already cloned into DIR (what a CI service does)
```

A profile (`[ci.profiles.*]` in `domain1.toml`) names its suites, S2's platform and toolchain matrix (`[container.variants.*]`) and S11's mode. `--fetch` keeps clones of the spec and the four assemblers under `.build/ci/checkouts`, each assembler at its origin's default branch and the spec at the pinned commit, and builds into `.build/ci/build`, so your checkouts and ordinary builds are never touched. Each run writes `ci.json`, a drift report against the previous run of the same profile: what changed underneath it (contract, assembler commits, toolchains, harness, config, host), suite and metric changes, new and resolved findings, and S12's golden drift. Its verdict is `baseline`, `unchanged`, `changed` or `regressed`. The run is published as `results/d1/<profile>` as well as `results/d1/latest`.

Nothing is scheduled. `.github/workflows/domain1-ci.yml` runs a profile on a GitHub x86_64 runner, but only when started by hand (`workflow_dispatch`); it downloads the previous run of the profile for the drift report and uploads the run as the artifact `domain1-<profile>`.

## What it needs

`domain1.toml` lists the paths below, relative to itself:

| Path | Used for |
| --- | --- |
| `../../../contextwindowarchitecture` | Spec, schemas, contract and conformance corpus. Pinned to one commit: the run stops if the checkout is at another commit or has uncommitted changes. |
| `../../../assembler-python` | `uv sync` into `.build/python-venv` |
| `../../../assembler-typescript` | Compiled from `src/` into `.build/typescript` with the checkout's own `tsc`. Needs `pnpm install` in the checkout once. |
| `../../../assembler-go` | `go build ./cmd/adapter` into `.build/bin` |
| `../../../assembler-rust` | `cargo build --example adapter` into `.build/rust-target` |

Builds write only under `.build/`. The checkouts are never modified.

S11's `llm` mode needs an OpenAI-compatible endpoint (`[summarizer] base_url`, `model`, and an API key in the environment variable `api_key_env` names, if the server wants one). `summarizer-cache/` holds every summary it produced, keyed by the parent's hash, prompt, model and request parameters; keep it with the config so `replay` can rebuild the same LLM corpus without a model.

S2 and S10 also need Podman with a running machine (`podman machine start`). The image (`container/Containerfile`) is built from the same working trees and rebuilt only when they change. Set `[container] enabled = false` to run without it: S2 then runs only its host cells and S10 only the macOS sandbox. If the container is enabled but cannot be built or run, those suites report `partial`.

## Oracles

| Module | What it is |
| --- | --- |
| `cwabench/canon/` | RFC 8785, snapshot digest, ECMAScript strings and UTF-16 ordering, RFC 3339 instants, the published tokenizers, and strict parsers for the three published renderers. Written from the spec, not from any assembler. |
| `cwabench/oracles/auditor/` | Checks A1–A16 (plan section 6), judged from the snapshot, trace and payload alone. It never assembles. |
| `cwabench/oracles/mutants.py` | 32 mutation operators S0 uses to measure the auditor's kill rate. |
| `cwabench/oracles/differential.py` | Staged comparison of the adapters' outcome, payload bytes and normalized trace. |
| `cwabench/oracles/label.py` | Judges an answer against its labeled snapshot's decisions. |
| `cwabench/oracles/metamorphic.py` | MR1–MR14: the transforms and how each relation is judged. |
| `cwabench/canon/validity.py` | The snapshot checks, independently: which one a snapshot breaks. Every published rejection breaks exactly one. |
| `cwabench/canon/render.py` | The three published renderers, from the spec's text: the payload a given set of items renders to, and its count. S0 checks it writes every published payload byte for byte. S7 uses it for the full size, the protected-only threshold and the floored one. |
| `cwabench/timelines.py` | Assembly timelines: one event stream per answer across the eight stages, from the snapshot and trace alone. |
| `cwabench/perf.py` | Timed invocations with the child's own peak RSS (`wait4`), sampling until the 95% CI is tight, log-log fits. |

| Module | What it generates |
| --- | --- |
| `cwabench/corpora/fuzz/generate.py` | Valid snapshots from intents: every admission fault in every slot, conflict groups, pipeline rules, budget pressure, refusals, edge-case pools (`pools.py`). |
| `cwabench/corpora/fuzz/steer.py` | Weights for the next round, from the coverage tags real traces showed. |
| `cwabench/corpora/fuzz/mutate.py` | 36 operators, each breaking one snapshot check or schema rule. |
| `cwabench/minimize.py` | Delta debugging over batches, candidates, rows, groups, placements, route rules, fields and bodies. |
| `cwabench/corpora/scale.py` | S7's cells: a shape, candidates, candidate tokens, tokenizer and renderer, built deterministically from a seed, with the full, protected and floored charged counts. |
| `cwabench/ci.py` | CI profiles, upstream mirrors, the drift report (`ci.json`) and publishing `results/d1/<profile>`. |
| `cwabench/producers/` | S11's producer pipeline: `source.py` (the synthetic corpus), `chunker.py`, `compressors.py` (stubs), `llm_summarizer.py` (OpenAI-compatible client), `cache.py` (content-addressed, immutable entries), `fidelity.py` (deterministic checks), `freeze.py` (R-18's variant rules and the snapshot). |
| `adapters/timing/` | In-process timing loops, one per language, built against each checkout without modifying it: Go through `go build -overlay`, Rust as a crate under `.build` with the checkout as a path dependency. |

## Output

Every file in a run directory is listed in its `index.json`, names its schema in `$schema`, and was validated before it was written. Schemas are in `schemas/`. Section 12 of the plan describes the layout. `results/d1/index.json` lists every run, the newest finished run of each CI profile (`profiles`) and the contract and adapter commits each run was made from (`commits`), so a copy of the results served without symlinks needs neither `latest` nor `<profile>` links. Adding a field to a file kind is additive (an optional property in the kind's current schema, always written from then on); removing, renaming or retyping one is a new major version with its own schema file (`cwabench/output.py`). The per-adapter `suites/S1/reports/*.conformance-report.json` files use the spec's own report format and validate against the spec's schema. S7 adds `perf/samples.jsonl` and `perf/summary.json` (every timing sample; percentiles, confidence intervals, peak RSS, scaling exponents, throughput), `suites/S7/sweeps/<snapshot-digest>.json` (budget sweep frames per adapter, each adapter's compact `curve` of counts per frame, and the shedding order item by item) and `timelines/<snapshot-digest>/<adapter>.json`. S7's snapshots reach tens of megabytes, so its rows keep each one's parameters and SHA-256 instead (`corpora/scale/index.json`); `cwabench.corpora.scale.build` regenerates the bytes. S5's summary tallies outcomes per corpus, per mutation operator and per steering round (`generated.by_round`: snapshots, passed, agreeing, outcomes per adapter), so its common views need no rows. S11 adds `summarizer/summary.json` (per arm: variants kept and dropped, fidelity per check, repeat stability, flip rate, fit utility over the sweep; the cache's hit rate, latency, tokens and invalidation), `summarizer/variants.jsonl` (every proposed variant and its checks) and `summarizer/calls.jsonl` (the provenance of every summary: model, endpoint host, parameters, prompt hash, response id, usage, latency).

`cwabench fixture <run>` writes `fixtures/runs/<run-id>`: the run trimmed to a sample of its rows (12 per file; findings whole), frames (16 per adapter in 2 sweep cells, with their timelines), corpus snapshots and golden entries, one shape's performance cells and fits, and only the blobs the kept files name. It validates like a run, its `index.json` says what it was cut from (`fixture`), and `fixtures/runs/index.json` lists the fixtures as `results/d1/index.json` lists runs, with no `latest` link. The committed fixtures are the nightly run, the dedicated S7 run and the failing run of 2026-10-08, the test data of `benchmark-ui`.

Findings say where they were reported: each carries `upstream` (the issue's URL and state, or null), copied by finding id from `findings/upstream.json` (`[findings].upstream`, schema kind `upstream`). The file is kept by hand; a finding's id is the hash of its signature, so an entry made from one run applies to every run that finds the same thing.
