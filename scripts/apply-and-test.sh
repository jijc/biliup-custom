#!/usr/bin/env bash
set -euo pipefail

UPSTREAM_SHA="${1:?upstream sha required}"
WORKDIR="${2:?workdir required}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM_DIR="$WORKDIR/upstream"

rm -rf "$UPSTREAM_DIR"
git clone --no-tags https://github.com/biliup/biliup.git "$UPSTREAM_DIR"
git -C "$UPSTREAM_DIR" checkout --detach "$UPSTREAM_SHA"

run_modifier() {
  local script="$1"
  set +e
  python "$ROOT/scripts/$script" "$UPSTREAM_DIR"
  local rc=$?
  set -e
  if [[ "$rc" -eq 42 ]]; then
    echo "native-review: $script"
    exit 42
  fi
  if [[ "$rc" -ne 0 ]]; then
    exit "$rc"
  fi
}

run_modifier modify_upstream.py
run_modifier restore_segment_mp4.py
run_modifier add_daily_seq_wxpusher.py
run_modifier fix_daily_seq_temp_filename.py
run_modifier fix_danmaku_recording_path.py
run_modifier fix_daily_seq_stream_gears.py
run_modifier restore_server_log.py
run_modifier apply_product_customizations.py
run_modifier fix_override_streamer_fields.py
run_modifier fix_missing_upload_template_safety.py
run_modifier fix_recordings_browser.py

# biliup-cli embeds the already-built WebUI at compile time. The official
# Dockerfile builds it first; focused Rust tests only need the directory to
# exist so that unrelated WebUI compilation does not mask Rust test results.
mkdir -p "$UPSTREAM_DIR/out"
printf '<!doctype html><title>biliup-custom test fixture</title>\n' > "$UPSTREAM_DIR/out/index.html"

cargo test --manifest-path "$UPSTREAM_DIR/Cargo.toml" -p biliup-cli biliup_custom_recording_path_tests -- --nocapture
cargo test --manifest-path "$UPSTREAM_DIR/Cargo.toml" -p biliup-cli biliup_custom_auto_mp4_tests -- --nocapture
cargo test --manifest-path "$UPSTREAM_DIR/Cargo.toml" -p biliup-cli biliup_custom_daily_sequence_tests -- --nocapture
cargo test --manifest-path "$UPSTREAM_DIR/Cargo.toml" -p biliup-cli biliup_custom_daily_seq_temp_tests -- --nocapture
cargo test --manifest-path "$UPSTREAM_DIR/Cargo.toml" -p biliup-cli biliup_custom_stream_gears_filename_tests -- --nocapture
cargo test --manifest-path "$UPSTREAM_DIR/Cargo.toml" -p biliup-cli biliup_custom_wxpusher_tests -- --nocapture
cargo test --manifest-path "$UPSTREAM_DIR/Cargo.toml" -p biliup-cli biliup_custom_recordings_path_tests -- --nocapture
cargo test --manifest-path "$UPSTREAM_DIR/Cargo.toml" -p biliup biliup_custom_record_date_tests -- --nocapture
cargo test --manifest-path "$UPSTREAM_DIR/Cargo.toml" -p danmaku biliup_custom_danmaku_path_tests -- --nocapture
