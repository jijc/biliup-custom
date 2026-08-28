#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "biliup-custom:submit-timeout:v1"
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
    raise ModifyError("unbalanced braces while locating submit_to_bilibili")


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


SUBMIT_FUNCTION = r'''// biliup-custom:submit-timeout:v1
const SUBMIT_TIMEOUT_SECS: u64 = 90;

pub async fn submit_to_bilibili(
    bilibili: &BiliBili,
    studio: &Studio,
    submit_api: Option<&str>,
) -> AppResult<ResponseData> {
    let submit_option = match submit_api {
        Some(submit) => SubmitOption::from_str(submit).unwrap_or(SubmitOption::App),
        _ => SubmitOption::App,
    };
    let submit_api_name = match &submit_option {
        SubmitOption::BCutAndroid => "bcut_android",
        SubmitOption::Web => "web",
        _ => "app",
    };

    info!(
        submit_api = submit_api_name,
        title = %studio.title,
        part_count = studio.videos.len(),
        timeout_secs = SUBMIT_TIMEOUT_SECS,
        "开始提交B站稿件"
    );

    let submit_future = async {
        match submit_option {
            SubmitOption::BCutAndroid => bilibili
                .submit_by_bcut_android(studio, None)
                .await
                .change_context(AppError::Unknown),
            SubmitOption::Web => bilibili
                .submit_by_web(studio, None)
                .await
                .change_context(AppError::Unknown),
            _ => bilibili
                .submit_by_app(studio, None)
                .await
                .change_context(AppError::Unknown),
        }
    };

    let result = match tokio::time::timeout(
        std::time::Duration::from_secs(SUBMIT_TIMEOUT_SECS),
        submit_future,
    )
    .await
    {
        Ok(result) => result?,
        Err(_) => {
            error!(
                submit_api = submit_api_name,
                title = %studio.title,
                part_count = studio.videos.len(),
                timeout_secs = SUBMIT_TIMEOUT_SECS,
                "B站最终投稿请求超时"
            );
            return Err(error_stack::Report::new(AppError::Custom(format!(
                "B站最终投稿请求超时（{}秒），文件上传已完成但稿件未确认提交成功",
                SUBMIT_TIMEOUT_SECS
            ))));
        }
    };

    info!("Submit successful");
    Ok(result)
}
'''


def modify(upstream: Path) -> None:
    path = upstream / UPLOAD_REL
    if not path.is_file():
        raise ModifyError(f"required upstream file missing: {path}")

    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print("already-modified")
        return

    start, end = _function_span(text, "pub async fn submit_to_bilibili")
    current = text[start:end]
    if "tokio::time::timeout" in current or "最终投稿请求超时" in current:
        print("native-review: upstream final submission already has timeout handling")
        raise SystemExit(NATIVE_REVIEW)

    required = [
        "SubmitOption::BCutAndroid",
        "SubmitOption::Web",
        "submit_by_bcut_android(studio, None)",
        "submit_by_web(studio, None)",
        "submit_by_app(studio, None)",
        'info!("Submit successful")',
    ]
    missing = [item for item in required if item not in current]
    if missing:
        raise ModifyError(f"submit_to_bilibili shape changed: missing {missing}")

    text = text[:start] + SUBMIT_FUNCTION + text[end:]
    path.write_text(text, encoding="utf-8")
    print("modified")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: fix_submit_timeout.py <upstream-dir>", file=sys.stderr)
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
