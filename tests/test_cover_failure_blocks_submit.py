import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_submit_pipeline_recovery_modifier import make_upstream


class CoverFailureBlocksSubmitTests(unittest.TestCase):
    def run_modifier(self, script: str, root: Path):
        return subprocess.run(
            [sys.executable, script, str(root)],
            text=True,
            capture_output=True,
        )

    def prepare_recovery_upstream(self, root: Path):
        make_upstream(root)
        for script in (
            "scripts/fix_submit_timeout.py",
            "scripts/fix_submit_pipeline_recovery.py",
        ):
            result = self.run_modifier(script, root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cover_failure_never_clears_cover_and_continues_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_recovery_upstream(root)

            result = self.run_modifier("scripts/fix_cover_failure_safety.py", root)
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

    def test_modifier_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.prepare_recovery_upstream(root)

            first = self.run_modifier("scripts/fix_cover_failure_safety.py", root)
            second = self.run_modifier("scripts/fix_cover_failure_safety.py", root)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

            upload = (root / "crates/biliup-cli/src/server/common/upload.rs").read_text(
                encoding="utf-8"
            )
            self.assertEqual(upload.count("biliup-custom:cover-failure-safety:v1"), 1)


if __name__ == "__main__":
    unittest.main()
