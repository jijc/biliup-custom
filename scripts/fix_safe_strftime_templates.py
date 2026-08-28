#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "biliup-custom:safe-strftime-templates:v1"
NATIVE_REVIEW = 42
UTIL_REL = Path("crates/biliup-cli/src/server/common/util.rs")


class ModifyError(RuntimeError):
    pass


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ModifyError(f"{label}: expected 1 match, got {count}")
    return text.replace(old, new, 1)


def replace_one_shape(text: str, shapes: list[tuple[str, str]], label: str) -> str:
    matches = [(old, new) for old, new in shapes if old in text]
    if len(matches) != 1:
        raise ModifyError(f"{label}: expected exactly one supported upstream/custom shape, got {len(matches)}")
    old, new = matches[0]
    return text.replace(old, new, 1)


def modify(upstream: Path) -> None:
    path = upstream / UTIL_REL
    if not path.is_file():
        raise ModifyError(f"required upstream file missing: {path}")

    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print("already-modified")
        return
    if "StrftimeItems::new_lenient" in text or "safe_local_format" in text:
        print("native-review: upstream already has safe/lenient strftime formatting")
        raise SystemExit(NATIVE_REVIEW)

    helper = f'''// {MARKER}
// Chrono's DateTime::format(...).to_string() panics when a user-supplied
// strftime template contains an invalid specifier (for example `%日`).
// Upload titles/descriptions and Recorder filename templates are user data,
// so never allow one malformed `%` sequence to unwind a background task.
fn safe_local_format(datetime: &chrono::DateTime<Local>, template: &str) -> String {{
    match chrono::format::StrftimeItems::new(template).parse() {{
        Ok(items) => datetime.format_with_items(items.iter()).to_string(),
        Err(error) => {{
            tracing::warn!(
                template,
                error = ?error,
                "Invalid strftime template; preserving invalid specifiers literally"
            );
            datetime
                .format_with_items(chrono::format::StrftimeItems::new_lenient(template))
                .to_string()
        }}
    }}
}}

'''
    text = replace_once(text, "impl Recorder {\n", helper + "impl Recorder {\n", "Recorder impl")

    text = replace_one_shape(
        text,
        [
            (
                "            let base = t.format(&template).to_string();",
                "            let base = safe_local_format(&t, &template);",
            ),
            (
                '''            let rendered = render_record_date(&template, t.naive_local());
            let base = t.format(&rendered).to_string();''',
                '''            let rendered = render_record_date(&template, t.naive_local());
            let base = safe_local_format(&t, &rendered);''',
            ),
            (
                '''            let rendered = render_record_date(&template, t.naive_local());
            let rendered = strip_daily_sequence_token(&rendered);
            let base = t.format(&rendered).to_string();''',
                '''            let rendered = render_record_date(&template, t.naive_local());
            let rendered = strip_daily_sequence_token(&rendered);
            let base = safe_local_format(&t, &rendered);''',
            ),
        ],
        "generate_filename format",
    )

    text = replace_one_shape(
        text,
        [
            (
                '''        self.streamer_info
            .date
            .with_timezone(&Local)
            .format(&template)
            .to_string()
''',
                '''        let date = self.streamer_info.date.with_timezone(&Local);
        safe_local_format(&date, &template)
''',
            ),
            (
                '''        let t = self.streamer_info.date.with_timezone(&Local);
        let rendered = render_record_date(&template, t.naive_local());
        t.format(&rendered).to_string()
''',
                '''        let t = self.streamer_info.date.with_timezone(&Local);
        let rendered = render_record_date(&template, t.naive_local());
        safe_local_format(&t, &rendered)
''',
            ),
            (
                '''        let t = self.streamer_info.date.with_timezone(&Local);
        let rendered = render_record_date(&template, t.naive_local());
        let rendered = strip_daily_sequence_token(&rendered);
        t.format(&rendered).to_string()
''',
                '''        let t = self.streamer_info.date.with_timezone(&Local);
        let rendered = render_record_date(&template, t.naive_local());
        let rendered = strip_daily_sequence_token(&rendered);
        safe_local_format(&t, &rendered)
''',
            ),
        ],
        "format_filename",
    )

    text = replace_once(
        text,
        '''        self.streamer_info
            .date
            .with_timezone(&Local)
            .format(&self.raw_template())
            .to_string()
''',
        '''        let template = self.raw_template();
        let date = self.streamer_info.date.with_timezone(&Local);
        safe_local_format(&date, &template)
''',
        "format_title",
    )

    text = replace_once(
        text,
        '''        self.streamer_info
            .date
            .with_timezone(&Local)
            .format(&self.template_with(template))
            .to_string()
''',
        '''        let template = self.template_with(template);
        let date = self.streamer_info.date.with_timezone(&Local);
        safe_local_format(&date, &template)
''',
        "format description/template",
    )

    rust_tests = r'''
#[cfg(test)]
mod biliup_custom_safe_strftime_tests {
    use super::safe_local_format;
    use chrono::{Local, TimeZone};

    #[test]
    fn invalid_percent_template_does_not_panic() {
        let date = Local
            .with_ymd_and_hms(2026, 8, 28, 12, 34, 56)
            .single()
            .expect("test local datetime");

        assert_eq!(
            safe_local_format(&date, "[%Y年%m月%日]-[主播录播]"),
            "[2026年08月%日]-[主播录播]"
        );
    }

    #[test]
    fn valid_day_template_still_formats_normally() {
        let date = Local
            .with_ymd_and_hms(2026, 8, 28, 12, 34, 56)
            .single()
            .expect("test local datetime");

        assert_eq!(
            safe_local_format(&date, "[%Y年%m月%d日]-%H点%M分"),
            "[2026年08月28日]-12点34分"
        );
    }
}

'''
    test_anchor = "\n#[cfg(test)]\nmod tests {"
    if test_anchor in text:
        text = text.replace(test_anchor, "\n" + rust_tests + "#[cfg(test)]\nmod tests {", 1)
    else:
        text = text.rstrip() + "\n" + rust_tests

    path.write_text(text, encoding="utf-8")
    print("modified")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: fix_safe_strftime_templates.py <upstream-dir>", file=sys.stderr)
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
