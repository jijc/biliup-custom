#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "// biliup-custom:daily-seq-temp-clean:v1"
NATIVE_REVIEW = 42
COMMON_REL = Path("crates/biliup-cli/src/server/common")
UTIL_REL = COMMON_REL / "util.rs"
UPLOAD_REL = COMMON_REL / "upload.rs"


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


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ModifyError(f"expected one {label} anchor, found {count}")
    return text.replace(old, new, 1)


UTIL_HELPER = r'''// biliup-custom:daily-seq-temp-clean:v1
fn strip_daily_sequence_token(template: &str) -> String {
    template
        .replace("{daily_seq}-", "")
        .replace("{daily_seq}", "")
}

#[cfg(test)]
mod biliup_custom_daily_seq_temp_tests {
    use super::*;

    #[test]
    fn temporary_recording_template_has_no_daily_sequence_placeholder() {
        assert_eq!(
            strip_daily_sequence_token(
                "/recordings/主播/2026-08-25/{daily_seq}-[20260826-005203]"
            ),
            "/recordings/主播/2026-08-25/[20260826-005203]"
        );
    }
}
'''


SEQUENCED_PATH = r'''fn daily_sequence_enabled(ctx: &Context) -> bool {
    let config = ctx.config();
    ctx.live_streamer()
        .filename_prefix
        .as_deref()
        .or(config.filename_prefix.as_deref())
        .is_some_and(|template| template.contains(DAILY_SEQ_TOKEN))
}

fn sequenced_path(path: &Path, seq: u32) -> Option<PathBuf> {
    let name = path.file_name()?.to_str()?;
    if parse_daily_sequence(name).is_some() {
        return None;
    }
    Some(path.with_file_name(format!("{seq:02}-{name}")))
}
'''


FINALIZE_DAILY_SEQUENCE = r'''async fn finalize_daily_sequence(paths: &mut [PathBuf], ctx: &Context) -> AppResult<()> {
    if !daily_sequence_enabled(ctx) {
        return Ok(());
    }

    let Some(video_path) = paths.first().cloned() else {
        return Ok(());
    };
    if video_path
        .file_name()
        .and_then(|s| s.to_str())
        .is_some_and(|name| parse_daily_sequence(name).is_some())
    {
        return Ok(());
    }

    let parent = video_path.parent().ok_or_else(|| {
        error_stack::Report::new(AppError::Custom(format!(
            "daily sequence path has no parent: {}",
            video_path.display()
        )))
    })?;
    let mut seq = next_daily_sequence(parent).change_context(AppError::Custom(format!(
        "failed to scan daily sequence directory {}",
        parent.display()
    )))?;

    loop {
        let target = sequenced_path(&video_path, seq).ok_or_else(|| {
            error_stack::Report::new(AppError::Custom(format!(
                "failed to build daily sequence path for {}",
                video_path.display()
            )))
        })?;
        if tokio::fs::try_exists(&target)
            .await
            .change_context(AppError::Custom(format!(
                "failed to check daily sequence target {}",
                target.display()
            )))?
        {
            seq = seq.saturating_add(1);
            continue;
        }

        tokio::fs::rename(&video_path, &target)
            .await
            .change_context(AppError::Custom(format!(
                "failed to finalize daily sequence {} -> {}",
                video_path.display(),
                target.display()
            )))?;
        paths[0] = target;

        for path in paths.iter_mut().skip(1) {
            let Some(companion_target) = sequenced_path(path, seq) else {
                continue;
            };
            match tokio::fs::rename(&*path, &companion_target).await {
                Ok(()) => *path = companion_target,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => {
                    error!(source = %path.display(), target = %companion_target.display(), error = ?e, "failed to rename daily-sequence companion file");
                }
            }
        }
        return Ok(());
    }
}
'''


SEQUENCED_PATH_TEST = r'''fn prefixes_daily_sequence_without_changing_extension() {
        assert_eq!(
            sequenced_path(Path::new("/recordings/a/2026-08-25/demo.mp4"), 3),
            Some(PathBuf::from("/recordings/a/2026-08-25/03-demo.mp4"))
        );
    }
'''


def _modify_util(util: str) -> str:
    if MARKER in util:
        return util
    if "biliup-custom:auto-modifier:v1" not in util:
        raise ModifyError("recording path modifier must run before temp filename cleanup")

    helper_anchor = "impl Recorder {"
    if helper_anchor not in util:
        print("native-review: Recorder implementation changed upstream")
        raise SystemExit(NATIVE_REVIEW)
    util = util.replace(helper_anchor, UTIL_HELPER + "\n" + helper_anchor, 1)

    generate_old = "            let rendered = render_record_date(&template, t.naive_local());\n            let base = t.format(&rendered).to_string();"
    generate_new = "            let rendered = render_record_date(&template, t.naive_local());\n            let rendered = strip_daily_sequence_token(&rendered);\n            let base = t.format(&rendered).to_string();"
    util = _replace_once(util, generate_old, generate_new, "generate_filename rendering")

    format_old = "        let rendered = render_record_date(&template, t.naive_local());\n        t.format(&rendered).to_string()"
    format_new = "        let rendered = render_record_date(&template, t.naive_local());\n        let rendered = strip_daily_sequence_token(&rendered);\n        t.format(&rendered).to_string()"
    util = _replace_once(util, format_old, format_new, "format_filename rendering")
    return util


def _modify_upload(upload: str) -> str:
    if MARKER in upload:
        return upload
    if "biliup-custom:daily-seq-wxpusher:v1" not in upload:
        raise ModifyError("daily sequence modifier must run before temp filename cleanup")

    upload = _replace_function(upload, "fn sequenced_path(path: &Path, seq: u32) -> Option<PathBuf>", SEQUENCED_PATH)
    upload = _replace_function(upload, "async fn finalize_daily_sequence(paths: &mut [PathBuf]) -> AppResult<()>", FINALIZE_DAILY_SEQUENCE)

    event_calls = upload.count("finalize_daily_sequence(&mut event_paths).await")
    path_calls = upload.count("finalize_daily_sequence(&mut paths).await")
    if event_calls != 1 or path_calls != 1:
        print(
            "native-review: daily sequence call sites changed upstream/customizer "
            f"(event={event_calls}, paths={path_calls})"
        )
        raise SystemExit(NATIVE_REVIEW)
    upload = upload.replace(
        "finalize_daily_sequence(&mut event_paths).await",
        "finalize_daily_sequence(&mut event_paths, ctx).await",
        1,
    )
    upload = upload.replace(
        "finalize_daily_sequence(&mut paths).await",
        "finalize_daily_sequence(&mut paths, ctx).await",
        1,
    )

    upload = _replace_function(
        upload,
        "fn replaces_daily_sequence_token_without_changing_extension()",
        SEQUENCED_PATH_TEST,
    )
    return MARKER + "\n" + upload


def modify(upstream: Path) -> None:
    util_path = upstream / UTIL_REL
    upload_path = upstream / UPLOAD_REL
    for path in (util_path, upload_path):
        if not path.is_file():
            raise ModifyError(f"required upstream file missing: {path}")

    util = util_path.read_text(encoding="utf-8")
    upload = upload_path.read_text(encoding="utf-8")

    util_marked = MARKER in util
    upload_marked = MARKER in upload
    if util_marked or upload_marked:
        if util_marked and upload_marked:
            print("already-modified")
            return
        raise ModifyError("partial previous daily-seq temp cleanup detected")

    util = _modify_util(util)
    upload = _modify_upload(upload)

    util_path.write_text(util, encoding="utf-8")
    upload_path.write_text(upload, encoding="utf-8")
    print("modified")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: fix_daily_seq_temp_filename.py <upstream-dir>", file=sys.stderr)
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
