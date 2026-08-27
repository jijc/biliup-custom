#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "// biliup-custom:preserve-files-without-upload-template:v1"
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


def modify(upstream: Path) -> None:
    upload_path = upstream / UPLOAD_REL
    if not upload_path.is_file():
        raise ModifyError(f"required upstream file missing: {upload_path}")

    upload = upload_path.read_text(encoding="utf-8")
    start, end = _function_span(upload, "async fn process_without_upload<F>")
    function = upload[start:end]

    has_marker = MARKER in function
    has_flag = "run_postprocessor: bool" in function
    if has_marker or has_flag:
        if has_marker and has_flag:
            print("already-modified")
            return
        raise ModifyError("partial missing-upload-template safety modification detected")

    signature_anchor = "    ctx: &Context,\n) -> AppResult<()>"
    if function.count(signature_anchor) != 1:
        raise ModifyError("process_without_upload signature shape changed")
    function = function.replace(
        signature_anchor,
        "    ctx: &Context,\n    run_postprocessor: bool,\n) -> AppResult<()>",
        1,
    )

    tail_anchor = "    execute_postprocessor(paths, ctx).await\n"
    if function.count(tail_anchor) != 1:
        raise ModifyError("process_without_upload postprocessor tail changed")
    safe_tail = r'''    if run_postprocessor {
        return execute_postprocessor(paths, ctx).await;
    }

    // biliup-custom:preserve-files-without-upload-template:v1
    // A missing upload template can be accidental. Keep local recordings and
    // skip destructive postprocessors such as rm. Explicit Noop templates use
    // run_postprocessor=true and retain the user's intentional postprocessing.
    if !paths.is_empty() {
        tracing::warn!(
            count = paths.len(),
            url = %ctx.live_streamer().url,
            "No upload template is bound; preserving local recording files and skipping postprocessor"
        );
    }
    Ok(())
'''
    function = function.replace(tail_anchor, safe_tail, 1)
    upload = upload[:start] + function + upload[end:]

    upload_path.write_text(upload, encoding="utf-8")
    print("modified")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: fix_missing_upload_template_safety.py <upstream-dir>", file=sys.stderr)
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
