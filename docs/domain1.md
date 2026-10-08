# Domain 1: assembly determinism, budgeting and traceability

Domain 1 of the CWA viability benchmark tests the **assembler** alone: the component that turns a frozen snapshot of
context items into the exact payload sent to a model, along with a trace of every decision it made. It sends no payload
to a model, and every pass or fail comes from code.

It tests all four published assemblers (Python, TypeScript, Go and Rust) as black boxes, through the shared adapter
protocol: snapshot bytes on stdin; a payload and trace, a rejection, or an "unsupported" exit on stdout. The harness is
[`domain1/`](../domain1/README.md).

**The result:** on the pinned specification (contract `16de4be`), all four assemblers pass every suite. The benchmark
found five defects on the way: three in the assemblers, one in a shared adapter, and one in the specification's own
tooling. All five were reported upstream and fixed, and the minimized reproducers became conformance cases.

## What it proves, and what it does not

It shows, for each assembler:

- **Determinism (R-23).** The same frozen snapshot gives the same payload bytes and the same decision. This holds
  across repeated runs, processes, environments, platforms, toolchains and all four implementations.
- **Correct decisions.** Every admission, exclusion, conflict, pipeline, fitting and refusal decision the trace records
  is the one the rules require (R-1–R-4, R-6, R-7, R-9–R-17, R-20–R-22, R-24–R-26).
- **Budgeting under pressure (R-16, R-17).** Protected content is rendered whole or the assembly is refused. Items are
  shed in tier order, and the refusal threshold is exact.
- **Refusal.** Degraded inputs produce the right refusal code and recovery action, with no payload.
- **Purity (R-18, R-23).** As far as observation can show, assembly reads nothing ambient: no network, no clock, no
  environment.

It does not show that a model does better with these payloads; Domains 2, 3 and 5 test that. It does not cover
requirements that only an application can meet, such as R-5.

## How it judges an answer

No single check is trusted alone. Every answer is judged by up to four independent oracles:

1. **Expected output.** The specification's conformance corpus, plus labeled generators. A labeled generator builds a
   snapshot from a table of intended outcomes, so every decision is known by construction. It is the only oracle that
   catches a mistake all four implementations share.
2. **Differential agreement.** The four assemblers must agree on the outcome, the payload bytes and the normalized
   trace.
3. **A trace auditor (A1–A16).** It never assembles. From the snapshot, trace and payload alone, it re-derives
   everything it can: conservation of candidates, token accounting, protected integrity, tier order, ordering, reason
   validity and precedence, conflict decisions, no synthesized text, and more.
4. **Metamorphic relations (MR1–MR14).** These are pairs of snapshots whose answers must relate in a known way. Some
   changes must leave the answer untouched: permutations, JSON respellings, equivalent timestamps, inert additions.
   Others must change it exactly as predicted: renamings that reverse id order, a raised budget or margin.

The oracles are code too, so the harness measures them on every run before trusting them (suite S0). Its independent
primitives reproduce every published value:
- 65/65 snapshot digests;
- 43/43 payload hashes and token counts;
- 43/43 published payloads, rendered byte for byte by an independent renderer.

The auditor passes every expected output and catches 1,616 of 1,626 deliberately broken traces (99.4%). Each of the 10
mutants it misses is listed, along with why it is beyond reach without assembling.

## Suites and results

These numbers come from the nightly run of 2026-10-08, against each assembler's `main`. The counts are per assembler,
and are the same for all four.

| Suite | What it does | Result |
| --- | --- | --- |
| S0 · Oracle self-check | The checks above, every run | all pass |
| S1 · Conformance replay | 65 cases and 25 rejections; each also compared with the report the assembler commits | 65/65 and 25/25, audit 65/65, 90/90 match the committed report |
| S2 · Repeatability | 98 snapshots, repeated on the host and across 15 environment cells (time zones, locales, threads, an empty environment, no HOME, a concurrent burst), on Linux with the clock shifted ±30 years, and on 7 platform and toolchain variants | 4,900/4,900 decisions identical to the host baseline |
| S4 · Metamorphic relations | 4,393 relation instances from 296 seeds | 4,393/4,393 hold |
| S5 · Generative fuzzing | 10,000 valid snapshots, steered toward unexercised behaviour, and 2,000 mutants that each break one snapshot check | 10,000/10,000 audit clean and agree, 2,000/2,000 rejected, no crash |
| S6 · Admission and pipeline | Every exclusion reason in every slot, 466 reason-precedence pairs, pipeline order | 920/920 labels matched |
| S7 · Budget pressure at scale | 10 to 10,000 candidates and 10k to 2M tokens, 8 shapes, 5 budget ratios; threshold search; budget sweeps | see below |
| S8 · Refusal and fitting | Every combination of the 6 refusal conditions, evidence degradation curves, exact fitting cases | 138/138 labels matched |
| S9 · Conflicts | Instruction and fact conflict groups; trust and verification must not change a decision | 204/204 labels matched |
| S10 · Purity | No network namespace, a read-only root, and `strace` on every syscall | 0 network syscalls, 0 file writes, 294/294 answers unchanged |
| S11 · Producer pipeline | Assembly over summarizer output (below) | 201/201 |
| S12 · Consensus goldens | 1,352 answers all four agree on, compared with adopted goldens | 1,352/1,352 match, no regressions |

All 35 reason codes (29 exclusions and 6 refusals) are exercised and pass for every assembler.

### Platforms and toolchains

S2 runs each variant in a container and compares every answer with the macOS arm64 host:

| Variant | Decisions | Payloads | Traces |
| --- | --- | --- | --- |
| Linux arm64, the default toolchains (Python 3.14, Node 24, Go 1.27, Rust 1.99) | 1,176/1,176 | 612/612 | 876/876 |
| Linux x86_64 (emulated) | 392/392 | 204/204 | 292/292 |
| Python 3.11 (the declared floor), 3.12, 3.13 | 392/392 each | 204/204 each | 292/292 each |
| Node 22 (the declared floor) | 392/392 | 204/204 | 292/292 |
| Go 1.26 (the go.mod floor) | 392/392 | 204/204 | 292/292 |
| Rust 1.80 (the declared MSRV) | 392/392 | 204/204 | 292/292 |

Payloads and traces are compared only where the reference answer has one, so a rejection counts as a decision only.
Each assembler builds and runs at the minimum toolchain it declares.

### Budget pressure at scale (S7)

These results come from a dedicated S7 run on the same day; the nightly profile leaves S7 out, since it takes about 70
minutes. Before any assembler runs, the harness renders each snapshot itself and computes the outcome every budget must
give.
The grid has 232 cells and 1,317 rows, and the four assemblers answered 4,181 times. Every answer was judged against
the prediction, for protected preservation, by the auditor and by agreement, and none failed. All four agree on every
row that at least one of them answered.

- **Refusal threshold exactness.** For each of 64 searches (shape × size × assembler), a binary search is never told
  the answer. Each one found the exact charged count below which assembly must refuse.
- **Shedding curves.** Each shape is assembled from full size down to refusal, refined to single-token boundaries:
  12,740 frames in all. The four assemblers agree frame for frame, and tier order holds on the curve itself.
- **The viability doc's case: 500k tokens into a 128k window.** Rust assembled it in 5.6 s, Go in 6.1 s and
  TypeScript in 12.1 s. Python exceeded the 30 s timeout.

Fitting re-counts the whole payload after every reduction, which the specification notes. The benchmark measured the
cost:

| In-process scaling exponent (median over shapes) | Python | TypeScript | Go | Rust |
| --- | --- | --- | --- | --- |
| No budget pressure | 0.96 | 0.64 | 0.94 | 0.96 |
| Budget at 10% of the candidates' tokens | 1.91 | 1.98 | 1.70 | 1.83 |

Without pressure, time grows linearly with the payload. Under heavy pressure it grows nearly quadratically in all four
implementations. Rust alone enforces slot caps in linear time (exponent 1.01, against 1.5–1.8 for the others).
Timeouts are performance findings, never correctness failures.

| On an Apple silicon Mac | Python | TypeScript | Go | Rust |
| --- | --- | --- | --- | --- |
| Minimal snapshot, end to end, p50 | 71.6 ms | 99.9 ms | 18.6 ms | 14.8 ms |
| Throughput, 100 × 10k at 50%, concurrency 1 → 8 (snapshots/s) | 5.9 → 38.9 | 7.6 → 44.8 | 24.1 → 76.5 | 30.1 → 98.7 |
| Peak RSS | 44 MB | 315 MB | 28 MB | 18 MB |

These absolute numbers describe one host. The exponents and the comparisons between assemblers are what carry over to
other machines.

### Summarization before freeze (S11)

R-18 forbids model calls during assembly, so summaries must be precomputed by a producer and frozen into the snapshot
as variants. S11 builds that pipeline over a synthetic corpus about fictional companies. The corpus is chunked into
24 evidence items, and each item is frozen three ways: with no variants, with deterministic stub variants, and with an
LLM's summaries. The LLM run used Qwen3.6-35B-A3B at temperature 0 with a fixed seed.

Assembly over every frozen variant set is deterministic and agreed across all four assemblers (201/201). The trace
names each selected variant's id and method. The summarizer itself is only measured:

- **Fidelity.** All 120 summaries pass the deterministic checks: no number, date, id, URL or name the source lacks;
  no new imperative or role marker; length within the band.
- **Repeat stability.** Even at temperature 0 with a fixed seed, only 76% of repeated summaries are identical.
- **Decision flips.** Freezing a different repeat changes the payload in 34 of 40 pairs, but it never changes which
  items are included.
- **Fit utility.** Across the budget sweep, evidence items survive (whole or summarized) at these rates: 44% with no
  variants, 79% with the LLM's, 89% with the stubs. The stubs include a first sentence that is usually shorter than
  the model's summary.

The committed cache of LLM responses lets this run be replayed without a model, byte for byte.

## Defects it found

Each defect was minimized automatically to a one- or two-item reproducer, reported, fixed upstream, and adopted by the
specification as a conformance case.

| Where | Defect | Found by |
| --- | --- | --- |
| Go assembler | `defaults_filled[]` omitted every candidate whose id another candidate or a producer exclusion shared | the auditor (A15) and disagreement, in fuzzing and MR1 ([assembler-go#1](https://github.com/contextwindowarchitecture/assembler-go/issues/1)) |
| Python assembler | A crash on an integer rule written with a zero fraction (`max_per_source: 1.0`), which the schema accepts | fuzzing, and MR4's integer spellings ([assembler-python#1](https://github.com/contextwindowarchitecture/assembler-python/issues/1)) |
| Rust assembler | A JSON parser that is not correctly rounded: some numbers read as a neighbouring double, giving wrong digests, wrong rejections and changed decisions | fuzzing with respelled numbers, and MR4 ([assembler-rust#1](https://github.com/contextwindowarchitecture/assembler-rust/issues/1)) |
| Python demo adapter | An unsupported-component exit (3) where a schema rejection (2) was due, because it looked up components before validating | mutation fuzzing: a schema-invalid snapshot that also named a blank tokenizer ([assembler-demo#1](https://github.com/contextwindowarchitecture/assembler-demo/issues/1)) |
| Specification tooling | The digest generator printed whole doubles above 2^53 as exact integers instead of ECMAScript's shortest digits | the independent primitives ([contextwindowarchitecture#3](https://github.com/contextwindowarchitecture/contextwindowarchitecture/issues/3)) |

The specification adopted `doubles-nearest`, `doubles-largest`, `integer-written-as-double` and `digest-beyond-2-53` as
new conformance cases, and changed `admission-reasons`.

## How far to trust it

- **The oracles are tested before they are trusted.** On every run they must reproduce the published values, and the
  auditor's kill rate is measured. Each labeled generator was checked against the reference assembler before use. On
  real answers with one decision altered, the label oracle rejects 4,052 of 4,052.
- **Planted defects are caught.** Opt-in tests plant defects in the reference assembler, and each is caught by the
  check meant for it:
  - a digest that depends on batch order;
  - a crash on fractions;
  - one extra budget token;
  - a truncated protected body;
  - a slow path;
  - a renamed variant method;
  - text rewritten in a variant;
  - an answer that varies between runs.
- **Mistakes shared by all four implementations** pass the differential oracle. Only the labels and the auditor can
  catch them, and their own tests come first.
- **Emulated x86_64** tests the binaries, not native hardware, and its Rust adapter is cross-compiled because `rustc`
  does not run under QEMU on arm64. A run on a native x86_64 runner is the stronger evidence.
- **The fidelity checks** confirm that each fact in a summary appears in its source, not that relations between facts
  survive.
- **The specification is a draft.** Every result is tied to the pinned contract commit, and goldens are re-baselined
  deliberately, never automatically.

## Reproducing it

```sh
cd domain1
uv sync
uv run cwabench setup                       # build the four adapters from sibling checkouts
uv run cwabench run                         # every suite; about 85 minutes, 70 of them S7
uv run cwabench ci nightly --fetch          # every suite but S7, against upstream main, with a drift report
```

`domain1/README.md` lists the checkouts each run needs. Every run writes a self-describing, schema-validated run
directory. Each `cwabench ci` run also writes a drift report against the previous run of the same profile: contract
or assembler bumps, changed suites and metrics, new and resolved findings, and golden drift.
