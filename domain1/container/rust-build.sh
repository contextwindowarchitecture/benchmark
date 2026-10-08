#!/bin/sh
# Build the Rust adapter and its clock probe for the image's platform ($1, Docker's TARGETARCH). The stage runs on the
# build host's own platform: for another platform (S2's emulated linux/amd64 on an arm64 host) it cross-compiles,
# because rustc itself segfaults under QEMU's user-mode emulation. The binaries it makes then run emulated.
set -eu
case "$1" in
  amd64) triple=x86_64-unknown-linux-gnu; gcc=x86_64-linux-gnu-gcc; packages="gcc-x86-64-linux-gnu libc6-dev-amd64-cross" ;;
  arm64) triple=aarch64-unknown-linux-gnu; gcc=aarch64-linux-gnu-gcc; packages="gcc-aarch64-linux-gnu libc6-dev-arm64-cross" ;;
  *) echo "rust-build.sh: no target triple for $1" >&2; exit 1 ;;
esac
mkdir -p /out/bin /out/probes
if [ "$(rustc -vV | sed -n 's/^host: //p')" = "$triple" ]; then
  cargo build --release --locked --example adapter
  cp target/release/examples/adapter /out/bin/cwa-adapter-rust
  rustc -O /probes/clock.rs -o /out/probes/clock-rust
else
  apt-get update && apt-get install -y --no-install-recommends $packages
  rustup target add "$triple"
  export "CARGO_TARGET_$(echo "$triple" | tr 'a-z-' 'A-Z_')_LINKER=$gcc"
  cargo build --release --locked --example adapter --target "$triple"
  cp "target/$triple/release/examples/adapter" /out/bin/cwa-adapter-rust
  rustc -O --target "$triple" -C "linker=$gcc" /probes/clock.rs -o /out/probes/clock-rust
  echo "cross-compiled from $(rustc -vV | sed -n 's/^host: //p')" > /out/rust-cross.version  # reported with the toolchain versions
fi
rustc --version > /out/rust.version
