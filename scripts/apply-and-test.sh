#!/usr/bin/env bash
set -euo pipefail

UPSTREAM_SHA="${1:?upstream sha required}"
WORKDIR="${2:?workdir required}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM_DIR="$WORKDIR/upstream"

rm -rf "$UPSTREAM_DIR"
git clone --no-tags https://github.com/biliup/biliup.git "$UPSTREAM_DIR"
git -C "$UPSTREAM_DIR" checkout --detach "$UPSTREAM_SHA"

set +e
python "$ROOT/scripts/modify_upstream.py" "$UPSTREAM_DIR"
MODIFIER_RC=$?
set -e

if [[ "$MODIFIER_RC" -eq 42 ]]; then
  echo "native-review"
  exit 42
fi
if [[ "$MODIFIER_RC" -ne 0 ]]; then
  exit "$MODIFIER_RC"
fi

cargo test --manifest-path "$UPSTREAM_DIR/Cargo.toml" -p biliup-cli biliup_custom_recording_path_tests -- --nocapture
cargo test --manifest-path "$UPSTREAM_DIR/Cargo.toml" -p biliup biliup_custom_record_date_tests -- --nocapture
cargo fmt --manifest-path "$UPSTREAM_DIR/Cargo.toml" --all -- --check
