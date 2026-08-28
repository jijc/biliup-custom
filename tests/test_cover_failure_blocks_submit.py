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

            for script in (
                "scripts/fix_submit_timeout.py",
                "scripts/fix_submit_pipeline_recovery.py",
                "scripts/fix_cover_failure_safety.py",
            ):
                result = subprocess.run(
                    [sys.executable, script, str(root)],
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            upload = (root / "crates/biliup-cli/src/server/common/upload.rs").read_text(
                encoding="utf-8"
            )

            # Studio.cover is serialized as a normal String. Clearing it would
            # send an empty cover value, not omit the field. Cover failure must
            # therefore stop this submission before final submit.
            self.assertNotIn("studio.cover.clear()", upload)
            self.assertIn("biliup-custom:cover-failure-safety:v1", upload)
            self.assertIn('"B站封面上传失败，停止本次投稿"', upload)
            self.assertIn('"B站封面上传超时，停止本次投稿"', upload)
            self.assertIn('"读取投稿封面失败，停止本次投稿"', upload)
            self.assertIn("return Err(error_stack::Report::new(AppError::Custom", upload)

    def test_modifier_is_wired_after_submit_recovery_everywhere(self):
        for path in (
            Path("scripts/apply-and-test.sh"),
            Path("scripts/build-image.sh"),
            Path(".github/workflows/docker-validate.yml"),
            Path(".github/workflows/publish.yml"),
        ):
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=str(path)):
                self.assertIn("fix_cover_failure_safety.py", text)
                self.assertLess(
                    text.index("fix_submit_pipeline_recovery.py"),
                    text.index("fix_cover_failure_safety.py"),
                )


if __name__ == "__main__":
    unittest.main()
