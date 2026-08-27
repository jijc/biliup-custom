import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


UPLOAD_RS = r'''use crate::server::infrastructure::models::hook_step::process;
use std::path::PathBuf;

async fn process_without_upload<F>(
    rx: Inspect<Receiver<SegmentInfo>, F>,
    ctx: &Context,
) -> AppResult<()>
where
    F: FnMut(&SegmentInfo),
{
    let mut paths = Vec::new();
    pin!(rx);
    while let Some(event) = rx.next().await {
        paths.extend(segment_paths(&event));
    }
    execute_postprocessor(paths, ctx).await
}

async fn process_actor_branch() -> AppResult<()> {
    let result = match ctx.upload_config() {
        Some(config) if config.is_noop_uploader() => {
            process_without_upload(inspect, &ctx).await
        }
        Some(config) => process_with_upload(inspect, &ctx, config).await,
        None => {
            let mut paths = Vec::new();
            pin!(inspect);
            while let Some(event) = inspect.next().await {
                paths.extend(segment_paths(&event));
            }
            // 无上传配置时，直接执行后处理
            execute_postprocessor(paths, &ctx).await
        }
    };
    result
}
'''


def make_upstream(root: Path) -> None:
    server_common = root / "crates/biliup-cli/src/server/common"
    server_common.mkdir(parents=True, exist_ok=True)
    (server_common / "upload.rs").write_text(UPLOAD_RS, encoding="utf-8")


class SegmentMp4ModifierTests(unittest.TestCase):
    def run_modifier(self, root: Path):
        return subprocess.run(
            [sys.executable, "scripts/restore_segment_mp4.py", str(root)],
            text=True,
            capture_output=True,
        )

    def test_completed_flv_segments_are_remuxed_immediately_and_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            upload = (
                root / "crates/biliup-cli/src/server/common/upload.rs"
            ).read_text(encoding="utf-8")

            self.assertIn("biliup-custom:auto-mp4:v1", upload)
            self.assertIn("async fn remux_completed_flv_to_mp4", upload)
            self.assertIn('extension().and_then(|s| s.to_str()) != Some("flv")', upload)
            self.assertIn('Command::new("ffmpeg")', upload)
            self.assertIn('"-c",\n            "copy"', upload)
            self.assertIn('"+faststart"', upload)
            self.assertIn("remove_file(src).await", upload)
            self.assertIn(
                "remux_completed_flv_to_mp4(&event.prev_file_path).await",
                upload,
            )
            self.assertIn("*video_path = converted;", upload)

    def test_no_upload_template_uses_the_same_mp4_pipeline_without_deleting_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            upload = (
                root / "crates/biliup-cli/src/server/common/upload.rs"
            ).read_text(encoding="utf-8")

            self.assertIn(
                "None => process_without_upload(inspect, &ctx, false).await,",
                upload,
            )
            self.assertIn(
                "process_without_upload(inspect, &ctx, true).await",
                upload,
            )
            self.assertIn("biliup-custom:preserve-files-without-upload-template:v1", upload)
            self.assertNotIn(
                "// 无上传配置时，直接执行后处理",
                upload,
            )

    def test_modifier_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            first = self.run_modifier(root)
            second = self.run_modifier(root)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

            upload = (
                root / "crates/biliup-cli/src/server/common/upload.rs"
            ).read_text(encoding="utf-8")
            self.assertEqual(upload.count("biliup-custom:auto-mp4:v1"), 1)
            self.assertEqual(upload.count("biliup-custom:preserve-files-without-upload-template:v1"), 1)


if __name__ == "__main__":
    unittest.main()
