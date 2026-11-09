#!/bin/sh
# Builds the plugin. Uses zig as the host linker when no system cc exists.
set -e
cd "$(dirname "$0")"
export PATH="$HOME/.cargo/bin:$PATH"
if ! command -v cc >/dev/null 2>&1 && [ -x "$HOME/.local/share/zam-zig/zigcc" ]; then
  export CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER="$HOME/.local/share/zam-zig/zigcc" CC="$HOME/.local/share/zam-zig/zigcc"
fi
cargo build --release
mkdir -p dist && cp target/wasm32-wasip1/release/zellij-agents.wasm dist/
echo "built dist/zellij-agents.wasm"
