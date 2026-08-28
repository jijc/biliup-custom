import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_submit_pipeline_recovery_modifier import make_upstream


class CoverFailureBlocksSubmitTests(unittest.TestCase):
    def test_cover_failure_never_clears_cover_and_continues_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)

            timeout = subprocess.run(
                [sys.executable, "scripts/fix_submit_timeout.py", str(root)],
                text=True,
                capture_output=True,
            )
            self.assertEqual(timeout.returncode, 0, timeout.stdout + timeout.stderr)

            recovery = subprocess.run(
                [sys.executable, "scripts/fix_submit_pipeline_recovery.py", str(root)],
                text=True,
                capture_output=True,
            )
            self.assertEqual(recovery.returncode, 0, recovery.stdout + recovery.stderr)

            upload = (root / "crates/biliup-cli/src/server/common/upload.rs").read_text(
                encoding="utf-8"
            )

            # Studio.cover is serialized as a normal String. Clearing it would
            # send an empty cover value, not omit the field. Cover failure must
            # therefore stop this submission before final submit.
            self.assertNotIn("studio.cover.clear()", upload)
            self.assertIn('"B站封面上传失败，停止本次投稿"', upload)
            self.assertIn('"B站封面上传超时，停止本次投稿"', upload)
            self.assertIn('"读取投稿封面失败，停止本次投稿"', upload)
            self.assertIn("return Err(error_stack::Report::new(AppError::Custom", upload)


if __name__ == "__main__":
    unittest.main()
