import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_daily_seq_wxpusher_modifier import make_upstream
from test_submit_timeout_modifier import UPLOAD_RS as SUBMIT_BASE_RS


BUILD_STUDIO_RS = r'''

pub(crate) async fn build_studio(
    upload_config: &UploadStreamer,
    bilibili: &BiliBili,
    videos: Vec<Video>,
    recorder: &Recorder,
) -> AppResult<Studio> {
    let mut studio: Studio = Studio::builder()
        .desc(recorder.format(&upload_config.description.clone().unwrap_or_default()))
        .cover(upload_config.cover_path.clone().unwrap_or_default())
        .title(recorder.format_title())
        .videos(videos)
        .build();
    // 处理封面上传
    if !studio.cover.is_empty()
        && let Ok(c) = &std::fs::read(&studio.cover).inspect_err(|e| error!(e=?e))
        && let Ok(url) = bilibili.cover_up(c).await.inspect_err(|e| error!(e=?e))
    {
        studio.cover = url;
    };

    Ok(studio)
}
'''


class ModifierInteractionTests(unittest.TestCase):
    def test_final_upload_pipeline_keeps_missing_template_file_safety(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            for script in (
                "scripts/add_daily_seq_wxpusher.py",
                "scripts/fix_missing_upload_template_safety.py",
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
            self.assertIn("run_postprocessor: bool", upload)
            self.assertIn("biliup-custom:preserve-files-without-upload-template:v1", upload)
            self.assertIn("if run_postprocessor {", upload)
            self.assertIn("return execute_postprocessor(paths, ctx).await;", upload)
            self.assertIn(
                "No upload template is bound; preserving local recording files and skipping postprocessor",
                upload,
            )

    def test_submit_timeout_then_recovery_preserves_timeout_and_adds_safe_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            upload_path = root / "crates/biliup-cli/src/server/common/upload.rs"
            upload_path.parent.mkdir(parents=True, exist_ok=True)
            upload_path.write_text(SUBMIT_BASE_RS + BUILD_STUDIO_RS, encoding="utf-8")

            for script in (
                "scripts/fix_submit_timeout.py",
                "scripts/fix_submit_pipeline_recovery.py",
            ):
                result = subprocess.run(
                    [sys.executable, script, str(root)],
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            upload = upload_path.read_text(encoding="utf-8")
            self.assertIn("biliup-custom:submit-timeout:v1", upload)
            self.assertIn("biliup-custom:submit-pipeline-recovery:v1", upload)
            self.assertEqual(upload.count("const SUBMIT_TIMEOUT_SECS: u64 = 90;"), 1)
            self.assertEqual(upload.count("const COVER_UPLOAD_TIMEOUT_SECS: u64 = 30;"), 1)
            self.assertIn("for attempt in 1..=2", upload)
            self.assertIn("confirm_recent_submission", upload)
            self.assertIn("successful_checks >= 2", upload)
            self.assertIn("Ok(Err(err)) => return Err(err)", upload)
            self.assertEqual(upload.count("submit_by_bcut_android(studio, None)"), 1)
            self.assertEqual(upload.count("submit_by_web(studio, None)"), 1)
            self.assertEqual(upload.count("submit_by_app(studio, None)"), 1)


if __name__ == "__main__":
    unittest.main()
