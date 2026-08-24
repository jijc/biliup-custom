#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

MARKER = "// biliup-custom:auto-modifier:v1"
NATIVE_REVIEW = 42

SERVER_REL = Path("crates/biliup-cli/src/server/common/util.rs")
DOWNLOADER_REL = Path("crates/biliup/src/downloader/util.rs")
CONFIG_REL = Path("crates/biliup-cli/src/server/config.rs")


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


def _native_review(server: str, downloader: str, config: str) -> str | None:
    if "record_date" in server or "record_date" in downloader:
        return "record_date found upstream"

    try:
        start, end = _function_span(server, "fn sanitize_filename(name: &str) -> String")
    except ModifyError:
        return "sanitize_filename shape changed"
    sanitize_fn = server[start:end]
    if not re.search(r"(?:\|\s*)?'/'(?:\s*\|)?", sanitize_fn):
        return "slash is no longer sanitized as a filename character"

    if re.search(r"\b(recording_output_dir|recording_dir|output_directory)\b", config):
        return "recording output directory setting found upstream"
    return None


SERVER_HELPERS = r'''// biliup-custom:auto-modifier:v1
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

fn logical_record_date_naive(dt: chrono::NaiveDateTime) -> String {
    (dt - chrono::Duration::hours(4))
        .format("%Y-%m-%d")
        .to_string()
}

fn render_record_date(template: &str, dt: chrono::NaiveDateTime) -> String {
    template.replace("{record_date}", &logical_record_date_naive(dt))
}
'''

SERVER_FILENAME_TEMPLATE = r'''    pub fn filename_template(&self) -> String {
        let raw = if let Some(prefix) = &self.filename_prefix {
            self.template_with(prefix)
        } else {
            format!("{}%Y-%m-%dT%H_%M_%S", self.streamer_info.name)
        };
        sanitize_recording_template(&raw)
    }
'''

SERVER_TEMPLATE_WITH = r'''    fn template_with(&self, template: &str) -> String {
        if template.starts_with(RECORDING_ROOT) {
            template
                .replace("{streamer}", &sanitize_component(&self.streamer_info.name))
                .replace("{title}", &sanitize_component(&self.streamer_info.title))
                .replace("{url}", &sanitize_component(&self.streamer_info.url))
        } else {
            template
                .replace("{streamer}", &self.streamer_info.name)
                .replace("{title}", &self.streamer_info.title)
                .replace("{url}", &self.streamer_info.url)
        }
    }
'''

SERVER_GENERATE_FILENAME = r'''    pub fn generate_filename(&self, suffix: &str) -> String {
        let template = self.filename_template();
        let mut t = Local::now();

        loop {
            let rendered = render_record_date(&template, t.naive_local());
            let base = t.format(&rendered).to_string();
            if !self.exists_with_suffix(&base, suffix) {
                return base;
            }
            t += Duration::seconds(1);
        }
    }
'''

SERVER_FORMAT_FILENAME = r'''    pub fn format_filename(&self) -> String {
        let template = self.filename_template();
        let t = self.streamer_info.date.with_timezone(&Local);
        let rendered = render_record_date(&template, t.naive_local());
        t.format(&rendered).to_string()
    }
'''

SERVER_TESTS = r'''
#[cfg(test)]
mod biliup_custom_recording_path_tests {
    use super::*;

    #[test]
    fn metadata_slashes_cannot_create_directories() {
        assert_eq!(sanitize_component("主播/甲"), "主播_甲");
        assert_eq!(sanitize_component(".."), "_");
    }

    #[test]
    fn recording_template_keeps_declared_directories_only() {
        assert_eq!(
            sanitize_recording_template("/recordings/主播/../{record_date}/标题"),
            "/recordings/主播/_/{record_date}/标题"
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
}
'''

DOWNLOADER_FORMAT = r'''// biliup-custom:auto-modifier:v1
pub fn format_filename_at(file_name: &str, local: chrono::NaiveDateTime) -> String {
    let record_date = (local - chrono::Duration::hours(4))
        .format("%Y-%m-%d")
        .to_string();
    let template = file_name.replace("{record_date}", &record_date);
    local.format(&template).to_string()
}

pub fn format_filename(file_name: &str) -> String {
    let local: DateTime<Local> = Local::now();
    format_filename_at(file_name, local.naive_local())
}
'''

DOWNLOADER_TESTS = r'''
#[cfg(test)]
mod biliup_custom_record_date_tests {
    use super::*;

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
}
'''


def modify(upstream: Path) -> None:
    server_path = upstream / SERVER_REL
    downloader_path = upstream / DOWNLOADER_REL
    config_path = upstream / CONFIG_REL
    for path in (server_path, downloader_path, config_path):
        if not path.is_file():
            raise ModifyError(f"required upstream file missing: {path}")

    server = server_path.read_text(encoding="utf-8")
    downloader = downloader_path.read_text(encoding="utf-8")
    config = config_path.read_text(encoding="utf-8")

    server_marked = MARKER in server
    downloader_marked = MARKER in downloader
    if server_marked or downloader_marked:
        if server_marked and downloader_marked:
            print("already-modified")
            return
        raise ModifyError("partial previous modification detected")

    review_reason = _native_review(server, downloader, config)
    if review_reason:
        print(f"native-review: {review_reason}")
        raise SystemExit(NATIVE_REVIEW)

    impl_anchor = "impl Recorder {"
    if impl_anchor not in server:
        raise ModifyError("Recorder impl anchor not found")
    server = server.replace(impl_anchor, SERVER_HELPERS + "\n" + impl_anchor, 1)
    server = _replace_function(
        server,
        "pub fn filename_template(&self) -> String",
        SERVER_FILENAME_TEMPLATE,
    )
    server = _replace_function(
        server,
        "fn template_with(&self, template: &str) -> String",
        SERVER_TEMPLATE_WITH,
    )
    server = _replace_function(
        server,
        "pub fn generate_filename(&self, suffix: &str) -> String",
        SERVER_GENERATE_FILENAME,
    )
    server = _replace_function(
        server,
        "pub fn format_filename(&self) -> String",
        SERVER_FORMAT_FILENAME,
    )

    _, sanitize_end = _function_span(server, "fn sanitize_filename(name: &str) -> String")
    server = server[:sanitize_end] + SERVER_TESTS + server[sanitize_end:]

    downloader = _replace_function(
        downloader,
        "pub fn format_filename(file_name: &str) -> String",
        DOWNLOADER_FORMAT,
    )
    first_tests = downloader.find("#[cfg(test)]")
    if first_tests < 0:
        downloader = downloader.rstrip() + "\n" + DOWNLOADER_TESTS + "\n"
    else:
        downloader = downloader[:first_tests] + DOWNLOADER_TESTS + "\n" + downloader[first_tests:]

    server_path.write_text(server, encoding="utf-8")
    downloader_path.write_text(downloader, encoding="utf-8")
    print("modified")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: modify_upstream.py <upstream-dir>", file=sys.stderr)
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
