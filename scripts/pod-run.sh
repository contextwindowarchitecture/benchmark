#!/usr/bin/env bash
# Run Domain 2's recorded run and S6 against the model beside the harness (scripts/pod-setup.sh), detached from the
# machine that started it: `tmux new-session -d -s runs scripts/pod-run.sh`, then `tmux attach -t runs` to watch.
#
# Each run is made in `llm` mode and, if it does not pass, once more: the second pass calls the model only for what
# the first could not fill (a failed call, say), since everything answered is in the cache. Then:
#
#   1. the recorded run: every family at its recorded size, S0 to S5 and S7;
#   2. S6 at pilot size, with S2 and S3 for its comparison with the scripted result (their pilot replies are already
#      cached by the first run, whose conversations include the pilot's).
#
# Logs go to $CWA_ROOT/logs, and the runs to domain2/results/d2 as usual. CWA_KEY_FILE names a file holding the
# server's API key, when it needs one.
set -uo pipefail

CWA_ROOT=${CWA_ROOT:-/workspace/cwa}
BENCH="$CWA_ROOT/cwa-extended/benchmark"
WORKERS=${WORKERS:-$(nproc)}
CONCURRENCY=${CONCURRENCY:-128}
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/go/bin:/opt/node/bin:$PATH"
if [ -n "${CWA_KEY_FILE:-}" ]; then  # the server's key, when it needs one, as domain2.toml's api_key_env names it
  CWA_BENCH_MODEL_KEY=$(cat "$CWA_KEY_FILE")
  export CWA_BENCH_MODEL_KEY
fi
mkdir -p "$CWA_ROOT/logs"
cd "$BENCH/domain2"

run() {  # run <log name> <cwabench run arguments>…: twice at most, until it passes
  local name=$1
  shift
  for pass in 1 2; do
    echo "== $name, pass $pass, $(date -u +%FT%TZ)" | tee -a "$CWA_ROOT/logs/runs.log"
    if uv run cwabench --domain 2 run --model llm --no-frames --no-build --concurrency "$CONCURRENCY" \
        --workers "$WORKERS" "$@" > "$CWA_ROOT/logs/$name-$pass.log" 2>&1; then
      echo "== $name passed, $(date -u +%FT%TZ)" | tee -a "$CWA_ROOT/logs/runs.log"
      return 0
    fi
    tail -5 "$CWA_ROOT/logs/$name-$pass.log" | tee -a "$CWA_ROOT/logs/runs.log"
  done
  echo "== $name did not pass" | tee -a "$CWA_ROOT/logs/runs.log"
  return 1
}

run recorded --size recorded --suites S0,S1,S2,S3,S4,S5,S7
run s6 --size pilot --suites S0,S1,S2,S3,S6
echo "== done, $(date -u +%FT%TZ)" | tee -a "$CWA_ROOT/logs/runs.log"
