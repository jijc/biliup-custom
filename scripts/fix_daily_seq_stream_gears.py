#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "// biliup-custom:daily-seq-stream-gears:v1"
NATIVE_REVIEW = 42
COMMON_REL = Path("crates/biliup-cli/src/server/common")
UTIL_REL = COMMON_REL / "util.rs"
STREAM_GEARS_REL = Path("crates/biliup-cli/src/server/core/downloader/stream_gears.rs")


class ModifyError(RuntimeError):
    pass


RUST_TESTS = r'''
#[cfg(test)]
mod biliup_custom_stream_gears_filename_tests {
    use super::*;
    use crate::server::infrastructure::models::StreamerInfo;
    use chrono::Utc;

    #[test]
    fn stream_gears_recording_template_hides_daily_sequence_placeholder() {
        let recorder = Recorder::new(
            Some(
                "/recordings/主播/{record_date}/{daily_seq}-[%Y年%m月%d日-%H时%M分%S秒][{title}]"
                    .to_string(),
            ),
            StreamerInfo::new(
                "主播",
                "https://example.com/live",
                "测试直播",
                Utc::now(),
                "",
            ),
        );
        let template = recorder.filename_template();
        assert!(!template.contains("{daily_seq}"));
        assert!(template.contains("{record_date}"));
        assert!(template.contains("[测试直播]"));
    }
}
'''


def modify(upstream: Path) -> None:
    util_path = upstream / UTIL_REL
    stream_gears_path = upstream / STREAM_GEARS_REL
    for path in (util_path, stream_gears_path):
        if not path.is_file():
            raise ModifyError(f"required upstream file missing: {path}")

    util = util_path.read_text(encoding="utf-8")
    stream_gears = stream_gears_path.read_text(encoding="utf-8")

    if MARKER in util:
        print("already-modified")
        return

    if "biliup-custom:daily-seq-temp-clean:v1" not in util:
        raise ModifyError("daily sequence temp cleanup modifier must run first")
    if "fn strip_daily_sequence_token(template: &str) -> String" not in util:
        raise ModifyError("daily sequence token stripping helper missing")

    # This is the real active-recording entrypoint used by the default
    # StreamGears downloader. If upstream changes it, stop the build so this
    # customization is reviewed instead of silently regressing again.
    stream_entry = "download_config.recorder.filename_template()"
    if stream_entry not in stream_gears:
        print("native-review: StreamGears filename entrypoint changed upstream")
        raise SystemExit(NATIVE_REVIEW)

    old = "        sanitize_recording_template(&raw)\n    }"
    new = "        strip_daily_sequence_token(&sanitize_recording_template(&raw))\n    }"
    count = util.count(old)
    if count != 1:
        print(f"native-review: Recorder::filename_template shape changed (anchor count={count})")
        raise SystemExit(NATIVE_REVIEW)

    util = util.replace(old, new, 1)
    util = MARKER + "\n" + util.rstrip() + "\n" + RUST_TESTS + "\n"
    util_path.write_text(util, encoding="utf-8")
    print("modified")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: fix_daily_seq_stream_gears.py <upstream-dir>", file=sys.stderr)
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
