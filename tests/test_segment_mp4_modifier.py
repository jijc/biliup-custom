import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


DOWNLOAD_RS = r'''use crate::server::infrastructure::models::hook_step::process;
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
'''


def make_upstream(root: Path) -> None:
    server_common = root / "crates/biliup-cli/src/server/common"
    server_common.mkdir(parents=True, exist_ok=True)
    (server_common / "download.rs").write_text(DOWNLOAD_RS, encoding="utf-8")


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

            download = (
                root / "crates/biliup-cli/src/server/common/download.rs"
            ).read_text(encoding="utf-8")

            self.assertIn("biliup-custom:auto-mp4:v1", download)
            self.assertIn("async fn remux_completed_flv_to_mp4", download)
            self.assertIn('extension().and_then(|s| s.to_str()) != Some("flv")', download)
            self.assertIn('Command::new("ffmpeg")', download)
            self.assertIn('"-c",\n            "copy"', download)
            self.assertIn('"+faststart"', download)
            self.assertIn("remove_file(src).await", download)
            self.assertIn(
                "remux_completed_flv_to_mp4(&event.prev_file_path).await",
                download,
            )
            self.assertIn("*video_path = converted;", download)

    def test_modifier_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            first = self.run_modifier(root)
            second = self.run_modifier(root)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

            download = (
                root / "crates/biliup-cli/src/server/common/download.rs"
            ).read_text(encoding="utf-8")
            self.assertEqual(download.count("biliup-custom:auto-mp4:v1"), 1)


if __name__ == "__main__":
    unittest.main()
