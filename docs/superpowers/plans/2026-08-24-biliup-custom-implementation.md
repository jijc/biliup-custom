# biliup-custom Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a thin, automatically rebased biliup custom image that writes every recording segment directly to `/recordings/{streamer}/{record_date}/[真实时间][主播][标题].flv`, with a 04:00 logical-day boundary, while preserving upstream database and upload compatibility.

**Architecture:** Keep `jijc/biliup-custom` as a patch-and-build repository, not a fork. GitHub Actions clones the current `biliup/biliup` `master`, applies one focused Rust patch, runs upstream-targeted tests, builds multi-architecture images, and publishes `ghcr.io/jijc/biliup-custom:latest` plus an upstream-SHA tag only after all gates pass. The patch preserves legacy filename behavior unless a template is rooted at `/recordings/`; only that root enables path-aware templates, keeping rollback to official biliup safe.

**Tech Stack:** Rust/chrono, biliup upstream source, Git patch, Bash/Python guard scripts, GitHub Actions, Docker Buildx, GHCR

**Spec:** `docs/superpowers/specs/2026-08-24-biliup-custom-design.md`

## Global Constraints

- Do not modify biliup SQLite schema, account-cookie format, streamer schema, upload-template schema, uploader behavior, retry behavior, or WebUI data model.
- NAS mounts remain `./data:/opt` and `/volume1/Biliup:/recordings`.
- Recording path must be `/recordings/{streamer}/{record_date}/文件名` from the moment the `.part` file is created; no `mv` and no end-of-stream relocation.
- `{record_date}` uses local time minus four hours; 00:00-03:59 belongs to the previous date and 04:00 onward belongs to the current date.
- Filename time remains the real segment creation time, not the shifted logical date.
- Existing `{streamer}`, `{title}`, `{url}` and chrono/strftime placeholders remain supported.
- Legacy templates that are not rooted at `/recordings/` retain official single-filename sanitization semantics.
- Path-aware templates must sanitize substituted values, prevent `.`/`..` traversal, and never escape `/recordings`.
- `latest` is updated only after patch, tests, and all image builds succeed.
- Publish `upstream-<short_sha>` for every successful upstream build.
- If the patch stops applying or tests/builds fail, keep the previous `latest` and create/update a GitHub Issue.
- If upstream appears to implement equivalent native path support, stop publishing a new patched `latest`, keep the last verified image, and create a migration-review Issue.
- The custom layer must remain removable: switching the NAS image back to `ghcr.io/biliup/caution:latest` must not require database migration.

---

## File Structure

Files created in `jijc/biliup-custom`:

- `patches/0001-recording-paths.patch` — the only biliup source modification; adds safe `/recordings` path templates and dynamic `{record_date}` rendering plus Rust unit tests.
- `scripts/check-upstream-native.py` — conservative pre-patch capability probe; exits with a dedicated code when upstream likely has native equivalent support.
- `scripts/apply-and-test.sh` — clones/checks a pinned upstream SHA, runs the capability probe, applies the patch, and executes the focused Rust tests.
- `tests/test_check_upstream_native.py` — unit tests for the capability probe using small synthetic upstream source fixtures.
- `.github/workflows/sync-build.yml` — scheduled/manual/push orchestration, multi-arch build, GHCR publish, lock update, and Issue notification.
- `upstream.lock` — last successfully published upstream commit SHA.
- `README.md` — image use, exact filename template, Synology mounts, rollback instructions, notification behavior, and one-time GHCR visibility step.

Upstream files modified by `patches/0001-recording-paths.patch` after checkout:

- `crates/biliup-cli/src/server/common/util.rs` — safe placeholder substitution, path-aware sanitization for `/recordings/`, `{record_date}` rendering for Recorder-based paths, and tests.
- `crates/biliup/src/downloader/util.rs` — dynamic `{record_date}` rendering at each `LifecycleFile::create()` so stream-gears segments that cross 04:00 are placed in the correct logical-day directory, and tests.

No Dockerfile is maintained in the custom repo. The workflow builds the patched upstream checkout with upstream's own `Dockerfile`, reducing drift.

---

### Task 1: Implement the Recording-Path Patch with Focused Rust Tests

**Files:**
- Create: `patches/0001-recording-paths.patch`
- Patch upstream: `crates/biliup-cli/src/server/common/util.rs`
- Patch upstream: `crates/biliup/src/downloader/util.rs`

**Interfaces:**
- Consumes: existing `Recorder::filename_template() -> String`, `Recorder::generate_filename(&self, suffix: &str) -> String`, and `biliup::downloader::util::format_filename(file_name: &str) -> String`.
- Produces: a reserved `{record_date}` placeholder; path-aware output only for templates beginning with `/recordings/`; helper `logical_record_date(...) -> String`; legacy behavior for every other template.

- [ ] **Step 1: Write failing tests in a temporary upstream checkout**

Add tests to `crates/biliup-cli/src/server/common/util.rs` covering legacy compatibility, safe substitutions, root confinement, and the 04:00 rule. The tests should assert these exact behaviors:

```rust
#[test]
fn legacy_template_keeps_flat_sanitization() {
    let info = test_streamer_info("主播/甲", "标题/一");
    let recorder = Recorder::new(
        Some("{streamer}/[%Y-%m-%d][{title}]".to_string()),
        info,
    );
    assert_eq!(
        recorder.filename_template(),
        "主播_甲_[%Y-%m-%d][标题_一]"
    );
}

#[test]
fn recording_template_preserves_only_declared_directories() {
    let info = test_streamer_info("主播/甲", "标题:一/二?");
    let recorder = Recorder::new(
        Some("/recordings/{streamer}/{record_date}/[%Y-%m-%d][{title}]".to_string()),
        info,
    );
    assert_eq!(
        recorder.filename_template(),
        "/recordings/主播_甲/{record_date}/[%Y-%m-%d][标题_一_二_]"
    );
}

#[test]
fn recording_template_neutralizes_dot_components() {
    let info = test_streamer_info("..", ".");
    let recorder = Recorder::new(
        Some("/recordings/{streamer}/{record_date}/{title}".to_string()),
        info,
    );
    assert_eq!(
        recorder.filename_template(),
        "/recordings/_/{record_date}/_"
    );
}

#[test]
fn logical_day_changes_at_four_am() {
    let before = chrono::NaiveDate::from_ymd_opt(2026, 8, 25)
        .unwrap()
        .and_hms_opt(3, 59, 59)
        .unwrap();
    let at_boundary = chrono::NaiveDate::from_ymd_opt(2026, 8, 25)
        .unwrap()
        .and_hms_opt(4, 0, 0)
        .unwrap();
    assert_eq!(logical_record_date_naive(before), "2026-08-24");
    assert_eq!(logical_record_date_naive(at_boundary), "2026-08-25");
}
```

Add tests to `crates/biliup/src/downloader/util.rs` proving that the lower-level stream-gears formatter resolves `{record_date}` at file-creation time while keeping the real `%H:%M` value unchanged. Expose a deterministic helper for the test rather than depending on wall-clock time:

```rust
#[test]
fn format_filename_at_keeps_real_time_but_shifts_record_date() {
    let dt = chrono::NaiveDate::from_ymd_opt(2026, 8, 25)
        .unwrap()
        .and_hms_opt(2, 10, 0)
        .unwrap();
    assert_eq!(
        format_filename_at(
            "/recordings/主播/{record_date}/[%Y年%m月%d日-%H时%M分%S秒]",
            dt
        ),
        "/recordings/主播/2026-08-24/[2026年08月25日-02时10分00秒]"
    );
}
```

- [ ] **Step 2: Run focused tests and verify they fail before implementation**

Run from the upstream checkout:

```bash
cargo test -p biliup-cli server::common::util::tests -- --nocapture
cargo test -p biliup downloader::util::tests -- --nocapture
```

Expected: new tests fail because `{record_date}` and `/recordings` path-aware semantics do not exist yet.

- [ ] **Step 3: Implement safe placeholder substitution in the server Recorder**

In `crates/biliup-cli/src/server/common/util.rs`, split sanitization into component and template modes. Preserve the official `sanitize_filename()` behavior for legacy templates, and add a `/recordings/`-only path mode. The implementation must follow this shape:

```rust
const RECORDING_ROOT: &str = "/recordings/";

fn sanitize_component(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for ch in value.chars() {
        match ch {
            '\\' | '/' | ':' | '*' | '?' | '"' | '<' | '>' | '|' => out.push('_'),
            c if c.is_control() => out.push('_'),
            _ => out.push(ch),
        }
    }
    let out = out.trim_end_matches([' ', '.']).to_string();
    match out.as_str() {
        "" | "." | ".." => "_".to_string(),
        _ => out,
    }
}

fn sanitize_recording_template(raw: &str) -> String {
    if !raw.starts_with(RECORDING_ROOT) {
        return sanitize_filename(raw);
    }

    let tail = &raw[RECORDING_ROOT.len()..];
    let clean = tail
        .split('/')
        .map(sanitize_component)
        .collect::<Vec<_>>()
        .join("/");
    format!("{RECORDING_ROOT}{clean}")
}
```

Before substituting `{streamer}`, `{title}`, and `{url}`, sanitize each value with `sanitize_component()` so a slash originating in live metadata can never become a directory separator. Keep literal `{record_date}` unresolved in `filename_template()` for stream-gears to render per segment.

- [ ] **Step 4: Implement deterministic logical-date rendering for Recorder paths**

Add a helper based on naive local wall time:

```rust
fn logical_record_date_naive(dt: chrono::NaiveDateTime) -> String {
    (dt - chrono::Duration::hours(4))
        .format("%Y-%m-%d")
        .to_string()
}

fn render_record_date(template: &str, dt: chrono::NaiveDateTime) -> String {
    template.replace("{record_date}", &logical_record_date_naive(dt))
}
```

Use `render_record_date()` immediately before chrono formatting inside `Recorder::generate_filename()` and `Recorder::format_filename()`. This keeps FFmpeg/Recorder-based paths correct without changing public configuration types.

- [ ] **Step 5: Implement dynamic `{record_date}` rendering in stream-gears lifecycle formatting**

In `crates/biliup/src/downloader/util.rs`, add a deterministic helper and make the existing wall-clock function delegate to it:

```rust
pub fn format_filename_at(file_name: &str, local: chrono::NaiveDateTime) -> String {
    let record_date = (local - chrono::Duration::hours(4))
        .format("%Y-%m-%d")
        .to_string();
    let template = file_name.replace("{record_date}", &record_date);
    local.format(&template).to_string()
}

pub fn format_filename(file_name: &str) -> String {
    format_filename_at(file_name, Local::now().naive_local())
}
```

This is required because stream-gears creates each segment through `LifecycleFile::create()` and formats the filename at that moment; a stream spanning 04:00 must move new segments into the new logical-date directory without moving earlier files.

- [ ] **Step 6: Run the focused Rust tests and verify they pass**

Run:

```bash
cargo test -p biliup-cli server::common::util::tests -- --nocapture
cargo test -p biliup downloader::util::tests -- --nocapture
```

Expected: PASS for all new and existing tests in those modules.

- [ ] **Step 7: Run formatting and compile checks for the touched crates**

Run:

```bash
cargo fmt --all -- --check
cargo check -p biliup-cli
cargo check -p biliup
```

Expected: all commands exit 0.

- [ ] **Step 8: Export only the source changes as the custom patch**

Run from the patched upstream checkout:

```bash
git diff -- \
  crates/biliup-cli/src/server/common/util.rs \
  crates/biliup/src/downloader/util.rs \
  > ../biliup-custom/patches/0001-recording-paths.patch

test -s ../biliup-custom/patches/0001-recording-paths.patch
```

Expected: patch exists and contains changes only to the two intended upstream files.

- [ ] **Step 9: Commit Task 1**

```bash
git add patches/0001-recording-paths.patch
git commit -m "feat: add safe recording path patch"
```

---

### Task 2: Add Upstream Capability Detection and Patch Verification

**Files:**
- Create: `scripts/check-upstream-native.py`
- Create: `scripts/apply-and-test.sh`
- Create: `tests/test_check_upstream_native.py`

**Interfaces:**
- Consumes: path to a checked-out `biliup/biliup` source tree.
- Produces: capability probe exit `0` = safe to patch; exit `42` = upstream may have equivalent native support and publishing must stop for review; any other nonzero = script error. `apply-and-test.sh <upstream_sha> <workdir>` leaves a tested patched checkout at `<workdir>/upstream` on success.

- [ ] **Step 1: Write failing Python tests for conservative native-support detection**

Create `tests/test_check_upstream_native.py` using `tempfile.TemporaryDirectory()` and synthetic `crates/biliup-cli/src/server/common/util.rs` / `server/config.rs` files. Cover these exact cases:

```python
def test_current_upstream_shape_is_patchable():
    # sanitize_filename still replaces '/' and there is no record_date/output setting
    assert run_probe(current_shape_fixture()) == 0


def test_record_date_placeholder_triggers_migration_review():
    assert run_probe(fixture_with('record_date')) == 42


def test_path_aware_sanitizer_triggers_migration_review():
    # upstream no longer treats '/' as an illegal filename char
    assert run_probe(fixture_without_slash_sanitization()) == 42


def test_user_configurable_recording_output_triggers_migration_review():
    assert run_probe(fixture_with_config('recording_output_dir')) == 42
```

- [ ] **Step 2: Run the tests and verify failure**

Run:

```bash
python -m unittest tests/test_check_upstream_native.py -v
```

Expected: FAIL because `scripts/check-upstream-native.py` does not exist.

- [ ] **Step 3: Implement the conservative capability probe**

Create `scripts/check-upstream-native.py`. It must inspect only specific upstream files, not broad repository-wide keyword matches. Use this contract:

```python
PATCHABLE = 0
NATIVE_REVIEW = 42

# Flag review when one of these is true:
# 1. server/common/util.rs already contains "record_date".
# 2. sanitize_filename no longer contains a match arm that replaces '/'.
# 3. server/config.rs exposes a clearly user-facing recording path field such as
#    "recording_output_dir", "recording_dir", or "output_directory".
```

Print a short machine-readable reason such as `native-review: record_date found` to stdout before exiting `42`.

- [ ] **Step 4: Run probe unit tests and verify pass**

Run:

```bash
python -m unittest tests/test_check_upstream_native.py -v
```

Expected: PASS.

- [ ] **Step 5: Implement the pinned checkout / apply / test script**

Create executable `scripts/apply-and-test.sh` with strict mode and exact ordering:

```bash
#!/usr/bin/env bash
set -euo pipefail

UPSTREAM_SHA="${1:?upstream sha required}"
WORKDIR="${2:?workdir required}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

rm -rf "$WORKDIR/upstream"
git clone https://github.com/biliup/biliup.git "$WORKDIR/upstream"
git -C "$WORKDIR/upstream" checkout --detach "$UPSTREAM_SHA"

python "$ROOT/scripts/check-upstream-native.py" "$WORKDIR/upstream"
git -C "$WORKDIR/upstream" apply --check "$ROOT/patches/0001-recording-paths.patch"
git -C "$WORKDIR/upstream" apply "$ROOT/patches/0001-recording-paths.patch"

cargo test --manifest-path "$WORKDIR/upstream/Cargo.toml" -p biliup-cli server::common::util::tests -- --nocapture
cargo test --manifest-path "$WORKDIR/upstream/Cargo.toml" -p biliup downloader::util::tests -- --nocapture
cargo fmt --manifest-path "$WORKDIR/upstream/Cargo.toml" --all -- --check
```

Do not run a full workspace test suite here; upstream's Docker build remains the broader integration compile gate.

- [ ] **Step 6: Verify the script against the current upstream master SHA**

Run:

```bash
SHA="$(git ls-remote https://github.com/biliup/biliup.git refs/heads/master | awk '{print $1}')"
./scripts/apply-and-test.sh "$SHA" "$(mktemp -d)"
```

Expected: exit 0 and both focused Rust test groups pass.

- [ ] **Step 7: Commit Task 2**

```bash
git add scripts/check-upstream-native.py scripts/apply-and-test.sh tests/test_check_upstream_native.py
git commit -m "ci: validate upstream before patching"
```

---

### Task 3: Build, Publish, Track, and Notify from GitHub Actions

**Files:**
- Create: `.github/workflows/sync-build.yml`
- Create: `upstream.lock`

**Interfaces:**
- Consumes: `patches/0001-recording-paths.patch`, scripts from Task 2, upstream `master` SHA, GitHub `GITHUB_TOKEN`.
- Produces: `ghcr.io/jijc/biliup-custom:latest`, `ghcr.io/jijc/biliup-custom:upstream-<12-char-sha>`, updated `upstream.lock`, and GitHub Issues for build failure or native-support review.

- [ ] **Step 1: Seed the upstream lock without claiming a published image**

Create `upstream.lock` containing exactly:

```text
UNBUILT
```

This guarantees the first manual or scheduled run performs a build.

- [ ] **Step 2: Create the workflow triggers, permissions, and concurrency guard**

Start `.github/workflows/sync-build.yml` with:

```yaml
name: Sync and build biliup-custom

on:
  workflow_dispatch:
  schedule:
    - cron: '17 */6 * * *'
  push:
    branches: [main]
    paths-ignore:
      - 'upstream.lock'
      - 'docs/**'

permissions:
  contents: write
  packages: write
  issues: write

concurrency:
  group: biliup-custom-sync
  cancel-in-progress: false
```

The six-hour cadence bounds upstream lag without rebuilding unnecessarily.

- [ ] **Step 3: Add a prepare job that resolves upstream SHA and skips unchanged schedules**

The job must:

1. checkout `jijc/biliup-custom`;
2. resolve `refs/heads/master` with `git ls-remote`;
3. expose full SHA and first 12 chars as job outputs;
4. read `upstream.lock`;
5. set `needs_build=true` on `workflow_dispatch` and repository `push` even when the SHA is unchanged;
6. on `schedule`, set `needs_build=false` only when `upstream.lock` exactly equals the current SHA.

Use `$GITHUB_OUTPUT`, not deprecated `set-output`.

- [ ] **Step 4: Add a validate job that distinguishes native-support review from ordinary failure**

The validate job checks out this repo and calls:

```bash
./scripts/apply-and-test.sh "${{ needs.prepare.outputs.upstream_sha }}" "$RUNNER_TEMP/biliup-custom"
```

Capture exit code `42` separately and expose `native_review=true`. Any other nonzero exit makes the job fail.

When `native_review=true`, do not continue to image builds.

- [ ] **Step 5: Add native-support migration Issue handling**

Add a job that runs only when `native_review == 'true'`. Use `actions/github-script@v7` to search for an open Issue titled:

```text
[migration] Upstream biliup may now support native recording paths
```

If none exists, create it with:

```text
The upstream capability guard detected a change that may make the custom recording-path patch unnecessary.

No new custom `latest` image was published. The previous verified image remains available.

Review upstream support for:
- recording output directory / path templates
- streamer/date subdirectories
- logical-day behavior

After confirming equivalent behavior, migrate Synology to the official image instead of extending this patch.
```

Do not automatically alter the patch or publish `latest` in this state.

- [ ] **Step 6: Add native multi-architecture build jobs following upstream's runner model**

Use a matrix with exactly:

```yaml
include:
  - platform: linux/amd64
    runner: ubuntu-24.04
  - platform: linux/arm64
    runner: ubuntu-24.04-arm
```

Each matrix job must:

1. checkout custom repo;
2. run `apply-and-test.sh` to produce `$RUNNER_TEMP/biliup-custom/upstream` at the pinned SHA;
3. login to GHCR with `${{ github.actor }}` / `${{ secrets.GITHUB_TOKEN }}`;
4. build the patched upstream using its own `Dockerfile` and the platform-native runner;
5. push by digest to `ghcr.io/jijc/biliup-custom`;
6. upload a one-file digest artifact named `digests-linux-amd64` or `digests-linux-arm64`.

Do not maintain a copied Dockerfile in this repository.

- [ ] **Step 7: Add manifest merge and immutable/updatable tags**

After both platform jobs succeed, download both digest artifacts and create one manifest with these two tags:

```text
ghcr.io/jijc/biliup-custom:latest
ghcr.io/jijc/biliup-custom:upstream-${SHORT_SHA}
```

Run `docker buildx imagetools inspect` on both tags and fail if either manifest is missing one architecture.

- [ ] **Step 8: Update `upstream.lock` only after successful manifest publication**

After manifest inspection succeeds:

```bash
printf '%s\n' "$UPSTREAM_SHA" > upstream.lock
git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add upstream.lock
git commit -m "chore: record upstream $SHORT_SHA"
git push
```

Because `upstream.lock` is ignored by the push trigger, this cannot recursively rebuild.

- [ ] **Step 9: Add build-failure Issue creation/update**

Add a final notification job with `if: ${{ failure() && needs.prepare.outputs.needs_build == 'true' }}` that opens or comments on one Issue titled:

```text
[sync] biliup-custom build failed
```

The comment/body must include the upstream SHA and a link to `${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}`. Reuse the same open Issue rather than creating one per failure.

- [ ] **Step 10: Close the sync-failure Issue after a later successful publish**

After a successful manifest + lock update, use `actions/github-script@v7` to locate the open `[sync] biliup-custom build failed` Issue, add a recovery comment containing the published upstream SHA, and close it.

Do not auto-close the `[migration]` Issue; that one requires human confirmation before returning to official biliup.

- [ ] **Step 11: Run YAML and script static checks**

Run locally when available:

```bash
python -m unittest tests/test_check_upstream_native.py -v
bash -n scripts/apply-and-test.sh
python - <<'PY'
from pathlib import Path
import yaml
yaml.safe_load(Path('.github/workflows/sync-build.yml').read_text())
print('workflow yaml ok')
PY
```

Expected: all checks exit 0. If PyYAML is unavailable locally, the first GitHub Actions manual run is the YAML parser gate.

- [ ] **Step 12: Commit Task 3**

```bash
git add .github/workflows/sync-build.yml upstream.lock
git commit -m "ci: auto-sync and publish custom images"
```

---

### Task 4: Document Synology Use, Rollback, and One-Time Configuration

**Files:**
- Create: `README.md`

**Interfaces:**
- Consumes: published GHCR image and existing Synology mounts.
- Produces: exact no-code operator instructions for long-term use and rollback.

- [ ] **Step 1: Write README installation and template section**

Document the image:

```text
ghcr.io/jijc/biliup-custom:latest
```

Document the required Synology mounts exactly:

```yaml
volumes:
  - ./data:/opt
  - /volume1/Biliup:/recordings
```

Document the long-term filename template exactly:

```text
/recordings/{streamer}/{record_date}/[%Y年%m月%d日-%H时%M分%S秒][{streamer}][{title}]
```

Show the expected result:

```text
/volume1/Biliup/
└── 测试录播-老姨/
    └── 2026-08-24/
        └── [2026年08月24日-15时35分25秒][测试录播-老姨][六耳猕猴合点宝宝~].flv
```

Also document that 2026-08-25 02:10 is stored under `2026-08-24/` while the filename still says `2026年08月25日-02时10分...`.

- [ ] **Step 2: Document no-move and segment lifecycle semantics**

Explain that `.flv.part` is created directly inside the final streamer/date directory and becomes `.flv` in place when the segment closes. Explicitly state that no `mv` postprocessor should be configured.

- [ ] **Step 3: Document update and failure behavior**

Explain:

- schedule checks every six hours;
- unchanged upstream SHA does not rebuild on scheduled runs;
- successful builds publish `latest` and `upstream-<sha>`;
- failed builds leave `latest` unchanged and open/update a sync Issue;
- suspected native upstream support stops patched publication and opens a migration Issue.

- [ ] **Step 4: Document rollback to official biliup**

Give the exact image replacement:

```text
from: ghcr.io/jijc/biliup-custom:latest
to:   ghcr.io/biliup/caution:latest
```

State that `/opt` database/config mount and `/volume1/Biliup` recordings remain unchanged, so no database migration is required. Before rollback, change any `/recordings/.../{record_date}/...` filename template back to a template supported by the then-current official biliup unless upstream has adopted the equivalent feature.

- [ ] **Step 5: Document the one-time GHCR package visibility check**

After the first successful image publish, instruct the operator to open the GitHub package settings and ensure the package is public if Synology should pull it without registry credentials. This is a one-time GitHub UI step, not an ongoing maintenance task.

- [ ] **Step 6: Commit Task 4**

```bash
git add README.md
git commit -m "docs: add Synology operation and rollback guide"
```

---

### Task 5: End-to-End GitHub and Recording Verification

**Files:**
- Verify: `.github/workflows/sync-build.yml`
- Verify: `upstream.lock`
- Verify: GHCR package manifests
- Verify on NAS: existing biliup Docker project configuration and recording output

**Interfaces:**
- Consumes: all previous tasks.
- Produces: a verified deployable `latest` image and evidence that the final path behavior works before enabling automatic container replacement.

- [ ] **Step 1: Trigger the workflow manually**

Run `Sync and build biliup-custom` with `workflow_dispatch` from GitHub Actions.

Expected: prepare → validate → amd64/arm64 build → manifest → lock update all succeed; no migration Issue is opened.

- [ ] **Step 2: Verify the lock matches the built upstream commit**

Fetch `upstream.lock` and confirm it is a 40-character SHA equal to the workflow's resolved upstream SHA.

- [ ] **Step 3: Verify both published tags and architectures**

Inspect:

```bash
docker buildx imagetools inspect ghcr.io/jijc/biliup-custom:latest
docker buildx imagetools inspect ghcr.io/jijc/biliup-custom:upstream-<12-char-sha>
```

Expected: each tag contains `linux/amd64` and `linux/arm64`.

- [ ] **Step 4: Switch Synology biliup image without changing mounts**

Change only the image to:

```text
ghcr.io/jijc/biliup-custom:latest
```

Keep:

```text
./data:/opt
/volume1/Biliup:/recordings
```

Do not configure `mv` postprocessing.

- [ ] **Step 5: Set the filename template once in biliup WebUI**

Use:

```text
/recordings/{streamer}/{record_date}/[%Y年%m月%d日-%H时%M分%S秒][{streamer}][{title}]
```

Keep the existing 45-minute segment setting and test-stage upload template behavior unchanged.

- [ ] **Step 6: Start one test recording and verify the `.part` location immediately**

Expected while recording:

```text
/volume1/Biliup/<主播>/<逻辑日期>/[真实时间][主播][标题].flv.part
```

The file must appear there during the active segment; it must not first appear under `/docker/biliup/data` and must not require end-of-stream movement.

- [ ] **Step 7: Verify one completed segment renames in place**

After a segment closes, confirm the same directory contains `.flv` and the `.part` suffix is gone. Confirm there is no duplicate source file in `/docker/biliup/data`.

- [ ] **Step 8: Verify metadata sanitization with a title containing path characters**

Use or wait for a title containing `/`, `:`, or `?`. Confirm those characters become `_` inside the filename and do not create unintended subdirectories.

- [ ] **Step 9: Verify automatic-upload compatibility only after path behavior passes**

Associate a normal biliup upload template for one controlled test session. Confirm the completed segment path under `/recordings/...` is accepted by the original upload pipeline and that no database/config migration occurs.

- [ ] **Step 10: Defer NAS auto-replacement until several days of recording stability**

Do not install or enable Watchtower in this implementation pass. Per the approved design, first run the custom image manually for the parallel stability test. Once stable, add a separate narrowly scoped Synology auto-update step that manages only the biliup-custom container.

- [ ] **Step 11: Record verification status in the README**

Add a short `Verified` section with the first successful upstream SHA and the tested NAS architecture. Do not claim upload compatibility until Step 9 has actually passed.

- [ ] **Step 12: Commit verification documentation**

```bash
git add README.md
git commit -m "docs: record first verified custom build"
```

---

## Self-Review Results

- **Spec coverage:** All approved requirements are assigned: direct final-directory recording (Tasks 1/5), 04:00 logical date (Task 1), safe metadata/path handling (Task 1), no schema/upload rewrite (global constraints and Task 5), automatic upstream sync (Task 3), immutable trace tag + `latest` (Task 3), failure Issues (Task 3), native-feature migration stop (Tasks 2/3), rollback to official image (Task 4), and delayed NAS auto-updater rollout after stability (Task 5).
- **Placeholder scan:** No `TBD`, `TODO`, or unspecified implementation steps remain.
- **Type consistency:** `{record_date}`, `logical_record_date_naive`, `format_filename_at`, exit code `42`, `upstream.lock`, and image tag names are consistent across tasks.
- **Scope note:** NAS auto-replacement is intentionally not implemented in this first pass because the approved design explicitly requires validating the custom image for several days before enabling an updater. The GitHub/GHCR side is fully automatic in this plan.
