import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_daily_seq_wxpusher_modifier import make_upstream


class ModifierInteractionTests(unittest.TestCase):
    def test_daily_sequence_modifier_keeps_missing_template_file_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            result = subprocess.run(
                [sys.executable, "scripts/add_daily_seq_wxpusher.py", str(root)],
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            upload = (root / "crates/biliup-cli/src/server/common/upload.rs").read_text(
                encoding="utf-8"
            )
            self.assertIn("run_postprocessor: bool", upload)
            self.assertIn("biliup-custom:preserve-files-without-upload-template:v1", upload)
            self.assertIn("if run_postprocessor {", upload)
            self.assertIn("return execute_postprocessor(paths, ctx).await;", upload)
            self.assertIn(
                "No upload template is bound; preserving local recording files and skipping postprocessor",
                upload,
            )


if __name__ == "__main__":
    unittest.main()
