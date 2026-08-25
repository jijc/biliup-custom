import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MODIFIER = "scripts/fix_daily_seq_stream_gears.py"


def make_upstream(root: Path) -> None:
    common = root / "crates/biliup-cli/src/server/common"
    downloader = root / "crates/biliup-cli/src/server/core/downloader"
    common.mkdir(parents=True, exist_ok=True)
    downloader.mkdir(parents=True, exist_ok=True)

    (common / "util.rs").write_text(
        r'''// biliup-custom:auto-modifier:v1
// biliup-custom:daily-seq-temp-clean:v1
fn strip_daily_sequence_token(template: &str) -> String {
    template
        .replace("{daily_seq}-", "")
        .replace("{daily_seq}", "")
}

impl Recorder {
    pub fn filename_template(&self) -> String {
        let raw = if let Some(prefix) = &self.filename_prefix {
            self.template_with(prefix)
        } else {
            format!("{}%Y-%m-%dT%H_%M_%S", self.streamer_info.name)
        };
        sanitize_recording_template(&raw)
    }
}
''',
        encoding="utf-8",
    )
    (downloader / "stream_gears.rs").write_text(
        r'''async fn start_download(download_config: DownloadConfig) {
    let file_name = download_config.recorder.filename_template();
    let file = LifecycleFile::with_hook(&file_name, "flv", hook);
}
''',
        encoding="utf-8",
    )


class DailySeqStreamGearsModifierTests(unittest.TestCase):
    def run_modifier(self, root: Path):
        return subprocess.run(
            [sys.executable, MODIFIER, str(root)],
            text=True,
            capture_output=True,
        )

    def test_stream_gears_filename_template_strips_daily_seq(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            util = (root / "crates/biliup-cli/src/server/common/util.rs").read_text(encoding="utf-8")
            self.assertIn(
                "strip_daily_sequence_token(&sanitize_recording_template(&raw))",
                util,
            )
            self.assertNotIn("        sanitize_recording_template(&raw)\n    }", util)

    def test_requires_actual_stream_gears_entrypoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            stream_gears = root / "crates/biliup-cli/src/server/core/downloader/stream_gears.rs"
            stream_gears.write_text("fn unrelated() {}\n", encoding="utf-8")
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
            self.assertIn("native-review", result.stdout)


if __name__ == "__main__":
    unittest.main()
