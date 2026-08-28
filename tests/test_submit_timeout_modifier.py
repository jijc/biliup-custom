import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


UPLOAD_RS = r'''use biliup::bilibili::{BiliBili, ResponseData, Studio};
use biliup::uploader::util::SubmitOption;
use error_stack::ResultExt;
use std::str::FromStr;
use tracing::{error, info};

pub enum AppError {
    Unknown,
    Custom(String),
}

type AppResult<T> = Result<T, error_stack::Report<AppError>>;

pub async fn submit_to_bilibili(
    bilibili: &BiliBili,
    studio: &Studio,
    submit_api: Option<&str>,
) -> AppResult<ResponseData> {
    let submit_option = match submit_api {
        Some(submit) => SubmitOption::from_str(submit).unwrap_or(SubmitOption::App),
        _ => SubmitOption::App,
    };

    let result = match submit_option {
        SubmitOption::BCutAndroid => bilibili
            .submit_by_bcut_android(studio, None)
            .await
            .change_context(AppError::Unknown)?,
        SubmitOption::Web => bilibili
            .submit_by_web(studio, None)
            .await
            .change_context(AppError::Unknown)?,
        _ => bilibili
            .submit_by_app(studio, None)
            .await
            .change_context(AppError::Unknown)?,
    };
    info!("Submit successful");
    Ok(result)
}
'''


def make_upstream(root: Path) -> None:
    path = root / "crates/biliup-cli/src/server/common/upload.rs"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(UPLOAD_RS, encoding="utf-8")


class SubmitTimeoutModifierTests(unittest.TestCase):
    def run_modifier(self, root: Path):
        return subprocess.run(
            [sys.executable, "scripts/fix_submit_timeout.py", str(root)],
            text=True,
            capture_output=True,
        )

    def test_final_submission_is_bounded_and_observable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            upload = (root / "crates/biliup-cli/src/server/common/upload.rs").read_text(
                encoding="utf-8"
            )
            self.assertIn("biliup-custom:submit-timeout:v1", upload)
            self.assertIn("const SUBMIT_TIMEOUT_SECS: u64 = 90;", upload)
            self.assertIn('"开始提交B站稿件"', upload)
            self.assertIn("part_count = studio.videos.len()", upload)
            self.assertIn("tokio::time::timeout", upload)
            self.assertIn("Duration::from_secs(SUBMIT_TIMEOUT_SECS)", upload)
            self.assertIn('"B站最终投稿请求超时"', upload)
            self.assertIn("AppError::Custom", upload)
            self.assertIn('info!("Submit successful")', upload)

            # Preserve all existing submit choices. This fix must not invent
            # the explicitly-not-supported automatic App -> Web fallback.
            self.assertEqual(upload.count("submit_by_bcut_android(studio, None)"), 1)
            self.assertEqual(upload.count("submit_by_web(studio, None)"), 1)
            self.assertEqual(upload.count("submit_by_app(studio, None)"), 1)

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
            self.assertEqual(upload.count("biliup-custom:submit-timeout:v1"), 1)
            self.assertEqual(upload.count("const SUBMIT_TIMEOUT_SECS: u64 = 90;"), 1)


if __name__ == "__main__":
    unittest.main()
