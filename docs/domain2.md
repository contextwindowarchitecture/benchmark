# Domain 2: long-horizon multi-turn stability

Domain 2 of the CWA viability benchmark is the first domain that sends payloads to a model. It asks one question:
**over a long multi-turn task, does a model given CWA-assembled context keep doing the task where a model given
conventionally assembled context degrades, with everything else held equal?** The model, its settings, the facts, the
user's turns, the budget, the token counter and the grader are the same in every arm; only the way the payload is
built changes. The harness is [`domain2/`](../domain2/README.md), built on Domain 1's.

**The result.** On the pinned specification (contract `16de4be`) and one model, the answer depends on who writes the
application's state:

- **With state the application writes from what the user actually said**, CWA holds accuracy at 0.97 to 0.99 over
  conversations of up to 100 turns at an 8,192-token budget. The fair conventional baseline (front truncation with
  the system prompt pinned) gets 0.82 and a rolling summary 0.86: CWA is 0.15 to 0.17 ahead, paired on the same
  probes.
- **With state the same model writes as the conversation goes**, CWA is level with pinned truncation (−0.002, 95%
  interval −0.033 to +0.028) and does not reach the rolling summary.
- **History alone in CWA's format** gains nothing: 0.026 below pinned truncation. CWA's format by itself does not
  change accuracy, but it does raise compliance with a rule the model otherwise ignores.
- **Over long documents**, CWA's evidence handling matches conventional retrieval (717/720 correct, against 719/720)
  once the application gives its evidence ids that sort by rank. With ids in document order it trailed by up to 0.19,
  because the specification renders a slot's items in id order.

So the gain comes from the state the application keeps, not from the assembler arranging history: CWA gives that
state a protected place that every budget keeps, and the model uses it. How good the state is decides the result.
The assembly itself was exact throughout: 220,608 assemblies on four assemblers, every one equal to the prediction and
clean under Domain 1's audit.

## What it proves, and what it does not

For the tasks, model and budgets it runs, as estimates with confidence intervals, it measures:

- **Aptitude retention**: accuracy at checkpoints every 10 turns over 10-, 50- and 100-turn conversations, against
  the full conversation as the upper bound.
- **Variable tracking**: whether the model answers with a figure's current value, and how often with a stale one.
- **Instruction persistence**: whether a rule given in the system prompt still holds at turn 100.
- **Unreliability**: how much repeated samples of the same conversation vary, per arm.
- **Long-context question answering** over corpora from a quarter of the budget to 16 times it.
- **Where each failure came from**: for every probe, whether the fact the answer needed was in the payload at all, so a
  wrong answer is attributed to the payload or to the model.

It does not show that CWA improves a model's reasoning: the model is held constant. It does not give a universal
effect size: the viability document's figures (a 39% drop, a 112% rise in unreliability) come from another setup, and
this domain reports its own numbers against its own baselines. Tools (Domain 3), prefix caching (Domain 4) and
injection (Domain 5) are left to their domains. With a model in the loop, every result is a statistical estimate.

The viability document describes CWA with features the specification does not have (temporal decay, an assembler
that writes state, compression during assembly). This domain tests the specification as written: the application
writes state and memory before assembly, the assembler admits, fits and renders, and summaries are precomputed.

## How it is set up

**Tasks.** Five seeded families, four of conversations and one of documents, all fictional and synthetic and
generated with their ground truth, so nothing is contaminated and every answer is known by construction:

| Family | What the user does | What a probe asks |
| --- | --- | --- |
| VT · Variable tracking | States figures, reassigns some, mentions near-miss figures with similar names, among small talk | a figure's current value |
| FR · Facts revealed | Reveals a record's fields, or an order's lines and fees, one per turn | the whole record, or the order's total |
| CC · Corrections | States figures, then corrects them, up to twice | a figure's corrected value |
| IP · Instruction persistence | VT, with one rule in the system prompt: brackets around figures, a sign-off, or capital letters | the figure, and the rule checked by code |
| LQ · Long context | (S4) a corpus of fictional site documents, chunked, with one question of each kind | a single fact, a comparison of two sites, or a fact the documents never state |

The recorded run has 20 conversations of each family per turn count (21 for IP, 7 per rule), 243 in all, with a probe
every 10 turns and after the last: 3,888 probe answers per arm and budget over 3 samples. LQ has 10 corpora at each of
four sizes.

**Arms.** Conventional baselines built by the harness, and CWA arms assembled by the four published assemblers:

| Arm | Payload |
| --- | --- |
| `control-full`, `control-concat` | the whole conversation with no budget; every fact in one turn: upper bounds |
| `concat` | every turn as native chat; when that exceeds the budget, nothing is sent (an overflow) |
| `truncate`, `truncate-pinned` | the newest turns that fit, the system prompt dropped or pinned; `truncate-pinned` is the fair reference |
| `window` | the last 10 turns |
| `summary` | the last 10 turns and a rolling summary the model writes as turns leave the window: the strongest conventional control |
| `cwa-history` | CWA with every turn as `interaction.history` |
| `cwa-state` | plus `state.task` written from the facts the user stated: the upper bound for state |
| `cwa-state-x` | plus `state.task` written by the model, turn by turn, from the user's messages: the realistic arm |
| `cwa-memory`, `cwa-pipeline` | `cwa-state` with the last 10 turns as history, older facts as `interaction.memory`, and for the second the route's supersession and deduplication |
| `cwa-format` | the `window` baseline's exact selection, rendered as CWA renders it: the format control |

`cwa-state`, `cwa-memory` and `cwa-pipeline` take their state and memory from the facts the user stated, so they show
what CWA gives an application whose state is right. `cwa-state-x` shows what it gives one whose state comes from a
model. The CWA arms use the specification's example chat profile and route policies, `cwa-messages/v1` and
`estimate-utf8/v1`, with a 35% margin.

**Model.** `Qwen/Qwen3.6-35B-A3B-FP8` on vLLM 0.31.0, in the official vLLM image that `scripts/pod-setup.sh` pins by
digest, on one 96 GB GPU beside the harness; thinking off, every sampling parameter stated in the request, temperature
0 with seed 7 for S2 and S4, and 0.7 with a seed per sample for S3 and S6. Every request and reply (136,779 of them) is
in the committed cache, keyed by the request's hash and the sample, so every result replays without a model.

**Budgets.** 8,192 and 16,384 input tokens for S2, 8,192 for S3, S4 and S6: well below the model's 262,144, so budget
pressure is real and cheap.

## How it judges an answer

No single check is trusted alone:

1. **Ground truth by construction**, and checked independently: S0 re-derives every script's answers from its text
   alone, without the generator, for all 243 scripts.
2. **Deterministic graders**: normalized exact match, multiple-choice letters, a marked total, and a record parsed as
   JSON. A stale value, a near-miss distractor and an unparsable reply are their own verdicts, never just "wrong".
3. **The fact-in-payload oracle**: from the trace (CWA) or the baseline's own record, and again from the payload's
   text, whether every fact the answer needs was sent. The two readings must agree: 94,632/94,632 CWA probes and
   78,980/78,980 baseline probes did.
4. **The assembly gate** (S1): Domain 1's oracles on every CWA payload before it is sent: all four assemblers agree
   byte for byte, the trace passes the audit, and the answer equals the harness's own prediction.
5. **A self-check before any result is trusted** (S0): 23,520 planted probe answers graded as intended, 74 planted
   replies, 1,575 planted omissions read correctly by the fact oracle, 18 baselines equal to hand-computed payloads,
   and 13 planted replies judged right against the IP rules.

## Results

From the recorded run of 2026-10-12 (`20261012T052348Z-4a0ea5d`: S0 to S5 and S7 at recorded size, 3 hours 5
minutes) and, for S6, a pilot-size run made just before it (`20261012T042632Z-4a0ea5d`, 57 minutes). Both ran on one
AWS EC2 g7e.4xlarge (16 vCPUs, 128 GB, an NVIDIA RTX PRO 6000 with 96 GB) with vLLM beside the harness, 128 requests
in flight, and both started from an empty model cache, so every reply in the committed cache was filled that day from
that server. Every result replays byte for byte from the cache.

### The assembly gate (S1)

All 1,578 conversation-and-arm pairs passed. 55,152 CWA snapshots (54,432 conversation probes and 720 LQ questions)
were assembled by all four assemblers: 220,608 answers, every one equal to the prediction and clean under the audit,
with no crash, timeout or disagreement. The model was only ever sent payloads that passed.

### Scripted conversations (S2)

Accuracy over every probe, three samples at temperature 0, with a 95% cluster-bootstrap interval over conversations,
and the paired difference from `truncate-pinned` on the same probes:

| Arm | 8,192 tokens | vs `truncate-pinned` | 16,384 tokens | Fact in payload at 8,192 |
| --- | --- | --- | --- | --- |
| `control-full` (no budget) | 0.995 | | | 100% |
| `concat` | 0.631 [0.598, 0.667] | −0.192 [−0.224, −0.159] | 0.896 | 69%: the rest did not fit and were not sent |
| `truncate` | 0.818 [0.784, 0.852] | −0.005 [−0.013, +0.003] | 0.896 | 90% |
| `truncate-pinned` | 0.823 [0.789, 0.856] | | 0.896 | 90% |
| `window` | 0.528 [0.484, 0.573] | −0.295 [−0.329, −0.261] | 0.528 | 55% |
| `summary` | 0.862 [0.826, 0.897] | +0.039 [+0.010, +0.069] | 0.862 | 93% |
| `cwa-format` | 0.529 [0.482, 0.575] | −0.294 [−0.329, −0.260] | 0.529 | 55% |
| `cwa-history` | 0.797 [0.759, 0.831] | −0.026 [−0.045, −0.007] | 0.885 | 86% |
| `cwa-state-x` | 0.821 [0.780, 0.860] | −0.002 [−0.033, +0.028] | 0.834 | 95% |
| `cwa-state` | 0.970 [0.959, 0.979] | +0.147 [+0.112, +0.183] | 0.968 | 100% |
| `cwa-memory`, `cwa-pipeline` | 0.989 [0.982, 0.995] | +0.166 [+0.134, +0.201] | 0.989 | 100% |

What the attribution shows:

- **Losing the fact is what fails the conventional arms.** At 8,192 tokens, pinned truncation sent the needed fact for
  90% of probes and answered 91% of those correctly, and 0% of the rest. Over 100-turn conversations its accuracy falls
  to 0.76. The arms with state from the user's facts sent it every time.
- **Model-written state carries its own errors.** `cwa-state-x` sent the fact more often than truncation (95%) but
  answered fewer of those correctly (86%), because its state can hold a wrong or stale value; three of its payloads
  were refused because the state alone outgrew the budget. Its weakest family is FR (0.44): the extractor keeps an
  order's lines poorly.
- **Format alone moves nothing:** `cwa-format` scores as `window` does, on the same selection.
- **History alone in CWA is slightly worse** than front truncation: CWA's rendering costs tokens, so fewer turns fit.
  At 16,384 tokens, where `concat` and both truncations send the whole conversation, it reaches 0.885 against their
  0.896.
- **Stale values** (an earlier value of the figure asked) stay near 5% or below in every arm; the oracle-state arm
  holds 2.4%, the memory arms 0.5%.

**Instruction persistence (IP).** Every arm that keeps the system prompt follows the sign-off rule on every probe and
the bracket rule on at least 89%; dropping the prompt (`truncate`) loses both on a third of probes. The capital-letters
rule is the one the model resists: with the whole conversation it complied on 7 of 336 probes, and the conventional arms
on 3 to 71 at 8,192 tokens. The CWA arms complied on 141 to 210, and the format control on 128 (`window`, its
selection: 71), so CWA's rendering of the instructions is much of what helps, not only its selection.

### Unreliability (S3)

S2's payloads again, five samples at temperature 0.7. The same order holds (`cwa-memory` 0.989, `cwa-state` 0.956,
`summary` 0.854, `truncate-pinned` 0.810, `cwa-state-x` 0.806, `window` 0.524), and the paired differences match S2's
within 1.3 points. Unreliability, the study's U90−10 (the spread between the 90th and 10th percentile scores of the
repeated samples), in points: `cwa-memory` 1.5, `cwa-state` 6.8, `summary` 7.3, `truncate-pinned` 8.8, `cwa-state-x`
9.4, `truncate` 9.5. The arms that answer reliably right also answer consistently.

### Long context (S4)

The LQ family at 8,192 tokens over corpora of 0.25 to 16 times the budget (about 3,000 to 132,000 tokens), 10
corpora at each size, six questions each, three samples at temperature 0. The retrieval arms draw on the same
retriever's 64 best chunks.

| Arm | 0.25× | 1× | 4× | 16× |
| --- | --- | --- | --- | --- |
| `control-full` (the whole corpus, no budget) | 0.889 | 0.839 | 0.722 | 0.628 |
| `truncate-pinned` (the corpus's tail that fits) | 0.889 | 0.461 | 0.317 | 0.300 |
| `rag` (the best chunks that fit, best first) | 0.994 | 1.000 | 1.000 | 1.000 |
| `cwa-evidence` | 0.917 | 0.950 | 0.917 | 0.806 |
| `cwa-reinforced` (the instructions repeated before the question) | 0.917 | 0.967 | 0.883 | 0.833 |
| `cwa-ranked` (`cwa-evidence`, its evidence ids led by the retriever's rank) | 0.983 | 1.000 | 1.000 | 1.000 |

The whole corpus degrades as it grows, and truncation loses the answer. The first two CWA arms, built on the
specification's `long-context-qa` route, trailed `rag` by up to 0.19 although every chunk an answer needed was in
their payloads at every size. Their misses were mostly comparisons, where the model named the other of two sites, and
questions the documents do not answer. The cause was order. The specification renders a placement's items in id order
(conformance/README.md, Rendering), and these arms gave each chunk its corpus id, so their evidence read in document
order, where `rag`'s reads best first; `control-full`, also in document order, missed the same questions.
`cwa-ranked` is `cwa-evidence` with each id led by the chunk's rank (`chunk:r001:d0042:c03`). It keeps exactly the same
evidence at every budget and differs only in order, and it answered 717 of the 720 questions correctly (`rag`: 719).
Its three misses are one comparison over the smallest corpus, answered wrongly in every sample, which `rag` also missed
once. The spec's repeated instructions helped a little at 16× and not otherwise.

So under CWA the order evidence reads in is the application's to choose, through the ids it gives, and an application
should give retrieved evidence ids that sort by rank. The specification already ranks a slot's items for shedding
(`order_by`); letting a route render in that order would remove the trap. That change is proposed to the specification
as CEP-0002, render a slot in its rank order, and is open for
[discussion](https://github.com/contextwindowarchitecture/contextwindowarchitecture/discussions/10).

### Cost and the token estimate (S5)

At 8,192 tokens, prompt tokens per correct answer: `cwa-memory` 1,727, `summary` 1,884, `window` 2,153, `cwa-state`
4,779, `truncate-pinned` 4,803, `cwa-state-x` 5,874. Keeping state and recent turns, not the whole history, is both the
most accurate and the cheapest. Latency is each reply's time as recorded when it was filled into the cache on
2026-10-12, with 128 requests in flight on one GPU, so it describes that load, not a single request.

No payload exceeded its budget by the server's own count (R-16), at a 35% margin, over 87,918 calls with a budget.
That margin was found, not assumed: `estimate-utf8/v1` estimates from bytes, and it under-counted the server's tokens
by up to 27.6% on short prompts (500 to 2,000 tokens, far below any budget) and, in the first recorded run, by 25% on
the extractor's state of many short facts, which is what raised the margin in steps from 20% to 35%. A margin m covers an under-count u only when m ≥ u ÷ (1 − u). Model-written text can be worse: told
the uppercase rule, the extractor once wrote its state in capitals, which the estimate under-counted by 37%. No fixed
margin bounds every text a model can write; a route that estimates tokens should keep its margin under measurement.

### Model in the loop (S6)

The study's setup: the model's reply at each turn becomes the history of the next. VT at pilot size, six arms, three
chains each at temperature 0.7 (108 chains, 5,760 turns, 576 probes), every CWA payload gated before it was sent:

| Arm | Accuracy | vs the scripted result (S3) |
| --- | --- | --- |
| `cwa-memory` | 1.000 | 0.000 |
| `cwa-history` | 0.958 | +0.046 |
| `truncate-pinned` | 0.948 | −0.008 |
| `summary` | 0.927 | −0.073 |
| `cwa-state` | 0.917 | −0.033 |
| `window` | 0.719 | +0.013 |

With the model's own replies in the history, every arm but `summary` stays within about five points of its scripted
result, so on this family the replies mostly do not anchor the model to its errors. The rolling summary, now written
over the model's replies, left the needed fact out on 6 of its 96 probes and fell 7 points (95% interval −0.162 to
0.000). Every interval reaches zero, and the pilot size is small (6 conversations), so these are indications, not the
recorded comparison.

### Goldens (S7)

The 6,048 probe snapshots all four assemblers agreed on in a pilot-size CI replay of every suite on the same host
(`20261012T135118Z-f2fee09`, 2026-10-12), adopted as goldens: every later run compares each assembler's answer with
them. Against the goldens adopted the day before from the first recorded run's replies, that replay and the recorded
run of 2026-10-12 each matched every answer they could compare, 21,504 of 21,504, with no regression. Neither made 672
of those goldens' snapshots, all `cwa-state-x`: with the refilled replies the extractor wrote a different state, so
those snapshots changed, and the adopted goldens hold the snapshots the clean pipeline now makes in their place. The
recorded run's other 196,224 answers are at recorded size only and have no golden.

## What the run corrected along the way

Making the first recorded run, on 2026-10-11, surfaced two problems in the harness's own work, and both were fixed
before it; the runs of 2026-10-12 above repeat it, with those fixes, from an empty cache:

- **In an earlier attempt, the fact oracle disagreed** on some FR scripts: the generator could state the same fact
  sentence twice, which no reader can tell apart from two facts. The generator and S0's independent check were fixed
  (FR version 4).
- **In that run's first clean pass, S5 found 30 calls over budget**, all in `cwa-state-x`: the extractor was told the IP
  rule as part of its task and wrote its state in capitals (27), and one CC conversation's state held 278 short facts
  (3). The producers are now told the task without the reply rule, which governs replies, not what the application
  keeps, and the margin rose to 35%.

Domain 2 found no defect in the specification or the assemblers; the assembly gate passed everywhere, and the runs of
2026-10-12 recorded no findings.

## How far to trust it

- **One model.** Every number is for one model at one quantization, thinking off. A second model family is the
  obvious next step, and the cache and harness are built to take one.
- **Synthetic tasks.** The conversations are generated, fictional and regular. That avoids contamination and gives
  exact ground truth, but real dialogue is messier: these are controlled measurements, not field results.
- **The oracle state is an upper bound by construction.** `cwa-state`, `cwa-memory` and `cwa-pipeline` take their
  state from ground truth, so their lead shows what good state is worth, not what an application will get. The
  realistic arm, `cwa-state-x`, depends on the extractor's prompt and the model; a better state writer would move it.
- **Producer quality decides much of it.** The extractor, the summarizer and the retriever are harness choices held
  equal across arms, and the fact-in-payload oracle attributes every failure to the payload or the model, but a
  different producer would give different numbers.
- **The estimator margin is measured, not proven.** No payload overran at 35%, but the margin covers the text these
  runs produced.
- **Sampling.** Three samples at temperature 0 (five at 0.7) per probe, with cluster-bootstrap intervals over
  conversations and paired comparisons on the same probes. Many arms and metrics are compared, so a single interval
  that barely excludes zero (the summary's +0.039) is weaker evidence than the large, consistent differences.
  Refilling every reply from the same server version on 2026-10-12 moved S2's accuracies at 8,192 tokens by at most
  1.2 points (`cwa-state-x`, 0.833 to 0.821, its paired difference from +0.009 to −0.002) against the first recorded
  run, and S6's pilot `summary` by 7 points: small margins move with the replies; the large differences held.
- **The long-context retrieval is easy.** Every question and fact sentence names its site, so every needed chunk
  ranked in the retriever's top three even at 16×: the retrieval arms differ in assembly and order, not in retrieval.
  Harder retrieval (paraphrased names, facts without the name) is not tested.
- **S6 is a pilot.** Six conversations of one family.
- **A draft specification.** Every result is tied to the pinned contract commit.

## Reproducing it

```sh
uv sync                                         # at the benchmark root
cd domain2
uv run cwabench --domain 2 run                  # every suite at pilot size, replaying the committed cache
uv run cwabench --domain 2 run --size recorded --suites S0,S1,S2,S3,S4,S5,S7   # the recorded run, replayed
uv run cwabench --domain 2 ci nightly           # the CI profile: a replay with a drift report
```

The recorded run replays in about 70 minutes on 16 cores; the pilot run of every suite in about 12. Reported runs are
made on one GPU host that runs vLLM beside the harness: `scripts/pod-setup.sh` starts it in the container `cwa-vllm`
from the image it pins by digest, with domain2.toml's arguments, and `scripts/pod-run.sh` makes the runs with
`run --model llm`, which calls the model only for what the cache lacks (`FRESH_CACHE=1` empties the cache first). `domain2/README.md` lists the configuration,
the checkouts each run needs and the run directory's layout.
