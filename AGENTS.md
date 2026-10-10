# AGENTS.md

Guidance for anyone, human or agent, working in this repository.

## What this is

The CWA (Context Window Architecture) viability benchmark. Each domain has a plan in `docs/plans/` and a harness in its
own directory:

| Path | What |
| --- | --- |
| `docs/benchmarking_cwa_viability.md` | The overall benchmark design |
| `docs/plans/` | Working plans: `domain-1-plan.md` and `domain-2-plan.md`, each with an "as built" section per phase. Git-ignored: read and update them, never commit them |
| `docs/` | Committed write-ups, one per domain, written once the domain's plan is fully implemented |
| `pyproject.toml` | The uv workspace: every domain is a member, with one lockfile (`uv.lock`) and one environment |
| `domain1/` | The Domain 1 harness (package `cwabench`, the `cwabench` command); start with `domain1/README.md` |
| `domain2/` | The Domain 2 harness (package `cwabench2`, run as `cwabench --domain 2`), in progress; it builds on `domain1/`. Start with `domain2/README.md` |
| `../benchmark-ui` | The results UI, its design baseline (`DESIGN.md`) and its plan live in `contextwindowarchitecture/benchmark-ui`, a sibling checkout. It reads this repository (schemas, results, write-ups), never the reverse. Its harness changes (fixture runs, runs-index pointers, sweep curves, aggregates, upstream links on findings) are made here at that plan's UI-P0, when asked; until then, no UI work |
| `scripts/` | `pod-setup.sh` sets up one Linux GPU host to run vLLM beside the harness; `pod-run.sh` runs Domain 2's recorded run and S6 there, detached in tmux |
| `.github/workflows/` | CI, started by hand only (`workflow_dispatch`); don't add schedules or push triggers unasked |

Code comments cite the plans by name and section (`domain-1-plan.md, 7.2`); the plans live in `docs/plans/`.

## Boundaries

- Outside this repository, read only the spec (`../../contextwindowarchitecture`) and the assembler checkouts
  (`../../assembler-{python,typescript,go,rust,template,demo}`). Leave other sibling directories alone.
- `../benchmark-ui` is the results UI's repository. Read it only to answer a question about the UI; never modify it
  from here.
- Never modify those checkouts. Builds write only under `domain1/.build/`, and `cwabench ci --fetch` keeps its own
  clones under `domain1/.build/ci/`.
- The spec is pinned in `domain1/domain1.toml`. Moving the pin is a deliberate change, made in its own commit.

## Working on the harness

```sh
uv sync                                # at the benchmark root: every domain, into one .venv
cd domain1
uv run pytest                          # fast; needs the spec checkout, no assembler
uv run cwabench run --suites S1        # one suite; a full run is ~85 min, S7 alone ~70
uv run cwabench ci nightly --fetch     # every suite but S7 against upstream main (~20 min)
uv run cwabench validate               # re-check the latest run directory, of any installed domain
cd ../domain2 && uv run pytest && uv run cwabench --domain 2 run --no-frames   # Domain 2: tests, a pilot
                                                                               #   run replaying the model (~6 min)
```

- One command serves every domain: plain `cwabench` is Domain 1, and `cwabench --domain <n>` runs domain n's
  command, which its package registers under the `cwabench.domains` entry points. Domain 1's code names no other
  domain; a new domain adds itself as a workspace member with its own entry point.
- `uv sync` inside a member directory installs that member alone and removes the other domains from the environment
  (Domain 1's CI does this on purpose); sync at the root to work on several.

- Python 3.12+, lines up to 120 characters; match the surrounding code's style, naming and comment density.
- Every output file names its schema (`"$schema": "cwa-bench-d1/<kind>/v1"`) and is validated before it is written. A
  new kind needs `domain1/schemas/<kind>.v1.schema.json`.
- A phase isn't done until the plan has its "as built" section and the README matches.
- When a domain's plan is fully implemented, write its committed write-up in `docs/` (for example `docs/domain1.md`):
  what the domain proves and how, the results, and how far they can be trusted, for readers who won't see the plan. The
  plan stays local; the write-up is what the repository publishes.
- Tests come with the change. Opt-in suites need `CWA_BENCH_REFERENCE=1` (the built reference assembler) or
  `CWA_BENCH_CONTAINER=1` (Podman).
- Don't call a model except through Domain 1's S11 `llm` mode or Domain 2's `run --model llm`, and only when asked.
  `domain1/summarizer-cache/` and `domain2/model-cache/` are committed so `replay` (Domain 2's default) works without
  one.

## Commits

- Commit incrementally while working: one logical change per commit, with tests passing, rather than one commit at the
  end.
- Commit directly on `main`. **Never push**; the maintainer pushes.
- Follow [Conventional Commits 1.0.0](https://www.conventionalcommits.org/en/v1.0.0/):
  `<type>(<scope>): <summary>`, imperative mood, summary under about 72 characters, a body that explains why.
  - Types: `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`, `chore`.
  - Scopes, as the change fits: `domain1`, `domain2`, a suite (`s2`, `s11`), `ci`, `schemas`, `plan`, `container`,
    `deps`.
  - Breaking changes (an output schema or CLI change that existing results or users depend on): `!` after the scope,
    plus a `BREAKING CHANGE:` footer.
  - Examples: `feat(s2): add the platform and toolchain matrix`, `fix(ci): take the source digest when a run starts`,
    `docs(plan): add P7 as built`.
- Sign off every commit (`git commit -s`). The Developer Certificate of Origin is required, and a commit hook rejects
  commits without it. The person committing owns the commit: no `Co-Authored-By` trailers, and never
  mention AI assistance anywhere in the message; the commit hook rejects it.
- Commits are GPG-signed by the global git config. Never bypass signing or hooks (`--no-gpg-sign`, `--no-verify`); if
  either fails, stop and ask.
- Never commit `results/`, `.build/`, `.venv/` or secrets. The `.gitignore` files cover the usual ones; check
  `git status` before committing.
