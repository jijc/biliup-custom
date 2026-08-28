import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ENDPOINTS_RS = r'''use crate::server::common::upload::{build_studio, submit_to_bilibili, upload};
use crate::server::errors::AppError;
use error_stack::Report;
use std::time::{Duration, UNIX_EPOCH};
use tracing::info;

pub async fn post_uploads() {
    // biliup-custom:manual-upload-feedback:v1
    tokio::spawn(async move {
        let result = async {
            let (bilibili, videos) = upload(
                upload_config
                    .user_cookie
                    .as_deref()
                    .unwrap_or("cookies.json"),
                None,
                line,
                &files,
                limit as usize,
            )
            .await?;
            if !videos.is_empty() {
                let recorder = Recorder::new(
                    upload_config.title.clone(),
                    StreamerInfo::new(
                        &upload_config.template_name,
                        "stream_title",
                        "",
                        Utc::now(),
                        "",
                    ),
                );
                let studio = build_studio(&upload_config, &bilibili, videos, &recorder).await?;
                submit_to_bilibili(&bilibili, &studio, submit_api.as_deref()).await?;
                info!(template_id = upload_config.id, "通过页面上传成功");
            }
            Ok::<_, Report<AppError>>(())
        }
        .await;
        if result.is_err() {
            tracing::error!(template_id, file_count, "页面上传失败");
        }
    });
}
'''

UPLOAD_RS = r'''pub async fn submit_to_bilibili() {
    // official auto-upload submission semantics sentinel
}

pub(crate) async fn build_studio() {
    // official cover/build semantics sentinel
}
'''


class ManualSubmitGuardScopeTests(unittest.TestCase):
    def run_modifier(self, root: Path):
        return subprocess.run(
            [sys.executable, "scripts/fix_manual_submit_guard.py", str(root)],
            text=True,
            capture_output=True,
        )

    def make_upstream(self, root: Path):
        endpoints = root / "crates/biliup-cli/src/server/api/endpoints.rs"
        upload = root / "crates/biliup-cli/src/server/common/upload.rs"
        endpoints.parent.mkdir(parents=True, exist_ok=True)
        upload.parent.mkdir(parents=True, exist_ok=True)
        endpoints.write_text(ENDPOINTS_RS, encoding="utf-8")
        upload.write_text(UPLOAD_RS, encoding="utf-8")

    def test_manual_guard_wraps_only_page_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_upstream(root)
            before_upload = (root / "crates/biliup-cli/src/server/common/upload.rs").read_text(
                encoding="utf-8"
            )

            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            endpoints = (root / "crates/biliup-cli/src/server/api/endpoints.rs").read_text(
                encoding="utf-8"
            )
            after_upload = (root / "crates/biliup-cli/src/server/common/upload.rs").read_text(
                encoding="utf-8"
            )

            self.assertEqual(after_upload, before_upload)
            self.assertIn("biliup-custom:manual-submit-guard:v1", endpoints)
            self.assertIn("MANUAL_BUILD_STUDIO_TIMEOUT_SECS", endpoints)
            self.assertIn("MANUAL_SUBMIT_TIMEOUT_SECS", endpoints)
            self.assertIn("tokio::time::timeout", endpoints)
            self.assertIn('"手动上传：媒体文件上传完成，开始构建B站稿件信息"', endpoints)
            self.assertIn('"手动上传：B站稿件信息构建超时"', endpoints)
            self.assertIn('"手动上传：开始最终投稿"', endpoints)
            self.assertIn('"手动上传：B站最终投稿请求超时"', endpoints)
            self.assertIn('"通过页面上传成功"', endpoints)

    def test_runtime_build_paths_restore_official_auto_submit(self):
        paths = (
            Path("scripts/apply-and-test.sh"),
            Path("scripts/build-image.sh"),
            Path(".github/workflows/docker-validate.yml"),
            Path(".github/workflows/publish.yml"),
        )
        for path in paths:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=str(path)):
                self.assertNotIn("fix_submit_timeout.py", text)
                self.assertNotIn("fix_submit_pipeline_recovery.py", text)
                self.assertIn("fix_manual_submit_guard.py", text)
                self.assertLess(
                    text.index("fix_manual_upload_feedback.py"),
                    text.index("fix_manual_submit_guard.py"),
                )

    def test_modifier_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_upstream(root)
            first = self.run_modifier(root)
            second = self.run_modifier(root)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

            endpoints = (root / "crates/biliup-cli/src/server/api/endpoints.rs").read_text(
                encoding="utf-8"
            )
            self.assertEqual(endpoints.count("biliup-custom:manual-submit-guard:v1"), 1)


if __name__ == "__main__":
    unittest.main()
