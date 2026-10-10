#!/usr/bin/env bash
# Set up one Linux GPU host (an x86_64 Ubuntu machine with an NVIDIA GPU and CUDA, such as a RunPod PyTorch pod) to
# run the benchmark and the model beside it, so a long `--model llm` run depends on nothing else.
#
# It installs the toolchains the adapters build with, clones the specification and the four assemblers at the
# commits the recorded run used, clones this repository, builds Domain 1's adapters, installs vLLM and starts it on
# 127.0.0.1:8000 in a tmux session named `vllm`, as domain2/domain2.toml's [model] describes. Every step is skipped
# when it is already done, so the script can be run again after a restart.
#
#   CWA_ROOT   where the checkouts go (default /workspace/cwa): the layout domain1.toml expects,
#              $CWA_ROOT/{contextwindowarchitecture,assembler-*} and $CWA_ROOT/cwa-extended/benchmark
#   HF_HOME    where the model's weights are kept (default /workspace/hf), on a volume that survives restarts
#   BENCH_REF  the benchmark commit or branch to check out (default main)
set -euo pipefail

CWA_ROOT=${CWA_ROOT:-/workspace/cwa}
export HF_HOME=${HF_HOME:-/workspace/hf}
BENCH_REF=${BENCH_REF:-main}
ORG=https://github.com/contextwindowarchitecture
NODE_VERSION=24.20.0
GO_VERSION=1.27.0
RUST_VERSION=1.99
VLLM_VERSION=0.31.0
# The specification as domain1.toml pins it, and the assemblers the recorded run assembled with
declare -A COMMITS=(
  [contextwindowarchitecture]=16de4be0534583f135a597171c927d42c87ee82c
  [assembler-python]=31f82710fdfb766b06f194b9224e26670d10c07c
  [assembler-typescript]=c533e4455073a27a2d5a4c65a3ddd62effa3795e
  [assembler-go]=fdd7e2fc74e53cdcd5da43541ee2959a6a68c98d
  [assembler-rust]=92b4d6f04f9d339d0925676ddbd30a2770a4d385
)

say() { printf '\n== %s\n' "$*"; }
mkdir -p "$CWA_ROOT" "$HF_HOME" "$CWA_ROOT/logs"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/go/bin:/opt/node/bin:$PATH"

say "system packages"
if ! command -v tmux >/dev/null || ! command -v zstd >/dev/null; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git curl ca-certificates build-essential pkg-config tmux \
    zstd xz-utils >/dev/null
fi

say "uv"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh

say "node $NODE_VERSION and pnpm"
if [ "$(node --version 2>/dev/null)" != "v$NODE_VERSION" ]; then
  rm -rf /opt/node && mkdir -p /opt/node
  curl -fsSL "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-x64.tar.xz" \
    | tar -xJ -C /opt/node --strip-components=1
fi
corepack enable

say "go $GO_VERSION"
if [ "$(go version 2>/dev/null | awk '{print $3}')" != "go$GO_VERSION" ]; then
  rm -rf /usr/local/go
  curl -fsSL "https://go.dev/dl/go$GO_VERSION.linux-amd64.tar.gz" | tar -xz -C /usr/local
fi

say "rust $RUST_VERSION"
if ! command -v rustup >/dev/null; then
  curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain "$RUST_VERSION"
fi
rustup toolchain install "$RUST_VERSION" --profile minimal
rustup default "$RUST_VERSION"

say "checkouts"
for repo in "${!COMMITS[@]}"; do
  if [ ! -d "$CWA_ROOT/$repo/.git" ]; then
    git clone -q "$ORG/$repo" "$CWA_ROOT/$repo"
  fi
  git -C "$CWA_ROOT/$repo" fetch -q origin
  git -C "$CWA_ROOT/$repo" -c advice.detachedHead=false checkout -q "${COMMITS[$repo]}"
done
BENCH="$CWA_ROOT/cwa-extended/benchmark"
if [ ! -d "$BENCH/.git" ]; then
  mkdir -p "$CWA_ROOT/cwa-extended"
  git clone -q "$ORG/benchmark" "$BENCH"
fi
git -C "$BENCH" fetch -q origin
git -C "$BENCH" -c advice.detachedHead=false checkout -q "$BENCH_REF"
git -C "$BENCH" pull -q --ff-only 2>/dev/null || true
(cd "$CWA_ROOT/assembler-typescript" && corepack install >/dev/null && pnpm install --frozen-lockfile --silent)

say "the benchmark's environment and Domain 1's adapters"
(cd "$BENCH" && uv sync -q)
(cd "$BENCH/domain1" && uv run cwabench setup)

say "vLLM $VLLM_VERSION"
if [ ! -x "$CWA_ROOT/vllm/bin/vllm" ]; then
  uv venv -q --python 3.12 "$CWA_ROOT/vllm"
  uv pip install -q --python "$CWA_ROOT/vllm/bin/python" "vllm==$VLLM_VERSION"
fi
if ! tmux has-session -t vllm 2>/dev/null; then
  tmux new-session -d -s vllm "HF_HOME=$HF_HOME $CWA_ROOT/vllm/bin/vllm serve Qwen/Qwen3.6-35B-A3B-FP8 \
    --served-model-name qwen3.6-35b-a3b-fp8 --host 127.0.0.1 --port 8000 --max-model-len 262144 \
    --gpu-memory-utilization 0.92 --max-num-seqs 256 --max-num-batched-tokens 16384 --enable-prefix-caching \
    --enable-prompt-tokens-details --generation-config vllm 2>&1 | tee $CWA_ROOT/logs/vllm.log"
fi
say "waiting for vLLM (the first start downloads about 35 GB of weights)"
until curl -fs http://127.0.0.1:8000/health >/dev/null; do
  if ! tmux has-session -t vllm 2>/dev/null; then
    echo "vLLM stopped; see $CWA_ROOT/logs/vllm.log" >&2
    exit 1
  fi
  sleep 10
done
curl -fs http://127.0.0.1:8000/version
echo
curl -fs http://127.0.0.1:8000/v1/models
echo
say "ready: $BENCH, vLLM on 127.0.0.1:8000"
