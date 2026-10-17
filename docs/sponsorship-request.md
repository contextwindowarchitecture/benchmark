# Asking for GPU time to finish the CWA viability benchmark

**From:** Melvin Hillsman, author and maintainer of the Context Window Architecture specification and its benchmark
**Contact:** ceo@mrhillsman.com · [contextwindowarchitecture.io](https://contextwindowarchitecture.io)
**Date:** 2026-10-12

> *Cover note to [sponsor]: [Two or three sentences on why this provider: the serving stacks you run, the hardware you
> want measured independently, the program this request fits.]*

## What I'm asking for

I'm asking for about six months of dedicated GPU time to finish an open benchmark. It measures what a deterministic
context assembler is worth to a production language-model system: whether a model keeps doing its job over a long
conversation, what serving it costs, and how much prompt injection gets through. Two of its five domains are done. I
ran them on one cloud host with a single GPU, and that machine has taken the work as far as it can. What's left needs
more GPUs, and for longer.

In numbers: about 9,750 GPU-hours on H100- or H200-class hardware, in three shapes. One GPU for the whole period, a
two-GPU instance for a month, and an eight-GPU node for three one-week windows. Plus one CPU-only machine and about
2 TB of storage. If that's more than you can give, a minimum of about 5,750 GPU-hours still covers the two remaining
domains with the most decision-relevant results, serving economics and injection resistance, and a second model for the
long-horizon domain that's already finished. The table under "The request, in detail" breaks it down.

Everything the compute produces goes out under an open license: the harness, every run, the model caches that let
anyone replay a run without a GPU, and a write-up per domain that says what was shown and how far to trust it.

## Why I built this

Production AI systems fail for a reason that has little to do with model quality. Every call to a model is a payload
assembled from instructions, retrieved documents, conversation history, tool output and the user's request. Today that
assembly is string concatenation, done differently in every codebase, and three expensive failures follow from it.

Long conversations degrade. A published Microsoft study found that models lose an average of 39% of their single-turn
accuracy once a task unfolds over several turns, and become 112% less reliable from run to run.

Serving money is wasted. Inference engines skip the expensive part of a request when its prefix is byte-identical to a
recent one. A single field that changes at the head of the prompt, a timestamp say, has been measured to take the
cache hit rate from 98.7% to 0.7%. Every turn is then billed at full price.

Untrusted text gets authority. Instructions buried in a retrieved document or a tool result are read with the same
weight as the developer's own, and the model acts on them.

Context Window Architecture (CWA) is my answer to that: an open, Apache-2.0 specification that treats payload assembly
as engineering rather than prose. Every item carries typed metadata: where it came from, how much to trust it, how
fresh it is, what it may cost in tokens. A conforming assembler admits, orders, fits and renders those items
deterministically, without calling a model, and leaves a trace of every decision it made. There are implementations in
Python, TypeScript, Go and Rust, and the specification lives at contextwindowarchitecture.io.

But a specification is a claim. I wrote the benchmark to test that claim with evidence that would survive a sceptical
reviewer: does structured assembly actually change accuracy, cost and security, by how much, and under what conditions?
Where the answer turns out to be no, I want that published too.

## What the benchmark is

Five domains, each with its own harness, its own pass/fail oracles and its own write-up:

| Domain | The question | Needs a model |
| --- | --- | --- |
| 1. Determinism and budgeting | Does the assembler do exactly what the specification says, every time, on every platform? | No |
| 2. Long-horizon stability | Over 10 to 100 turns, does a model with CWA-assembled context keep doing the task where conventionally assembled context degrades? | Yes |
| 3. Agentic context engineering | Does a CWA harness lift an open-weight model's score on an agentic retrieval benchmark (Context-Bench)? | Yes, a frontier-class open model |
| 4. Serving economics | How much prefix-cache reuse, latency reduction and throughput does CWA's stable-prefix layout deliver on real inference engines (vLLM, SGLang)? | Yes, on dedicated GPUs |
| 5. Instruction hierarchy and injection | Does keeping untrusted evidence structurally separate from governance reduce indirect prompt injection to near zero without loss of benign utility? | Yes |

A few rules hold across all five. Pass or fail is decided by code, never by a judge model. Every model reply is cached
and committed, so any run can be replayed byte for byte without a model. Every result is a statistical estimate with a
confidence interval, compared against fair conventional baselines on the same inputs. And every result is pinned to one
commit of the specification.

## What I've already done, without a sponsor

Both finished domains ran on one AWS EC2 host: 16 vCPUs, 128 GB of memory, one NVIDIA RTX PRO 6000 with 96 GB. The
model was served by vLLM on the same machine as the harness, so every reported result comes from one box.

**Domain 1 is complete and published.** All four assemblers pass every suite on the pinned specification: conformance
replay; repeatability across 14 environment cells and 7 platform and toolchain variants; 4,393 metamorphic relations,
10,000 fuzzed snapshots and 2,000 mutants; budget pressure from 10 to 10,000 candidates and up to 2 million tokens; and
purity under a syscall tracer. Along the way the benchmark found five defects, three in assemblers, one in an adapter
and one in the specification's own tooling. All five were reported, fixed upstream and turned into conformance cases.
On the design's headline stress case, 500,000 candidate tokens squeezed into a 128,000-token window, Rust assembled in
8.4 seconds, Go in 14.3 and TypeScript in 26.6; Python went past the 30-second limit. Under heavy budget pressure every
implementation's time grows nearly quadratically, which the benchmark measured and reported rather than glossed over.

**Domain 2 is complete and published.** The harness generates long conversations whose ground truth is known by
construction: variables reassigned across turns, facts revealed one per turn, corrections, instructions that must
persist, and long documents. It assembles every turn through all four assemblers, builds conventional baselines from
the same script, sends every arm to the same model with the same settings, and grades deterministically. The recorded
run covers 243 conversations of 10 to 100 turns with one 35B mixture-of-experts model, and every reply is committed, so
the run replays without a GPU. Here is accuracy over 3,888 probe answers per arm at an 8,192-token budget, with 95%
intervals:

| Arm | Accuracy | Against pinned truncation |
| --- | --- | --- |
| The whole conversation, no budget (the upper bound) | 0.995 | |
| CWA with state and memory written from what the user said | 0.989 | +0.166 [+0.134, +0.201] |
| Rolling summary written by the same model | 0.862 | +0.039 [+0.010, +0.069] |
| Pinned truncation (the fair conventional baseline) | 0.823 | |
| CWA with state the same model writes | 0.821 | −0.002 [−0.033, +0.028] |
| CWA with history alone | 0.797 | −0.026 [−0.045, −0.007] |
| Sliding window of 10 turns | 0.528 | −0.295 [−0.329, −0.261] |

The finding is more specific than "CWA helps". The gain comes from the state the application keeps, not from the
assembler arranging history. With accurate state, CWA holds 0.97 to 0.99 across 100-turn conversations where
truncation falls to 0.76. With state the model writes for itself, it is level with truncation. Over long documents,
CWA's evidence handling matched conventional retrieval (717 of 720 against 719) once evidence ids were sorted by rank,
which exposed a rendering-order trap in the specification itself; I've drafted a change proposal for it. The assembly
was exact throughout: 220,608 assemblies, every one equal to what the harness predicted.

A results site that reads every run is live at benchmark.contextwindowarchitecture.io, with both domains in full.

## What your GPUs would unlock

Each remaining piece needs more than a single-GPU host, for its own reason.

**Domain 2 needs a second model.** Every result so far is for one model, and I want to be able to say the findings
aren't one model's quirk. The second family is a 70B-class dense model: at 8-bit it fits on one GPU, at full precision
(about 140 GB of weights) it needs two. And the model-in-the-loop suite, which calls the model at every turn of a
chain, has only ever run at pilot size. The compute produces the same recorded results for the second family, the
model-in-the-loop comparison at recorded size, and an extended write-up.

**Domain 3 needs a big model.** The design names GLM-4.6, a 355-billion-parameter open model that takes an eight-GPU
node to serve. The question is whether a CWA harness closes the gap between it and closed models on Context-Bench, and
what each point of lift costs.

**Domain 4 needs quiet, dedicated hardware.** Its question is physical: time to first token, cache hit rate and tokens
per second on vLLM and SGLang. Those numbers are only trustworthy on unshared GPUs, at one, two and eight of them. The
output is prefix stability, latency reduction and throughput gain for CWA layouts against conventional ones, by engine
and model size, and a cost model built on measured numbers rather than assumptions.

**Domain 5 needs volume.** The injection benchmark (BIPIA) is tens of thousands of adversarial prompts, run against two
models with repeats. The output is attack success rate against benign utility, per attack vector.

A CPU-only machine takes the rest. It runs Domains 1 and 2's CI, which replays every committed reply without a model,
and carries the assembler side of every other domain, so the GPUs stay free for model work.

## The request, in detail

| Resource | Configuration | Duration | GPU-hours | Covers |
| --- | --- | --- | --- | --- |
| A. One dedicated GPU | 1 × H100 80 GB or H200 141 GB; 200+ GB RAM; 1 TB local disk | 6 months, continuous | 4,400 | Domain 2's second model at 8-bit and its model-in-the-loop suite at recorded size; Domain 5; Domain 4's single-GPU configurations; development for Domain 3 |
| B. A two-GPU instance | 2 × H100 or H200, NVLink | 4 weeks | 1,350 | Domain 2's second model at full precision; Domain 4's tensor-parallel pair; Domain 5's second model |
| C. An eight-GPU node | 8 × H100 or H200, NVLink | 3 windows of 1 week | 4,000 | Domain 3's recorded runs with a GLM-4.6-class model; Domain 4's large-model configuration |
| D. One CPU machine | 16+ cores x86_64; 64 GB RAM | 6 months | n/a | Domains 1 and 2's CI; the assembler side of every domain |
| Storage | 2 TB object or block storage | 6 months | n/a | Model weights, caches, run directories |

That's about 9,750 GPU-hours in total. **If you can only do part of it**, A, B and D together, about 5,750 GPU-hours,
finish Domain 2's second model, all of Domain 5, and Domain 4 at one and two GPUs. Domain 3 and the eight-GPU
measurements would wait.

To put a price on it: at the on-demand rates specialist GPU clouds publish for this class of hardware, roughly $2 to
$4 per GPU-hour, the full request is worth about $25,000 to $40,000 and the minimum about $15,000 to $23,000. At
hyperscaler list prices, about three times that. If your hardware is a different class, B200 or MI300X for example, the
shapes carry over directly and the hours shrink.

What I'm not asking for: no training or fine-tuning, no proprietary data, no sustained egress (results are megabytes,
and the model caches go into git), and nothing beyond the period.

If you run a different inference engine, or hardware that isn't NVIDIA, the harness doesn't mind: it talks to any
OpenAI-compatible server. Appendix B says what changes and what doesn't.

## What you get

- **Your name on the results.** Every result the sponsored compute produces, Domain 2's second model and Domains 3 to
  5, credits you as the benchmark's compute sponsor, in the write-ups and on the results site.
- **Domain 4's numbers, measured on your platform, by name.** The serving-economics domain measures real engines on
  real hardware, and its published numbers state the hardware and stack they came from. That's an independent,
  reproducible measurement of prefix-cache economics on your platform.
- **Evidence your own people can check.** Every run directory and model cache is published, so your engineers can
  replay any result without a GPU and see exactly what was measured.
- **Early access** to results as each domain completes, before publication.
- **A result the prefix-caching stacks can cite.** CWA's stable-prefix layout exists to make prefix caching pay.
  Measured proof that it does is something those stacks can point to.

## How much to trust the numbers

I built the benchmark to be doubted. Its graders are code, tested against planted wrong answers before every run. Its
baselines are the fair ones, pinned truncation, a sliding window, a rolling summary written by the same model, never
only the naive one. An ablation ladder separates effects, so a gain is attributed to format, selection, state or
memory rather than to "CWA" as a whole. Every failure is attributed: was the needed fact in the payload at all, and if
so, did the model miss it? Results are confidence intervals from paired comparisons on the same conversations, with
thresholds declared before a recorded run. The specification is still a draft, so every result names the commit it
was measured against, and each write-up says what its domain does not prove. Domain 2's write-up reports where CWA
didn't help, with state the model writes itself and with history alone, as plainly as where it did.

## When things would happen

| Months from access | Milestone |
| --- | --- |
| 1 | Domain 2: the second model family and the model-in-the-loop suite at recorded size; write-up extended |
| 2 to 3 | Domain 4: vLLM and SGLang at one and two GPUs; first eight-GPU window |
| 3 to 4 | Domain 5: injection suite on both models; write-up published |
| 4 to 5 | Domain 3: Context-Bench harness; two eight-GPU windows for recorded runs; write-up published |
| 6 | Domain 4 write-up; results site complete; every run verified replayable |

## Next step

A 30-minute call would do it. We'd match the request to the hardware and program you actually have, I'd adjust the
configuration above to fit, and we'd fix the schedule.

Melvin Hillsman · ceo@mrhillsman.com · contextwindowarchitecture.io

## Appendix A: how I sized the hours

The numbers come from measured runs, not from a model of GPU performance alone.

**Domain 2.** Its recorded run (243 conversations, every suite but the model-in-the-loop one, from an empty cache)
took 3 hours 5 minutes on one 96 GB GPU serving a 35B mixture-of-experts model with 3B active parameters at 8-bit,
with 128 requests in flight; the model-in-the-loop suite took 57 minutes at pilot size; the committed cache holds
136,779 replies. A 70B-class dense model has about 20 times the active parameters, so I estimate a recorded run at 50
to 100 GPU-hours on an H100, and the model-in-the-loop suite at recorded size at about ten times its pilot. A recorded
run gets repeated several times before it's final (the first model's was remade after each of its harness fixes, then
once more from an empty cache), so the estimate allows six per configuration.

**Domain 3.** Context-Bench's token volume is modest; its published per-model cost is tens of dollars of API tokens.
The cost is holding an eight-GPU node up while a 355B-parameter model is served. Development happens on a smaller
model on resource A, and the node is reserved for recorded runs only.

**Domain 4.** Latency and cache measurements need a quiet, dedicated machine, warmed and measured per configuration:
two engines, three model sizes, one, two and eight GPUs, several hundred replayed sessions each. Single-GPU work runs
on resource A; the pair and the node are reserved for their own configurations.

**Domain 5.** Short adversarial prompts in volume with repeats, on two models. It fits within resource A, with resource
B for the larger model.

**Rates.** Specialist-cloud on-demand H100 and H200 rates of about $2 to $4 per GPU-hour, and hyperscaler list rates
of about $7 to $12, as published in mid-2026 price surveys ([one](https://jarvislabs.ai/blog/h100-price),
[two](https://www.spheron.network/blog/h100-price-per-hour-2026/),
[three](https://www.gmicloud.ai/en/blog/h200-gpu-cloud-rental-guide)).

## Appendix B: other inference engines and hardware

The reported runs so far used vLLM on one NVIDIA RTX PRO 6000. The harness talks to any OpenAI-compatible server, so a
sponsor or community member with another serving stack or other hardware can rerun the report on it. A run on another
engine or GPU is published as its own arm beside the vLLM-on-NVIDIA results, never merged into them: different kernels
and number formats can change a model's reply even at temperature 0, so each configuration keeps its own committed
model cache and names the engine and hardware it ran on. Domain 1 calls no model and is unaffected; in Domain 2, the
suites that send payloads to the model are the ones that would differ.

| Engine | Fit | Notes |
| --- | --- | --- |
| SGLang | The best second engine | Serves the same FP8 checkpoint on the same class of GPU, with prefix caching, per-request seeds and a deterministic-inference mode; already one of Domain 4's two engines |
| llama.cpp | A separate quantized arm | Needs GGUF weights rather than the FP8 checkpoint, so its results mix the engine's effect with the quantization's; useful for a claim about laptop-class serving |
| TensorRT-LLM, LMDeploy | Possible | OpenAI-compatible servers; little added over SGLang for these measurements |
| Ollama, MLX | Not for reported results | Ollama wraps llama.cpp with less control; MLX runs on Apple hardware, not on a GPU host like the one the reported runs share |

vLLM also runs on AMD GPUs through ROCm, with the same server and flags. AMD has no 96 GB counterpart to the RTX PRO
6000:

| AMD GPU | Memory | Fit for Domain 2's model (a 35B mixture-of-experts at FP8, 262,144-token context) |
| --- | --- | --- |
| Instinct MI300X | 192 GB | The closest match in practice: one GPU holds the same checkpoint and the full context under stock vLLM, though it is a tier above the RTX PRO 6000 in memory and bandwidth. It uses AMD's FNUZ variant of FP8, so vLLM converts the weights, a small numerical difference of its own |
| Instinct MI325X, MI350X, MI355X | 256 to 288 GB | Also fit on one GPU; the MI350 series uses the same FP8 format as NVIDIA |
| Radeon AI PRO R9700 | 32 GB | The FP8 weights do not fit on one card; two cards run this model only on an experimental community fork of vLLM |
| Radeon PRO W7900 | 48 GB | No hardware FP8, and the 16-bit weights do not fit |

A single MI300X was listed at about $2 to $3.60 per GPU-hour by specialist clouds in 2026
([one](https://www.spheron.network/gpu-rental/mi300x/), [two](https://deploybase.ai/articles/amd-mi300x-price)); some
providers sell it only as eight-GPU nodes, the shape of resource C.
