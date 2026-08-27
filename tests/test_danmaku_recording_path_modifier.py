import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SERVER_UTIL = r'''use chrono::{Duration, Local};

const RECORDING_ROOT: &str = "/recordings/";

fn sanitize_filename(name: &str) -> String {
    name.replace('/', "_")
}

fn sanitize_component(value: &str) -> String {
    value.replace('/', "_")
}

fn sanitize_recording_template(raw: &str) -> String {
    if !raw.starts_with(RECORDING_ROOT) {
        return sanitize_filename(raw);
    }
    let tail = &raw[RECORDING_ROOT.len()..];
    let clean = tail.split('/').map(sanitize_component).collect::<Vec<_>>().join("/");
    format!("{RECORDING_ROOT}{clean}")
}

fn strip_daily_sequence_token(template: &str) -> String {
    template
        .replace("{daily_seq}-", "")
        .replace("{daily_seq}", "")
}

pub fn danmaku_filename_template(filename_prefix: Option<&str>, name: &str) -> String {
    let template = filename_prefix
        .map(|prefix| prefix.replace("{streamer}", name))
        .unwrap_or_else(|| format!("{}%Y-%m-%dT%H_%M_%S", name));
    sanitize_filename(&template)
}
'''

DANMAKU_CLIENT = r'''use std::path::{Path, PathBuf};

fn format_output_path(template: &Path) -> PathBuf {
    let now = chrono::Local::now();
    let path_str = template.to_string_lossy();
    let formatted = now.format(&path_str).to_string();
    PathBuf::from(formatted).with_extension("xml")
}
'''


def make_upstream(root: Path) -> None:
    server = root / "crates/biliup-cli/src/server/common"
    danmaku = root / "crates/danmaku/src"
    server.mkdir(parents=True, exist_ok=True)
    danmaku.mkdir(parents=True, exist_ok=True)
    (server / "util.rs").write_text(SERVER_UTIL, encoding="utf-8")
    (danmaku / "client.rs").write_text(DANMAKU_CLIENT, encoding="utf-8")


class DanmakuRecordingPathModifierTests(unittest.TestCase):
    def run_modifier(self, root: Path):
        return subprocess.run(
            [sys.executable, "scripts/fix_danmaku_recording_path.py", str(root)],
            text=True,
            capture_output=True,
        )

    def test_nested_recordings_path_logical_day_and_temp_name_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            server = (root / "crates/biliup-cli/src/server/common/util.rs").read_text(encoding="utf-8")
            client = (root / "crates/danmaku/src/client.rs").read_text(encoding="utf-8")

            self.assertIn(
                "strip_daily_sequence_token(&sanitize_recording_template(&template))",
                server,
            )
            self.assertIn('replace("{record_date}", &record_date)', client)
            self.assertIn("chrono::Duration::hours(4)", client)
            self.assertIn("biliup_custom_danmaku_path_tests", client)

    def test_modifier_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            first = self.run_modifier(root)
            second = self.run_modifier(root)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

            server = (root / "crates/biliup-cli/src/server/common/util.rs").read_text(encoding="utf-8")
            client = (root / "crates/danmaku/src/client.rs").read_text(encoding="utf-8")
            self.assertEqual(server.count("biliup-custom:danmaku-recording-path:v1"), 1)
            self.assertEqual(client.count("biliup-custom:danmaku-recording-path:v1"), 1)

    def test_native_record_date_support_stops_for_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            client = root / "crates/danmaku/src/client.rs"
            client.write_text(client.read_text(encoding="utf-8") + "\n// upstream record_date support\n", encoding="utf-8")
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
            self.assertIn("native-review:", result.stdout)


if __name__ == "__main__":
    unittest.main()
