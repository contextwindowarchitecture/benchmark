#!/usr/bin/env bash
# Check and run both domains on the host scripts/pod-setup.sh prepared, detached from the machine that started it:
# `tmux new-session -d -s runs scripts/pod-run.sh`, then `tmux attach -t runs` to watch.
#
# The steps, in order; a step that fails is recorded and the next one runs:
#
#   1. Domain 2's tests, and its opt-in tests on the reference assembler;
#   2. Domain 2's recorded run (every family at its recorded size; S0 to S5 and S7) against the model beside it;
#   3. Domain 2's S6 at pilot size, with S2 and S3 for its comparison with the scripted result;
#   4. Domain 1's tests, its opt-in tests on the reference assembler and in the container (once in each engine
#      installed), a full run with Podman, and S2 and S10, the suites that use the container, again with Docker.
#
# A model run is made in `llm` mode and made again only when calls failed: the second pass calls the model for just
# what the first could not fill, since everything answered is in the cache. Any other failure is the run's result.
# With FRESH_CACHE=1, Domain 2's cache is moved aside first (to model-cache.<time>), so every reply comes from this
# host's server.
#
# Logs go to $CWA_ROOT/logs, each step's verdict to $CWA_ROOT/logs/runs.log, and the runs to each domain's results
# directory as usual. CWA_KEY_FILE names a file holding the server's API key, when it needs one.
set -uo pipefail

CWA_ROOT=${CWA_ROOT:-/workspace/cwa}
BENCH="$CWA_ROOT/cwa-extended/benchmark"
WORKERS=${WORKERS:-$(nproc)}
CONCURRENCY=${CONCURRENCY:-128}
LOGS="$CWA_ROOT/logs"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/go/bin:/opt/node/bin:$PATH"
if [ -n "${CWA_KEY_FILE:-}" ]; then  # the server's key, when it needs one, as domain2.toml's api_key_env names it
  CWA_BENCH_MODEL_KEY=$(cat "$CWA_KEY_FILE")
  export CWA_BENCH_MODEL_KEY
fi
mkdir -p "$LOGS"

note() { echo "== $*, $(date -u +%FT%TZ)" | tee -a "$LOGS/runs.log"; }

step() {  # step <name> <directory> <command>…: run once, record its verdict
  local name=$1 directory=$2
  shift 2
  note "$name"
  if (cd "$directory" && "$@") > "$LOGS/$name.log" 2>&1; then
    note "$name passed"
  else
    tail -5 "$LOGS/$name.log" | tee -a "$LOGS/runs.log"
    note "$name FAILED"
  fi
}

model_run() {  # model_run <name> <cwabench run arguments>…: Domain 2 in llm mode, again only if calls failed
  local name=$1
  shift
  for pass in 1 2; do
    note "$name, pass $pass"
    if (cd "$BENCH/domain2" && uv run cwabench --domain 2 run --model llm --no-frames --no-build \
        --concurrency "$CONCURRENCY" --workers "$WORKERS" "$@") > "$LOGS/$name-$pass.log" 2>&1; then
      note "$name passed"
      return 0
    fi
    tail -5 "$LOGS/$name-$pass.log" | tee -a "$LOGS/runs.log"
    if ! grep -qE 'call\(s\) failed|halted on a model call' "$LOGS/$name-$pass.log" \
        && ! grep -qE '"(endpoint|replay_miss)"' "$BENCH/domain2/results/d2/latest/findings.jsonl" 2>/dev/null; then
      break  # no call failed, so another pass would give the same result
    fi
  done
  note "$name FAILED"
  return 1
}

if [ "${FRESH_CACHE:-0}" = 1 ] && [ -d "$BENCH/domain2/model-cache" ]; then
  mv "$BENCH/domain2/model-cache" "$BENCH/domain2/model-cache.$(date -u +%Y%m%dT%H%M%SZ)"
  note "Domain 2's cache moved aside; starting empty"
fi

step d2-tests "$BENCH/domain2" uv run pytest -q
step d2-tests-reference "$BENCH/domain2" env CWA_BENCH_REFERENCE=1 uv run pytest -q tests/test_gate.py
model_run d2-recorded --size recorded --suites S0,S1,S2,S3,S4,S5,S7
model_run d2-s6 --size pilot --suites S0,S1,S2,S3,S6
step d1-tests "$BENCH/domain1" uv run pytest -q
step d1-tests-opt-in "$BENCH/domain1" env CWA_BENCH_REFERENCE=1 CWA_BENCH_CONTAINER=1 uv run pytest -q
step d1-run "$BENCH/domain1" uv run cwabench run
step d1-run-docker "$BENCH/domain1" uv run cwabench run --suites S2,S10 --container-engine docker
note "done"
