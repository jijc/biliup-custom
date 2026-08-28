#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "biliup-custom:manual-submit-guard:v1"
MANUAL_FEEDBACK_MARKER = "biliup-custom:manual-upload-feedback:v1"
NATIVE_REVIEW = 42
ENDPOINTS_REL = Path("crates/biliup-cli/src/server/api/endpoints.rs")


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
    raise ModifyError("unbalanced braces while locating post_uploads")


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


def modify(upstream: Path) -> None:
    path = upstream / ENDPOINTS_REL
    if not path.is_file():
        raise ModifyError(f"required upstream file missing: {path}")

    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print("already-modified")
        return
    if MANUAL_FEEDBACK_MARKER not in text:
        raise ModifyError("manual upload feedback modifier must run before manual submit guard")

    start, end = _function_span(text, "pub async fn post_uploads")
    function = text[start:end]
    if "MANUAL_SUBMIT_TIMEOUT_SECS" in function or "手动上传：B站最终投稿请求超时" in function:
        print("native-review: upstream/manual endpoint already has submit timeout handling")
        raise SystemExit(NATIVE_REVIEW)

    old = '''                let studio = build_studio(&upload_config, &bilibili, videos, &recorder).await?;
                submit_to_bilibili(&bilibili, &studio, submit_api.as_deref()).await?;
                info!(template_id = upload_config.id, "通过页面上传成功");
'''
    if function.count(old) != 1:
        raise ModifyError(
            f"manual build/submit anchor changed: expected 1 match, got {function.count(old)}"
        )

    new = '''                tracing::info!(
                    template_id = upload_config.id,
                    part_count = videos.len(),
                    "手动上传：媒体文件上传完成，开始构建B站稿件信息"
                );
                let studio = match tokio::time::timeout(
                    Duration::from_secs(MANUAL_BUILD_STUDIO_TIMEOUT_SECS),
                    build_studio(&upload_config, &bilibili, videos, &recorder),
                )
                .await
                {
                    Ok(result) => result?,
                    Err(_) => {
                        tracing::error!(
                            template_id = upload_config.id,
                            timeout_secs = MANUAL_BUILD_STUDIO_TIMEOUT_SECS,
                            "手动上传：B站稿件信息构建超时"
                        );
                        return Err(Report::new(AppError::Custom(format!(
                            "手动上传：B站稿件信息构建超时（{}秒），媒体文件已上传，但尚未确认投稿成功",
                            MANUAL_BUILD_STUDIO_TIMEOUT_SECS
                        ))));
                    }
                };

                tracing::info!(
                    template_id = upload_config.id,
                    part_count = studio.videos.len(),
                    timeout_secs = MANUAL_SUBMIT_TIMEOUT_SECS,
                    "手动上传：开始最终投稿"
                );
                match tokio::time::timeout(
                    Duration::from_secs(MANUAL_SUBMIT_TIMEOUT_SECS),
                    submit_to_bilibili(&bilibili, &studio, submit_api.as_deref()),
                )
                .await
                {
                    Ok(result) => {
                        result?;
                    }
                    Err(_) => {
                        tracing::error!(
                            template_id = upload_config.id,
                            part_count = studio.videos.len(),
                            timeout_secs = MANUAL_SUBMIT_TIMEOUT_SECS,
                            "手动上传：B站最终投稿请求超时"
                        );
                        return Err(Report::new(AppError::Custom(format!(
                            "手动上传：B站最终投稿请求超时（{}秒），媒体文件已上传，但稿件未确认提交成功",
                            MANUAL_SUBMIT_TIMEOUT_SECS
                        ))));
                    }
                }
                info!(template_id = upload_config.id, "通过页面上传成功");
'''
    function = function.replace(old, new, 1)

    prefix = f'''// {MARKER}
const MANUAL_BUILD_STUDIO_TIMEOUT_SECS: u64 = 60;
const MANUAL_SUBMIT_TIMEOUT_SECS: u64 = 120;

'''
    text = text[:start] + prefix + function + text[end:]
    path.write_text(text, encoding="utf-8")
    print("modified")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: fix_manual_submit_guard.py <upstream-dir>", file=sys.stderr)
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
