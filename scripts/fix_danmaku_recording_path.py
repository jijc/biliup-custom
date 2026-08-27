#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "// biliup-custom:danmaku-recording-path:v1"
NATIVE_REVIEW = 42
SERVER_REL = Path("crates/biliup-cli/src/server/common/util.rs")
CLIENT_REL = Path("crates/danmaku/src/client.rs")


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


SERVER_FUNCTION = r'''// biliup-custom:danmaku-recording-path:v1
pub fn danmaku_filename_template(filename_prefix: Option<&str>, name: &str) -> String {
    let template = filename_prefix
        .map(|prefix| prefix.replace("{streamer}", name))
        .unwrap_or_else(|| format!("{}%Y-%m-%dT%H_%M_%S", name));

    if template.starts_with(RECORDING_ROOT) {
        strip_daily_sequence_token(&sanitize_recording_template(&template))
    } else {
        sanitize_filename(&template)
    }
}
'''

CLIENT_FUNCTIONS = r'''// biliup-custom:danmaku-recording-path:v1
fn format_output_path_at(template: &Path, local: chrono::NaiveDateTime) -> PathBuf {
    let record_date = (local - chrono::Duration::hours(4))
        .format("%Y-%m-%d")
        .to_string();
    let path_template = template
        .to_string_lossy()
        .replace("{record_date}", &record_date);
    let formatted = local.format(&path_template).to_string();
    PathBuf::from(formatted).with_extension("xml")
}

fn format_output_path(template: &Path) -> PathBuf {
    format_output_path_at(template, chrono::Local::now().naive_local())
}

#[cfg(test)]
mod biliup_custom_danmaku_path_tests {
    use super::*;

    #[test]
    fn logical_record_date_changes_at_four_am_without_changing_real_timestamp() {
        let template = Path::new(
            "/recordings/主播/{record_date}/[%Y年%m月%d日-%H时%M分%S秒][标题]",
        );
        let before = chrono::NaiveDate::from_ymd_opt(2026, 8, 27)
            .unwrap()
            .and_hms_opt(3, 59, 59)
            .unwrap();
        let at_boundary = chrono::NaiveDate::from_ymd_opt(2026, 8, 27)
            .unwrap()
            .and_hms_opt(4, 0, 0)
            .unwrap();

        assert_eq!(
            format_output_path_at(template, before),
            PathBuf::from(
                "/recordings/主播/2026-08-26/[2026年08月27日-03时59分59秒][标题].xml"
            )
        );
        assert_eq!(
            format_output_path_at(template, at_boundary),
            PathBuf::from(
                "/recordings/主播/2026-08-27/[2026年08月27日-04时00分00秒][标题].xml"
            )
        );
    }
}
'''


def modify(upstream: Path) -> None:
    server_path = upstream / SERVER_REL
    client_path = upstream / CLIENT_REL
    for path in (server_path, client_path):
        if not path.is_file():
            raise ModifyError(f"required upstream file missing: {path}")

    server = server_path.read_text(encoding="utf-8")
    client = client_path.read_text(encoding="utf-8")

    server_marked = MARKER in server
    client_marked = MARKER in client
    if server_marked or client_marked:
        if server_marked and client_marked:
            print("already-modified")
            return
        raise ModifyError("partial previous modification detected")

    if "record_date" in client:
        print("native-review: danmaku output path already references record_date")
        raise SystemExit(NATIVE_REVIEW)

    if "const RECORDING_ROOT" not in server or "fn sanitize_recording_template" not in server:
        raise ModifyError("recording path helper missing; run modify_upstream.py first")
    if "fn strip_daily_sequence_token(template: &str) -> String" not in server:
        raise ModifyError("daily sequence temp helper missing; run fix_daily_seq_temp_filename.py first")

    server = _replace_function(
        server,
        "pub fn danmaku_filename_template(filename_prefix: Option<&str>, name: &str) -> String",
        SERVER_FUNCTION,
    )
    client = _replace_function(
        client,
        "fn format_output_path(template: &Path) -> PathBuf",
        CLIENT_FUNCTIONS,
    )

    server_path.write_text(server, encoding="utf-8")
    client_path.write_text(client, encoding="utf-8")
    print("modified")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: fix_danmaku_recording_path.py <upstream-dir>", file=sys.stderr)
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
