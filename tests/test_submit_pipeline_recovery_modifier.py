import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


UPLOAD_RS = r'''use crate::server::common::util::Recorder;
use crate::server::errors::{AppError, AppResult};
use crate::server::infrastructure::models::upload_streamer::UploadStreamer;
use biliup::bilibili::{BiliBili, ResponseData, Studio};
use biliup::uploader::util::SubmitOption;
use error_stack::ResultExt;
use std::str::FromStr;
use tracing::{error, info};

// biliup-custom:submit-timeout:v1
const SUBMIT_TIMEOUT_SECS: u64 = 90;

pub async fn submit_to_bilibili(
    bilibili: &BiliBili,
    studio: &Studio,
    submit_api: Option<&str>,
) -> AppResult<ResponseData> {
    let submit_option = match submit_api {
        Some(submit) => SubmitOption::from_str(submit).unwrap_or(SubmitOption::App),
        _ => SubmitOption::App,
    };
    let submit_api_name = match &submit_option {
        SubmitOption::BCutAndroid => "bcut_android",
        SubmitOption::Web => "web",
        _ => "app",
    };

    info!(
        submit_api = submit_api_name,
        title = %studio.title,
        part_count = studio.videos.len(),
        timeout_secs = SUBMIT_TIMEOUT_SECS,
        "开始提交B站稿件"
    );

    let submit_future = async {
        match submit_option {
            SubmitOption::BCutAndroid => bilibili
                .submit_by_bcut_android(studio, None)
                .await
                .change_context(AppError::Unknown),
            SubmitOption::Web => bilibili
                .submit_by_web(studio, None)
                .await
                .change_context(AppError::Unknown),
            _ => bilibili
                .submit_by_app(studio, None)
                .await
                .change_context(AppError::Unknown),
        }
    };

    let result = match tokio::time::timeout(
        std::time::Duration::from_secs(SUBMIT_TIMEOUT_SECS),
        submit_future,
    )
    .await
    {
        Ok(result) => result?,
        Err(_) => {
            error!(
                submit_api = submit_api_name,
                title = %studio.title,
                part_count = studio.videos.len(),
                timeout_secs = SUBMIT_TIMEOUT_SECS,
                "B站最终投稿请求超时"
            );
            return Err(error_stack::Report::new(AppError::Custom(format!(
                "B站最终投稿请求超时（{}秒），文件上传已完成但稿件未确认提交成功",
                SUBMIT_TIMEOUT_SECS
            ))));
        }
    };

    info!("Submit successful");
    Ok(result)
}

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
    if !studio.cover.is_empty()
        && let Ok(c) = &std::fs::read(&studio.cover).inspect_err(|e| error!(e=?e))
        && let Ok(url) = bilibili.cover_up(c).await.inspect_err(|e| error!(e=?e))
    {
        studio.cover = url;
    }

    Ok(studio)
}
'''


def make_upstream(root: Path) -> None:
    path = root / "crates/biliup-cli/src/server/common/upload.rs"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(UPLOAD_RS, encoding="utf-8")


class SubmitPipelineRecoveryModifierTests(unittest.TestCase):
    def run_modifier(self, root: Path):
        return subprocess.run(
            [sys.executable, "scripts/fix_submit_pipeline_recovery.py", str(root)],
            text=True,
            capture_output=True,
        )

    def test_bounds_cover_stage_and_reuses_uploaded_parts_for_one_safe_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            upload = (root / "crates/biliup-cli/src/server/common/upload.rs").read_text(
                encoding="utf-8"
            )

            self.assertIn("biliup-custom:submit-pipeline-recovery:v1", upload)

            # The cover step sits between the last media Upload completed and
            # final submit, so it must not be allowed to hang silently.
            self.assertIn("const COVER_UPLOAD_TIMEOUT_SECS: u64 = 30;", upload)
            self.assertIn('"开始上传B站封面"', upload)
            self.assertIn("tokio::time::timeout", upload)
            self.assertIn("bilibili.cover_up", upload)
            self.assertIn("studio.cover.clear()", upload)
            self.assertIn('"B站封面上传超时，将继续无封面投稿"', upload)
            self.assertIn('"B站封面上传失败，将继续无封面投稿"', upload)
            self.assertIn('"B站稿件信息构建完成"', upload)

            # If final submit times out, query Bilibili first. Only when the
            # remote archive list was successfully checked and no matching new
            # draft exists may we reuse the already-uploaded Video tokens for
            # exactly one final-submit retry.
            self.assertIn("confirm_recent_submission", upload)
            self.assertIn('"is_pubing,pubed,not_pubed"', upload)
            self.assertIn("Some(2)", upload)
            self.assertIn("successful_checks >= 2", upload)
            self.assertIn("for attempt in 1..=2", upload)
            self.assertIn("Ok(Err(err)) => return Err(err)", upload)
            self.assertIn('"未发现新稿件，复用已上传视频信息重试最终投稿一次"', upload)
            self.assertIn('"远端稿件状态无法确认，不自动重试"', upload)
            self.assertIn('"已从B站稿件列表确认稿件存在"', upload)

            # Do not create App->Web fallback or duplicate submit branches.
            self.assertEqual(upload.count("submit_by_bcut_android(studio, None)"), 1)
            self.assertEqual(upload.count("submit_by_web(studio, None)"), 1)
            self.assertEqual(upload.count("submit_by_app(studio, None)"), 1)

    def test_modifier_is_wired_into_all_build_paths(self):
        paths = [
            Path("scripts/apply-and-test.sh"),
            Path("scripts/build-image.sh"),
            Path(".github/workflows/docker-validate.yml"),
            Path(".github/workflows/publish.yml"),
        ]
        for path in paths:
            text = path.read_text(encoding="utf-8")
            with self.subTest(path=str(path)):
                self.assertIn("fix_submit_pipeline_recovery.py", text)
                self.assertLess(
                    text.index("fix_submit_timeout.py"),
                    text.index("fix_submit_pipeline_recovery.py"),
                )

    def test_modifier_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            first = self.run_modifier(root)
            second = self.run_modifier(root)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

            upload = (root / "crates/biliup-cli/src/server/common/upload.rs").read_text(
                encoding="utf-8"
            )
            self.assertEqual(upload.count("biliup-custom:submit-pipeline-recovery:v1"), 1)


if __name__ == "__main__":
    unittest.main()
