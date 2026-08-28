import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


UTIL_RS = r'''use chrono::{Duration, Local};
use tracing::{error, info};

pub struct Recorder {
    pub filename_prefix: Option<String>,
    pub streamer_info: StreamerInfo,
}

impl Recorder {
    fn raw_template(&self) -> String {
        self.filename_prefix.clone().unwrap_or_else(|| "%Y-%m-%d".to_string())
    }

    fn template_with(&self, template: &str) -> String {
        template
            .replace("{streamer}", &self.streamer_info.name)
            .replace("{title}", &self.streamer_info.title)
            .replace("{url}", &self.streamer_info.url)
    }

    pub fn generate_filename(&self, suffix: &str) -> String {
        let template = self.filename_template();
        let mut t = Local::now();
        loop {
            let base = t.format(&template).to_string();
            if !self.exists_with_suffix(&base, suffix) {
                return base;
            }
            t += Duration::seconds(1);
        }
    }

    pub fn format_filename(&self) -> String {
        let template = self.filename_template();
        self.streamer_info
            .date
            .with_timezone(&Local)
            .format(&template)
            .to_string()
    }

    pub fn format_title(&self) -> String {
        self.streamer_info
            .date
            .with_timezone(&Local)
            .format(&self.raw_template())
            .to_string()
    }

    pub fn format(&self, template: &str) -> String {
        self.streamer_info
            .date
            .with_timezone(&Local)
            .format(&self.template_with(template))
            .to_string()
    }
}
'''


class SafeStrftimeTemplatesModifierTests(unittest.TestCase):
    def run_modifier(self, root: Path):
        return subprocess.run(
            [sys.executable, "scripts/fix_safe_strftime_templates.py", str(root)],
            text=True,
            capture_output=True,
        )

    def make_upstream(self, root: Path):
        util = root / "crates/biliup-cli/src/server/common/util.rs"
        util.parent.mkdir(parents=True, exist_ok=True)
        util.write_text(UTIL_RS, encoding="utf-8")
        return util

    def test_invalid_strftime_is_made_non_panicking_at_the_shared_recorder_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            util = self.make_upstream(root)

            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            text = util.read_text(encoding="utf-8")

            self.assertIn("biliup-custom:safe-strftime-templates:v1", text)
            self.assertIn("StrftimeItems::new(template).parse()", text)
            self.assertIn("StrftimeItems::new_lenient(template)", text)
            self.assertIn("safe_local_format", text)
            self.assertIn("Invalid strftime template", text)
            self.assertNotIn("t.format(&template).to_string()", text)
            self.assertNotIn(".format(&self.raw_template())", text)
            self.assertNotIn(".format(&self.template_with(template))", text)

    def test_modifier_adds_a_real_rust_regression_for_percent_followed_by_chinese_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            util = self.make_upstream(root)

            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            text = util.read_text(encoding="utf-8")

            self.assertIn("biliup_custom_safe_strftime_tests", text)
            self.assertIn("invalid_percent_template_does_not_panic", text)
            self.assertIn("%日", text)
            self.assertIn("%d日", text)

    def test_modifier_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            util = self.make_upstream(root)

            first = self.run_modifier(root)
            second = self.run_modifier(root)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            text = util.read_text(encoding="utf-8")
            self.assertEqual(text.count("biliup-custom:safe-strftime-templates:v1"), 1)

    def test_all_runtime_build_paths_apply_safe_strftime_before_upload_guards(self):
        paths = (
            Path("scripts/apply-and-test.sh"),
            Path("scripts/build-image.sh"),
            Path(".github/workflows/docker-validate.yml"),
            Path(".github/workflows/publish.yml"),
        )
        for path in paths:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=str(path)):
                self.assertIn("fix_safe_strftime_templates.py", text)
                self.assertLess(
                    text.index("fix_safe_strftime_templates.py"),
                    text.index("fix_manual_submit_guard.py"),
                )


if __name__ == "__main__":
    unittest.main()
