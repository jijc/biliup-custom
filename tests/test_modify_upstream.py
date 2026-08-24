import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SERVER_UTIL = r'''use chrono::{Duration, Local};

pub struct Recorder;

impl Recorder {
    pub fn filename_template(&self) -> String {
        let raw = "demo".to_string();
        sanitize_filename(&raw)
    }

    fn template_with(&self, template: &str) -> String {
        template
            .replace("{streamer}", "主播")
            .replace("{title}", "标题")
            .replace("{url}", "url")
    }

    pub fn generate_filename(&self, suffix: &str) -> String {
        let template = self.filename_template();
        Local::now().format(&template).to_string()
    }

    pub fn format_filename(&self) -> String {
        let template = self.filename_template();
        Local::now().format(&template).to_string()
    }
}

fn sanitize_filename(name: &str) -> String {
    let mut out = String::new();
    for ch in name.chars() {
        match ch {
            '\\' | '/' | ':' | '*' | '?' | '"' | '<' | '>' | '|' => out.push('_'),
            _ => out.push(ch),
        }
    }
    out
}
'''

DOWNLOADER_UTIL = r'''use chrono::{DateTime, Local};

pub fn format_filename(file_name: &str) -> String {
    let local: DateTime<Local> = Local::now();
    local.format(file_name).to_string()
}
'''

CONFIG = r'''pub struct Config {
    pub filename_prefix: Option<String>,
}
'''


def make_upstream(root: Path) -> None:
    server = root / "crates/biliup-cli/src/server/common"
    downloader = root / "crates/biliup/src/downloader"
    config_dir = root / "crates/biliup-cli/src/server"
    server.mkdir(parents=True, exist_ok=True)
    downloader.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)
    (server / "util.rs").write_text(SERVER_UTIL, encoding="utf-8")
    (downloader / "util.rs").write_text(DOWNLOADER_UTIL, encoding="utf-8")
    (config_dir / "config.rs").write_text(CONFIG, encoding="utf-8")


class ModifyUpstreamTests(unittest.TestCase):
    def run_modifier(self, root: Path):
        return subprocess.run(
            [sys.executable, "scripts/modify_upstream.py", str(root)],
            text=True,
            capture_output=True,
        )

    def test_current_upstream_shape_gets_path_and_four_am_support(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            server = (root / "crates/biliup-cli/src/server/common/util.rs").read_text(encoding="utf-8")
            downloader = (root / "crates/biliup/src/downloader/util.rs").read_text(encoding="utf-8")

            self.assertIn('const RECORDING_ROOT: &str = "/recordings/";', server)
            self.assertIn('sanitize_component(&self.streamer_info.name)', server)
            self.assertIn('render_record_date(&template', server)
            self.assertIn('pub fn format_filename_at(', downloader)
            self.assertIn('chrono::Duration::hours(4)', downloader)

    def test_modifier_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            first = self.run_modifier(root)
            second = self.run_modifier(root)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

            server = (root / "crates/biliup-cli/src/server/common/util.rs").read_text(encoding="utf-8")
            downloader = (root / "crates/biliup/src/downloader/util.rs").read_text(encoding="utf-8")
            self.assertEqual(server.count("biliup-custom:auto-modifier:v1"), 1)
            self.assertEqual(downloader.count("biliup-custom:auto-modifier:v1"), 1)

    def test_upstream_record_date_stops_for_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            path = root / "crates/biliup-cli/src/server/common/util.rs"
            path.write_text(
                path.read_text(encoding="utf-8") + "\n// upstream record_date support\n",
                encoding="utf-8",
            )
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
            self.assertIn("native-review:", result.stdout)

    def test_native_output_directory_setting_stops_for_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            path = root / "crates/biliup-cli/src/server/config.rs"
            path.write_text(
                "pub struct Config { pub recording_output_dir: Option<String> }\n",
                encoding="utf-8",
            )
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
            self.assertIn("native-review:", result.stdout)


if __name__ == "__main__":
    unittest.main()
