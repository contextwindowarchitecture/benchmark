# Request for compute sponsorship: the CWA viability benchmark

**From:** Melvin Hillsman, author and maintainer of the Context Window Architecture specification and its benchmark
**Contact:** ceo@mrhillsman.com · [contextwindowarchitecture.io](https://contextwindowarchitecture.io)
**Date:** 2026-10-12

> *Cover note to [sponsor]: [Two or three sentences on why this provider: the serving stacks you run, the hardware you
> want measured independently, the program this request fits.]*

## The ask

Dedicated GPU capacity, over about six months, to finish an open benchmark that measures what a deterministic context
assembler is worth to a production language-model system: in accuracy over long conversations, in serving cost, and in
resistance to prompt injection. The work so far, two of the five domains, ran on one cloud host with a single GPU.
What remains needs more GPUs than that, and for longer.

The request is about 9,750 GPU-hours on H100- or H200-class hardware in three shapes (one GPU for the whole period, a
two-GPU instance for a month, and an eight-GPU node for three one-week windows), plus one CPU-only machine and about
2 TB of storage. A minimum configuration of about 5,750 GPU-hours covers serving economics and injection resistance,
the two remaining domains with the most decision-relevant results, and a second model for the completed long-horizon
domain. Section 5 has the table.

Everything produced is published under an open license: the harness, every run, the model caches that let anyone
replay a run without a GPU, and a write-up per domain that states what was proven and how far to trust it.

## 1. Why this matters

Production AI systems fail for a reason that has little to do with model quality. Every call to a model is a payload
assembled from instructions, retrieved documents, conversation history, tool output and the user's request. Today that
assembly is ad hoc string concatenation, and three costly failures follow from it:

- **Long conversations degrade.** A published Microsoft study found that models lose an average of 39% of their
  single-turn accuracy once a task unfolds over several turns, and become 112% less reliable from run to run.
- **Serving cost is wasted.** Inference engines skip the expensive part of a request when its prefix is byte-identical
  to a recent one. A single field that changes at the head of the prompt, such as a timestamp, has been measured to
  take the cache hit rate from 98.7% to 0.7%, so every turn is billed at full price.
- **Untrusted text gets authority.** Instructions embedded in a retrieved document or a tool result are read with the
  same weight as the developer's own, and the model acts on them.

Context Window Architecture (CWA) is an open, Apache-2.0 specification that treats payload assembly as engineering
rather than prose. Every item carries typed metadata: where it came from, how trusted it is, how fresh it is, what it
may cost in tokens. A conforming assembler admits, orders, fits and renders those items deterministically, without
calling a model, and emits a trace of every decision it made. Implementations exist in four languages (Python,
TypeScript, Go and Rust), and the specification is published at contextwindowarchitecture.io.

A specification is a claim. The benchmark exists to test it with evidence that would survive a sceptical review: does
structured assembly actually change accuracy, cost and security, by how much, and under what conditions?

## 2. What the benchmark is

Five domains, each with its own harness, its own pass/fail oracles and its own write-up:

| Domain | Question | Needs a model |
| --- | --- | --- |
| 1. Determinism and budgeting | Does the assembler do exactly what the specification says, every time, on every platform? | No |
| 2. Long-horizon stability | Over 10 to 100 turns, does a model with CWA-assembled context keep doing the task where conventionally assembled context degrades? | Yes |
| 3. Agentic context engineering | Does a CWA harness lift an open-weight model's score on an agentic retrieval benchmark (Context-Bench)? | Yes, a frontier-class open model |
| 4. Serving economics | How much prefix-cache reuse, latency reduction and throughput does CWA's stable-prefix layout deliver on real inference engines (vLLM, SGLang)? | Yes, on dedicated GPUs |
| 5. Instruction hierarchy and injection | Does keeping untrusted evidence structurally separate from governance reduce indirect prompt injection to near zero without loss of benign utility? | Yes |

Design principles that hold across all five: pass or fail is decided by code, never by a judge model; every model
reply is cached and committed, so any run can be replayed byte for byte without a model; every result is a statistical
estimate with a confidence interval, compared against fair conventional baselines on the same inputs; and every result
is pinned to one commit of the specification.

## 3. What is already done, without sponsorship

Both completed domains were run on one AWS EC2 host (16 vCPUs, 128 GB, one NVIDIA RTX PRO 6000 with 96 GB), with the
model served by vLLM beside the harness, so every reported result comes from one machine.

**Domain 1 is complete and published.** All four assemblers pass every suite on the pinned specification: conformance
replay, repeatability across 14 environment cells and 7 platform and toolchain variants, 4,393 metamorphic relations,
10,000 fuzzed snapshots and 2,000 mutants, budget pressure from 10 to 10,000 candidates and up to 2 million tokens, and
purity under a syscall tracer. The benchmark found five defects on the way (three in assemblers, one in an adapter, one
in the specification's own tooling); all were reported, fixed upstream, and adopted as conformance cases. On the
design's headline stress case, 500,000 candidate tokens into a 128,000-token window, Rust assembled in 8.4 seconds, Go
in 14.3 and TypeScript in 26.6; Python exceeded the 30-second limit. Under heavy budget pressure every implementation's
time grows nearly quadratically, a cost the benchmark measured and reported.

**Domain 2 is complete and published.** The harness generates long conversations with ground truth known by
construction (variables reassigned across turns, facts revealed one per turn, corrections, instructions that must
persist, and long documents), assembles every turn through all four assemblers, builds conventional baselines from the
same script, sends every arm to the same model with the same settings, and grades deterministically. The recorded run
covers 243 conversations of 10 to 100 turns with one 35B mixture-of-experts model, and every reply is committed so the
run replays without a GPU. Accuracy over 3,888 probe answers per arm at an 8,192-token budget, with 95% intervals:

| Arm | Accuracy | Against pinned truncation |
| --- | --- | --- |
| The whole conversation, no budget (the upper bound) | 0.995 | |
| CWA with state and memory written from what the user said | 0.989 | +0.166 [+0.134, +0.201] |
| Rolling summary written by the same model | 0.862 | +0.039 [+0.010, +0.069] |
| Pinned truncation (the fair conventional baseline) | 0.823 | |
| CWA with state the same model writes | 0.821 | −0.002 [−0.033, +0.028] |
| CWA with history alone | 0.797 | −0.026 [−0.045, −0.007] |
| Sliding window of 10 turns | 0.528 | −0.295 [−0.329, −0.261] |

The finding is specific: CWA's gain comes from the state the application keeps, not from the assembler arranging
history. With accurate state, CWA holds 0.97 to 0.99 over 100-turn conversations where truncation falls to 0.76; with
state the model writes itself, it is level with truncation. Over long documents, CWA's evidence handling matched
conventional retrieval (717 of 720 against 719) once evidence ids sorted by rank, which exposed a rendering-order trap
in the specification; a change proposal for it has been drafted. The assembly itself was exact throughout: 220,608
assemblies, every one equal to the harness's prediction.

A results site that reads every run is live at benchmark.contextwindowarchitecture.io, with both domains in full.

## 4. What the sponsorship unlocks, and why it needs GPUs

| Domain | Why it needs more than one GPU host | What the sponsored compute produces |
| --- | --- | --- |
| 2 | Every result so far is for one model. The second model family is a 70B-class dense model, at 8-bit on one GPU and at full precision (about 140 GB of weights) on two, and the model-in-the-loop suite, which calls the model at every turn of a chain, has run only at pilot size. | The same recorded results for a second model family, so the findings are shown not to be one model's; the model-in-the-loop comparison at recorded size; the write-up extended |
| 3 | The design names GLM-4.6, a 355-billion-parameter open model that needs an eight-GPU node to serve. The test is whether a CWA harness closes the gap between it and closed models. | The open-weight capability lift, measured on Context-Bench, with cost per point |
| 4 | The question is physical: time to first token, cache hit rate and tokens per second on vLLM and SGLang. The measurements are trustworthy only on dedicated, unshared GPUs, at one, two and eight GPUs. | Prefix stability, latency reduction and throughput gain for CWA layouts against conventional ones, by engine and model size; a cost model built on measured numbers |
| 5 | The injection benchmark (BIPIA) is tens of thousands of adversarial prompts, run against two models with repeats. | Attack success rate against benign utility, per attack vector |

A CPU-only machine runs Domains 1 and 2's CI, which replays every committed reply without a model, and carries the
assembler side of every other domain, so the GPUs are kept for model work.

## 5. The request

| Resource | Configuration | Duration | GPU-hours | Covers |
| --- | --- | --- | --- | --- |
| A. One dedicated GPU | 1 × H100 80 GB or H200 141 GB; 200+ GB RAM; 1 TB local disk | 6 months, continuous | 4,400 | Domain 2's second model at 8-bit and its model-in-the-loop suite at recorded size; Domain 5; Domain 4's single-GPU configurations; development for Domain 3 |
| B. A two-GPU instance | 2 × H100 or H200, NVLink | 4 weeks | 1,350 | Domain 2's second model at full precision; Domain 4's tensor-parallel pair; Domain 5's second model |
| C. An eight-GPU node | 8 × H100 or H200, NVLink | 3 windows of 1 week | 4,000 | Domain 3's recorded runs with a GLM-4.6-class model; Domain 4's large-model configuration |
| D. One CPU machine | 16+ cores x86_64; 64 GB RAM | 6 months | n/a | Domains 1 and 2's CI; the assembler side of every domain |
| Storage | 2 TB object or block storage | 6 months | n/a | Model weights, caches, run directories |

Total: about 9,750 GPU-hours. **Minimum configuration:** A, B and D, about 5,750 GPU-hours, which completes Domain 2's
second model, Domain 5 in full and Domain 4 at one and two GPUs, and defers Domain 3 and the eight-GPU measurements.

At the on-demand rates specialist GPU clouds publish for this class of hardware, roughly $2 to $4 per GPU-hour, the
full request is worth about $25,000 to $40,000 and the minimum about $15,000 to $23,000; at hyperscaler list prices,
about three times that. If the sponsor's hardware is a different class (B200 or MI300X, for example), the shapes
transfer directly and the hours shrink.

What is not being asked for: no training or fine-tuning, no proprietary data, no sustained egress (results are
megabytes, and the model caches are committed to git), and no commitment beyond the period.

## 6. What the sponsor receives

- **Attribution** on every result the sponsored compute produces (Domain 2's second model, Domains 3 to 5), in the
  write-ups and on the results site, as the compute sponsor of the benchmark.
- **Domain 4's results, by name, on the sponsor's platform.** The serving-economics domain measures real engines on
  real hardware, and its published numbers will state the hardware and stack they were measured on. It is a
  reproducible, independent measurement of prefix-cache economics on that platform.
- **Replayable evidence.** Every run directory and model cache is published, so the sponsor's own engineers can replay
  any result without a GPU and verify what was measured.
- **Early access** to results as each domain completes, ahead of publication.
- **Evidence for the serving stacks that implement prefix caching.** CWA's stable-prefix layout exists to make prefix
  caching pay; measured proof that it does is a result those stacks can cite.

## 7. How far the results can be trusted

The benchmark is built to be doubted. Its graders are code, tested against planted wrong answers before every run. Its
baselines are the fair ones (pinned truncation, a sliding window, a rolling summary written by the same model), never
only the naive one. It separates effects with an ablation ladder, so a gain is attributed to format, selection, state
or memory rather than to "CWA". Every failure is attributed: was the needed fact in the payload at all, and if so, did
the model miss it? Results are confidence intervals from paired comparisons on the same conversations, with thresholds
declared before a recorded run. And the specification is a draft: every result names the commit it was measured
against, and each write-up says what its domain does not prove. Domain 2's write-up reports where CWA did not help
(state the model writes itself, history alone) as plainly as where it did.

## 8. Proposed schedule

| Months from access | Milestone |
| --- | --- |
| 1 | Domain 2: the second model family and the model-in-the-loop suite at recorded size; write-up extended |
| 2 to 3 | Domain 4: vLLM and SGLang at one and two GPUs; first eight-GPU window |
| 3 to 4 | Domain 5: injection suite on both models; write-up published |
| 4 to 5 | Domain 3: Context-Bench harness; two eight-GPU windows for recorded runs; write-up published |
| 6 | Domain 4 write-up; results site complete; every run verified replayable |

## 9. Next step

A 30-minute call to match the request to the sponsor's available hardware and program, after which the configuration
in section 5 is adjusted and the schedule fixed.

Melvin Hillsman · ceo@mrhillsman.com · contextwindowarchitecture.io

## Appendix: how the hours were sized

The numbers are extrapolated from measured runs, not from a model of GPU performance alone.

- **Domain 2.** Its recorded run (243 conversations, every suite but the model-in-the-loop one, from an empty cache)
  took 3 hours 5 minutes on one 96 GB GPU serving a 35B mixture-of-experts model with 3B active parameters at 8-bit,
  128 requests in flight, and the model-in-the-loop suite took 57 minutes at pilot size; the committed cache holds
  136,779 replies. A 70B-class dense model has about 20 times the active parameters, so a recorded run is estimated at
  50 to 100 GPU-hours on an H100, and the model-in-the-loop suite at recorded size at about ten times its pilot. A
  recorded run is repeated several times before it is final (the first model's was remade after each of its harness
  fixes, then once more from an empty cache); the estimate allows six per configuration.
- **Domain 3.** Context-Bench's token volume is modest (its published per-model cost is tens of dollars of API tokens),
  so the cost is holding an eight-GPU node up while a 355B-parameter model is served. Development happens on a smaller
  model on resource A; the node is reserved only for recorded runs.
- **Domain 4.** Latency and cache measurements need a quiet, dedicated machine, warmed and measured per configuration:
  two engines, three model sizes, one, two and eight GPUs, several hundred replayed sessions each. Single-GPU work runs
  on resource A; the pair and the node are reserved for their configurations.
- **Domain 5.** Short adversarial prompts in volume with repeats, on two models; fits within resource A, with resource B
  for the larger model.
- **Rates.** Specialist-cloud on-demand H100 and H200 rates of about $2 to $4 per GPU-hour, and hyperscaler list rates
  of about $7 to $12, as published in mid-2026 price surveys ([one](https://jarvislabs.ai/blog/h100-price),
  [two](https://www.spheron.network/blog/h100-price-per-hour-2026/),
  [three](https://www.gmicloud.ai/en/blog/h200-gpu-cloud-rental-guide)).
