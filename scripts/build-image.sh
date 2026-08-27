#!/usr/bin/env bash
set -euo pipefail

UPSTREAM_SHA="${1:?upstream sha required}"
IMAGE_TAG="${2:?image tag required}"
WORKDIR="${3:-${RUNNER_TEMP:-/tmp}/biliup-custom-image}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPSTREAM_DIR="$WORKDIR/upstream"

rm -rf "$UPSTREAM_DIR"
mkdir -p "$WORKDIR"
git clone --no-tags https://github.com/biliup/biliup.git "$UPSTREAM_DIR"
git -C "$UPSTREAM_DIR" checkout --detach "$UPSTREAM_SHA"

python "$ROOT/scripts/modify_upstream.py" "$UPSTREAM_DIR"
python "$ROOT/scripts/restore_segment_mp4.py" "$UPSTREAM_DIR"
python "$ROOT/scripts/add_daily_seq_wxpusher.py" "$UPSTREAM_DIR"
python "$ROOT/scripts/fix_daily_seq_temp_filename.py" "$UPSTREAM_DIR"
python "$ROOT/scripts/fix_danmaku_recording_path.py" "$UPSTREAM_DIR"
python "$ROOT/scripts/fix_daily_seq_stream_gears.py" "$UPSTREAM_DIR"
python "$ROOT/scripts/restore_server_log.py" "$UPSTREAM_DIR"
python "$ROOT/scripts/apply_product_customizations.py" "$UPSTREAM_DIR"
python "$ROOT/scripts/fix_override_streamer_fields.py" "$UPSTREAM_DIR"
python "$ROOT/scripts/fix_missing_upload_template_safety.py" "$UPSTREAM_DIR"
python "$ROOT/scripts/fix_recordings_browser.py" "$UPSTREAM_DIR"

docker build \
  --label "org.opencontainers.image.source=https://github.com/jijc/biliup-custom" \
  --label "io.biliup-custom.upstream-revision=$UPSTREAM_SHA" \
  --tag "$IMAGE_TAG" \
  "$UPSTREAM_DIR"

echo "built-image=$IMAGE_TAG"
