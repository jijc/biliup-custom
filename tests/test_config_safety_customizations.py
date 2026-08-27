import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from test_product_customizations import make_upstream


OVERRIDE_MODAL_TSX = r'''const OverrideModal = () => {
  const handleOk = async () => {
    let values = await api.current?.validate()
    const entityFields = new Set([
      'id',
      'url',
      'remark',
      'filename',
      'split_time',
      'split_size',
      'upload_id',
      'status',
      'format',
      'time_range',
      'excluded_keywords',
      'preprocessor',
      'segment_processor',
      'downloaded_processor',
      'postprocessor',
      'opt_args',
      'override',
    ])

    if (values) {
      Object.keys(values).forEach(key => {
        if (!entityFields.has(key)) {
          delete values[key]
        }
      })
      await onOk(values)
    }
  }
}
'''


UPLOAD_RS = r'''use tracing::{error, info};

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

impl UploaderActor {
    async fn handle_message(&mut self, msg: UploaderMessage) {
        match msg {
            UploaderMessage::SegmentEvent(rx, ctx) => {
                let inspect = rx.inspect(|f| {
                    let file = f.prev_file_path.display().to_string();
                    info!(file, "Insert file");
                });
                let result = match ctx.upload_config() {
                    Some(config) if config.is_noop_uploader() => {
                        info!(
                            uploader = ?config.uploader,
                            "Skipping upload because uploader is Noop"
                        );
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
                if let Err(e) = &result {
                    error!("Process segment event failed: {}", e);
                }
            }
        }
    }
}
'''


def make_safety_upstream(root: Path) -> None:
    make_upstream(root)
    files = {
        "app/ui/OverrideModal.tsx": OVERRIDE_MODAL_TSX,
        "crates/biliup-cli/src/server/common/upload.rs": UPLOAD_RS,
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class ConfigSafetyCustomizationTests(unittest.TestCase):
    def run_modifier(self, root: Path):
        return subprocess.run(
            [sys.executable, "scripts/apply_product_customizations.py", str(root)],
            text=True,
            capture_output=True,
        )

    def test_override_modal_preserves_current_streamer_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_safety_upstream(root)
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            modal = (root / "app/ui/OverrideModal.tsx").read_text(encoding="utf-8")
            self.assertIn("biliup-custom:preserve-streamer-fields:v1", modal)
            self.assertIn("      'filename_prefix',", modal)
            self.assertIn("      'upload_streamers_id',", modal)
            self.assertNotIn("      'filename',", modal)
            self.assertNotIn("      'upload_id',", modal)

    def test_missing_upload_template_preserves_recording_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_safety_upstream(root)
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            upload = (root / "crates/biliup-cli/src/server/common/upload.rs").read_text(encoding="utf-8")
            self.assertIn("biliup-custom:preserve-files-without-upload-template:v1", upload)
            self.assertIn("No upload template is bound; preserving local recording files", upload)
            self.assertEqual(upload.count("execute_postprocessor(paths, &ctx).await"), 1)
            self.assertIn("Some(config) if config.is_noop_uploader()", upload)
            self.assertIn("process_without_upload(inspect, &ctx).await", upload)


if __name__ == "__main__":
    unittest.main()
