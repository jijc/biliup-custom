from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "check_upstream_native.py"

CURRENT_UTIL = r'''fn sanitize_filename(name: &str) -> String {
    let mut out = String::with_capacity(name.len());
    for ch in name.chars() {
        match ch {
            '\\' | '/' | ':' | '*' | '?' | '"' | '<' | '>' | '|' => out.push('_'),
            c if c.is_control() => out.push('_'),
            _ => out.push(ch),
        }
    }
    out
}
'''


class NativeCapabilityProbeTests(unittest.TestCase):
    def run_probe(self, util_text: str = CURRENT_UTIL, config_text: str = "") -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            util = root / "crates/biliup-cli/src/server/common/util.rs"
            config = root / "crates/biliup-cli/src/server/config.rs"
            util.parent.mkdir(parents=True, exist_ok=True)
            config.parent.mkdir(parents=True, exist_ok=True)
            util.write_text(util_text, encoding="utf-8")
            config.write_text(config_text, encoding="utf-8")
            return subprocess.run(
                [sys.executable, str(PROBE), str(root)],
                text=True,
                capture_output=True,
                check=False,
            )

    def test_current_upstream_shape_is_patchable(self) -> None:
        result = self.run_probe()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("patchable:", result.stdout)

    def test_record_date_placeholder_triggers_migration_review(self) -> None:
        result = self.run_probe(CURRENT_UTIL + '\nconst X: &str = "{record_date}";\n')
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertIn("native-review: record_date", result.stdout)

    def test_path_aware_sanitizer_triggers_migration_review(self) -> None:
        util = CURRENT_UTIL.replace("'\\\\' | '/' | ':' |", "'\\\\' | ':' |")
        result = self.run_probe(util)
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertIn("native-review: slash sanitization changed", result.stdout)

    def test_user_configurable_recording_output_triggers_migration_review(self) -> None:
        result = self.run_probe(config_text="pub recording_output_dir: Option<String>,\n")
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertIn("native-review: recording output config", result.stdout)


if __name__ == "__main__":
    unittest.main()
