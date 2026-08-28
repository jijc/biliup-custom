# Submit Timeout and Log WebSocket Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship one validated biliup-custom release that hardens realtime-log WebSockets and prevents final Bilibili submission from hanging silently after all video parts have uploaded.

**Architecture:** Keep both fixes as source modifiers applied to the current upstream checkout. WebSocket resilience remains isolated to `ws.rs` and the log viewer. Final submission resilience is a separate modifier around `submit_to_bilibili`, so both automatic and manual submissions get stage logs and a bounded timeout without changing file-upload semantics or adding App→Web fallback.

**Tech Stack:** Python source modifiers/tests, Rust/Tokio/Axum/reqwest, Next.js/TypeScript, GitHub Actions, Docker.

**Spec:** `CUSTOM_CHANGES.md`

## Global Constraints

- Do not touch recording, FLV→MP4, daily numbering, filtering threshold, or postprocessor success semantics.
- Do not implement automatic App→Web fallback for code 21566.
- A timeout/final-submit failure must return an error, so successful postprocessing (including `rm`) cannot run.
- Manual submission background failures keep safe `template_id + file_count` observability; do not persist full upstream error bodies because upload errors can contain short-lived upload authorization.
- Publish only after Python tests, current-upstream focused Rust tests, full official Docker/WebUI build, container smoke test, multi-arch publish, and published `latest` smoke succeed.

---

### Task 1: Regression tests for final submission timeout

**Files:**
- Create: `tests/test_submit_timeout_modifier.py`
- Create: `scripts/fix_submit_timeout.py`

**Interfaces:**
- Consumes: upstream `pub async fn submit_to_bilibili(...) -> AppResult<ResponseData>`.
- Produces: marker `biliup-custom:submit-timeout:v1`, a 90-second bounded final-submit future, start/timeout logs, unchanged successful `Submit successful` behavior.

- [x] Write a failing fixture test that requires timeout wrapping, start log, timeout error, all three submit API branches, and idempotency.
- [x] Run the Python suite and confirm the new test fails before implementation.
- [x] Implement the minimal structural modifier.
- [x] Run the Python suite and confirm it passes.

### Task 2: Preserve safe manual-upload failure logging

**Files:**
- Review: `scripts/fix_manual_upload_feedback.py`
- Review: `tests/test_manual_upload_feedback_modifier.py`

**Interfaces:**
- Consumes: the existing manual background task result.
- Produces: safe failure logging with `template_id + file_count`, while final-submit timeout has its own explicit stage/error log.

- [x] Review the tempting `error = ?e` change against current upstream comments.
- [x] Reject that change because upstream upload errors can wrap short-lived authorization material.
- [x] Restore the existing safe logging behavior and keep final-submit-specific diagnostics in `fix_submit_timeout.py`.

### Task 3: Wire modifier into every build path and documentation

**Files:**
- Modify: `scripts/apply-and-test.sh`
- Modify: `scripts/build-image.sh`
- Modify: `.github/workflows/docker-validate.yml`
- Modify: `.github/workflows/publish.yml`
- Modify: `CUSTOM_CHANGES.md`

**Interfaces:**
- Consumes: `scripts/fix_submit_timeout.py`.
- Produces: identical modifier order across local test/build, Docker validation, and release publishing.

- [x] Add the new modifier after manual-upload feedback and WebSocket resilience.
- [ ] Document 90-second final-submit timeout, stage logs, failure/file-safety semantics, marker, tests, affected upstream file, and PR timeline.

### Task 4: Verification and release

**Files:** none beyond fixes required by verification.

- [ ] Confirm Python modifier tests pass on the final head.
- [ ] Confirm focused Rust tests pass against current upstream master.
- [ ] Confirm official Dockerfile/WebUI build and smoke test pass.
- [ ] Review PR diff for recording/upload safety invariants and secret-safe logging.
- [ ] Merge PR to `main` only after all PR checks are green.
- [ ] Confirm `publish.yml` succeeds for amd64 + arm64, manifest creation, pull, and `--help` smoke.
- [ ] Only then tell the NAS user to `docker compose pull && docker compose up -d`.
