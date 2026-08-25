#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

MARKER = "// biliup-custom:auto-mp4:v1"
NATIVE_REVIEW = 42
UPLOAD_REL = Path("crates/biliup-cli/src/server/common/upload.rs")


class ModifyError(RuntimeError):
    pass


def _matching_brace(text: str, open_idx: int) -> int:
    depth = 0
    i = open_idx
    state = "code"
    block_depth = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""

        if state == "code":
            if ch == '"':
                state = "string"
            elif ch == "'":
                state = "char"
            elif ch == "/" and nxt == "/":
                state = "line_comment"
                i += 1
            elif ch == "/" and nxt == "*":
                state = "block_comment"
                block_depth = 1
                i += 1
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        elif state == "string":
            if ch == "\\":
                i += 1
            elif ch == '"':
                state = "code"
        elif state == "char":
            if ch == "\\":
                i += 1
            elif ch == "'":
                state = "code"
        elif state == "line_comment":
            if ch == "\n":
                state = "code"
        elif state == "block_comment":
            if ch == "/" and nxt == "*":
                block_depth += 1
                i += 1
            elif ch == "*" and nxt == "/":
                block_depth -= 1
                i += 1
                if block_depth == 0:
                    state = "code"
        i += 1
    raise ModifyError("unbalanced braces while locating function")


def _function_span(text: str, signature: str) -> tuple[int, int]:
    idx = text.find(signature)
    if idx < 0:
        raise ModifyError(f"expected function signature not found: {signature}")
    start = text.rfind("\n", 0, idx) + 1
    open_idx = text.find("{", idx + len(signature))
    if open_idx < 0:
        raise ModifyError(f"opening brace not found for: {signature}")
    close_idx = _matching_brace(text, open_idx)
    end = close_idx + 1
    if end < len(text) and text[end] == "\n":
        end += 1
    return start, end


def _replace_function(text: str, signature: str, replacement: str) -> str:
    start, end = _function_span(text, signature)
    return text[:start] + replacement.rstrip() + "\n" + text[end:]


UPLOAD_HELPERS = r'''// biliup-custom:auto-mp4:v1
fn mp4_target_for_finished_flv(src: &std::path::Path) -> Option<std::path::PathBuf> {
    // Finalized segments end in .flv. Active recordings end in .part, so they
    // are intentionally ignored and can never be touched by the converter.
    if src.extension().and_then(|s| s.to_str()) != Some("flv") {
        return None;
    }
    Some(src.with_extension("mp4"))
}

async fn remux_completed_flv_to_mp4(
    src: &std::path::Path,
) -> AppResult<std::path::PathBuf> {
    let Some(dst) = mp4_target_for_finished_flv(src) else {
        return Ok(src.to_path_buf());
    };

    // If a previous run already produced a non-empty MP4, reuse it. Only then
    // is the source FLV removed, keeping retries idempotent and loss-safe.
    if let Ok(meta) = tokio::fs::metadata(&dst).await {
        if meta.len() > 0 {
            if let Err(e) = tokio::fs::remove_file(src).await {
                tracing::warn!(source = %src.display(), error = ?e, "MP4 exists but source FLV could not be removed");
            }
            return Ok(dst);
        }
        let _ = tokio::fs::remove_file(&dst).await;
    }

    // Write to a temporary path first. The final .mp4 name only appears after
    // ffmpeg exits successfully and the file is confirmed non-empty.
    let tmp = dst.with_extension("mp4.partial");
    let _ = tokio::fs::remove_file(&tmp).await;
    let started = std::time::Instant::now();

    info!(source = %src.display(), target = %dst.display(), "开始自动转换 FLV → MP4");
    let status = tokio::process::Command::new("ffmpeg")
        .args([
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-fflags",
            "+genpts+igndts",
            "-i",
        ])
        .arg(src)
        .args([
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-avoid_negative_ts",
            "make_zero",
            "-f",
            "mp4",
        ])
        .arg(&tmp)
        .kill_on_drop(true)
        .status()
        .await
        .change_context(AppError::Custom(format!(
            "failed to start ffmpeg for {}",
            src.display()
        )))?;

    if !status.success() {
        let _ = tokio::fs::remove_file(&tmp).await;
        return Err(AppError::Custom(format!(
            "ffmpeg FLV to MP4 remux failed with status {status} for {}",
            src.display()
        ))
        .into());
    }

    let meta = tokio::fs::metadata(&tmp).await.change_context(AppError::Custom(format!(
        "ffmpeg MP4 output missing for {}",
        src.display()
    )))?;
    if meta.len() == 0 {
        let _ = tokio::fs::remove_file(&tmp).await;
        return Err(AppError::Custom(format!(
            "ffmpeg produced empty MP4 for {}",
            src.display()
        ))
        .into());
    }

    tokio::fs::rename(&tmp, &dst)
        .await
        .change_context(AppError::Custom(format!(
            "failed to finalize MP4 {}",
            dst.display()
        )))?;

    // The source is deleted only after a valid MP4 has been finalized.
    if let Err(e) = tokio::fs::remove_file(src).await {
        tracing::warn!(source = %src.display(), error = ?e, "MP4 conversion succeeded but source FLV could not be removed");
    }

    info!(
        source = %src.display(),
        target = %dst.display(),
        mib = meta.len() as f64 / 1048576.0,
        seconds = started.elapsed().as_secs_f64(),
        "自动转换 FLV → MP4 完成"
    );
    Ok(dst)
}

#[cfg(test)]
mod biliup_custom_auto_mp4_tests {
    use super::*;

    #[test]
    fn only_finalized_flv_files_get_an_mp4_target() {
        assert_eq!(
            mp4_target_for_finished_flv(std::path::Path::new("/recordings/a.flv")),
            Some(std::path::PathBuf::from("/recordings/a.mp4"))
        );
        assert_eq!(
            mp4_target_for_finished_flv(std::path::Path::new("/recordings/a.flv.part")),
            None
        );
        assert_eq!(
            mp4_target_for_finished_flv(std::path::Path::new("/recordings/a.mp4")),
            None
        );
    }
}
'''


PROCESS_WITHOUT_UPLOAD = r'''async fn process_without_upload<F>(
    rx: Inspect<Receiver<SegmentInfo>, F>,
    ctx: &Context,
) -> AppResult<()>
where
    F: FnMut(&SegmentInfo),
{
    let mut paths = Vec::new();
    pin!(rx);
    while let Some(event) = rx.next().await {
        let mut event_paths = segment_paths(&event);
        if let Some(video_path) = event_paths.first_mut() {
            match remux_completed_flv_to_mp4(&event.prev_file_path).await {
                Ok(converted) => {
                    *video_path = converted;
                }
                Err(e) => {
                    // Conversion failure must never destroy or hide the source
                    // FLV. Keep the original path and continue recording.
                    error!(
                        file = ?event.prev_file_path,
                        error = ?e,
                        "自动转换 MP4 失败，保留原 FLV"
                    );
                }
            }
        }
        paths.extend(event_paths);
    }
    execute_postprocessor(paths, ctx).await
}
'''

NO_UPLOAD_INLINE_RE = re.compile(
    r"(?P<indent>^[ \t]*)None => \{\n"
    r"(?P=indent)    let mut paths = Vec::new\(\);\n"
    r"(?P=indent)    pin!\(inspect\);\n"
    r"(?P=indent)    while let Some\(event\) = inspect\.next\(\)\.await \{\n"
    r"(?P=indent)        paths\.extend\(segment_paths\(&event\)\);\n"
    r"(?P=indent)    \}\n"
    r"(?P=indent)    // 无上传配置时，直接执行后处理\n"
    r"(?P=indent)    execute_postprocessor\(paths, &ctx\)\.await\n"
    r"(?P=indent)\}",
    re.MULTILINE,
)


def modify(upstream: Path) -> None:
    upload_path = upstream / UPLOAD_REL
    if not upload_path.is_file():
        raise ModifyError(f"required upstream file missing: {upload_path}")

    upload = upload_path.read_text(encoding="utf-8")
    if MARKER in upload:
        print("already-modified")
        return

    # If upstream grows an equivalent implementation, stop for human review
    # instead of stacking another converter on top of it.
    if "remux_completed_flv_to_mp4" in upload:
        print("native-review: upstream already contains FLV to MP4 remux logic")
        raise SystemExit(NATIVE_REVIEW)

    signature = "async fn process_without_upload<F>"
    start, _ = _function_span(upload, signature)
    upload = upload[:start] + UPLOAD_HELPERS + "\n\n" + upload[start:]
    upload = _replace_function(upload, signature, PROCESS_WITHOUT_UPLOAD)

    # When no upload template is selected, upstream duplicates the no-upload
    # loop inline instead of calling process_without_upload(). Match that one
    # exact structural block (whitespace-independent) and route it through the
    # shared function. Any semantic upstream change still fails the build.
    matches = list(NO_UPLOAD_INLINE_RE.finditer(upload))
    if len(matches) != 1:
        raise ModifyError(f"expected one no-upload inline branch, found {len(matches)}")
    match = matches[0]
    replacement = f"{match.group('indent')}None => process_without_upload(inspect, &ctx).await,"
    upload = upload[: match.start()] + replacement + upload[match.end() :]

    upload_path.write_text(upload, encoding="utf-8")
    print("modified")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: restore_segment_mp4.py <upstream-dir>", file=sys.stderr)
        return 2
    try:
        modify(Path(argv[1]))
        return 0
    except SystemExit as exc:
        return int(exc.code)
    except ModifyError as exc:
        print(f"modifier-error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
