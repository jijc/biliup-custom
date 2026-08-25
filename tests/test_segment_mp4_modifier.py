import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SERVER_UTIL = r'''use chrono::{Duration, Local};

pub struct Recorder;

impl Recorder {
    pub fn filename_template(&self) -> String {
        let raw = "demo".to_string();
        sanitize_filename(&raw)
    }

    fn template_with(&self, template: &str) -> String {
        template
            .replace("{streamer}", "主播")
            .replace("{title}", "标题")
            .replace("{url}", "url")
    }

    pub fn generate_filename(&self, suffix: &str) -> String {
        let template = self.filename_template();
        Local::now().format(&template).to_string()
    }

    pub fn format_filename(&self) -> String {
        let template = self.filename_template();
        Local::now().format(&template).to_string()
    }
}

fn sanitize_filename(name: &str) -> String {
    let mut out = String::new();
    for ch in name.chars() {
        match ch {
            '\\' | '/' | ':' | '*' | '?' | '"' | '<' | '>' | '|' => out.push('_'),
            _ => out.push(ch),
        }
    }
    out
}
'''

DOWNLOADER_UTIL = r'''use chrono::{DateTime, Local};

pub fn format_filename(file_name: &str) -> String {
    let local: DateTime<Local> = Local::now();
    local.format(file_name).to_string()
}
'''

CONFIG = r'''pub struct Config {
    pub filename_prefix: Option<String>,
}
'''

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
    downloader = root / "crates/biliup/src/downloader"
    config_dir = root / "crates/biliup-cli/src/server"
    server_common.mkdir(parents=True, exist_ok=True)
    downloader.mkdir(parents=True, exist_ok=True)
    config_dir.mkdir(parents=True, exist_ok=True)

    (server_common / "util.rs").write_text(SERVER_UTIL, encoding="utf-8")
    (server_common / "download.rs").write_text(DOWNLOAD_RS, encoding="utf-8")
    (downloader / "util.rs").write_text(DOWNLOADER_UTIL, encoding="utf-8")
    (config_dir / "config.rs").write_text(CONFIG, encoding="utf-8")


class SegmentMp4ModifierTests(unittest.TestCase):
    def test_completed_flv_segments_are_remuxed_immediately_and_safely(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            result = subprocess.run(
                [sys.executable, "scripts/modify_upstream.py", str(root)],
                text=True,
                capture_output=True,
            )
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
            self.assertIn("paths.push(converted);", download)


if __name__ == "__main__":
    unittest.main()
