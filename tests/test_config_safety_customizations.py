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
      const cleanValues = removeCircularReferences(values)
      await onOk(cleanValues)
    }
  }
}
'''


TEMPLATE_MODAL_TSX = r'''const TemplateModal = () => {
  const handleOk = async () => {
    let values = await api.current?.validate()
    values = {
      ...values,
      remark: values?.remark?.trim(),
      url: values?.url?.trim(),
      format: values?.format?.trim(),
      time_range: JSON.stringify(values?.time_range?.map((date: Date) => date.toISOString())),
    }
    await onOk(values)
  }
}
'''


API_STREAMER_TS = r'''export interface LiveStreamerEntity {
  id: number;
  url: string;
  remark: string;
  filename: string;
  split_time?: number;
  split_size?: number;
  upload_id?: number;
  status?: string;
  upload_status?: string;
  statusTag?: React.ReactNode;
  format?: string;
  time_range?: string | Date[];
  excluded_keywords?: string[];
  preprocessor?: Record<'run', string>[];
  segment_processor?: Record<'run', string>[];
  downloaded_processor?: Record<'run', string>[];
  postprocessor?: (Record<'run' | 'mv', string> | 'rm')[];
  opt_args?: string[];
  override?: Record<string, any>;
}

export interface StudioEntity {
  id: number;
  template_name: string;
  user_cookie: string;
  up_selection_reply: number;
  up_close_reply: number;
  up_close_danmu: number;
}
'''


UPLOAD_TEMPLATE_EDIT_TSX = r'''let uploadStreamers = {
  ...data,
  interaction: (data.up_close_danmu === 1 ? ['up_close_danmu'] : [])
    .concat(data.up_close_reply === 1 ? ['up_close_reply'] : [])
    .concat(data.up_selection_reply === 1 ? ['up_selection_reply'] : []),
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
        "app/ui/TemplateModal.tsx": TEMPLATE_MODAL_TSX,
        "app/lib/api-streamer.ts": API_STREAMER_TS,
        "app/(app)/upload-manager/edit/page.tsx": UPLOAD_TEMPLATE_EDIT_TSX,
        "crates/biliup-cli/src/server/common/upload.rs": UPLOAD_RS,
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class ConfigSafetyCustomizationTests(unittest.TestCase):
    def run_script(self, script: str, root: Path):
        return subprocess.run(
            [sys.executable, f"scripts/{script}", str(root)],
            text=True,
            capture_output=True,
        )

    def test_override_modal_preserves_current_and_future_streamer_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_safety_upstream(root)
            result = self.run_script("fix_override_streamer_fields.py", root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            modal = (root / "app/ui/OverrideModal.tsx").read_text(encoding="utf-8")
            self.assertIn("biliup-custom:preserve-streamer-fields:v1", modal)
            self.assertIn("      'filename_prefix',", modal)
            self.assertIn("      'upload_streamers_id',", modal)
            self.assertNotIn("      'filename',", modal)
            self.assertNotIn("      'upload_id',", modal)
            self.assertNotIn("      'split_time',", modal)
            self.assertNotIn("      'split_size',", modal)
            self.assertIn("await onOk({ id: entity?.id, override: cleanValues.override })", modal)
            self.assertNotIn("await onOk({ ...entity, ...cleanValues })", modal)

    def test_optional_streamer_fields_can_be_explicitly_cleared(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_safety_upstream(root)
            result = self.run_script("fix_override_streamer_fields.py", root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            modal = (root / "app/ui/TemplateModal.tsx").read_text(encoding="utf-8")
            self.assertIn("biliup-custom:clearable-streamer-fields:v1", modal)
            self.assertIn("'filename_prefix'", modal)
            self.assertIn("'upload_streamers_id'", modal)
            self.assertIn("'format'", modal)
            self.assertIn("'time_range'", modal)
            self.assertIn("clearableValues[field] = null", modal)

    def test_live_streamer_types_match_backend_field_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_safety_upstream(root)
            result = self.run_script("fix_override_streamer_fields.py", root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            api = (root / "app/lib/api-streamer.ts").read_text(encoding="utf-8")
            self.assertIn("filename_prefix?: string | null;", api)
            self.assertIn("upload_streamers_id?: number | null;", api)
            self.assertNotIn("filename: string;", api)
            self.assertNotIn("upload_id?: number;", api)
            self.assertNotIn("split_time?: number;", api)
            self.assertNotIn("split_size?: number;", api)
            self.assertIn("up_selection_reply: boolean | number;", api)
            self.assertIn("up_close_reply: boolean | number;", api)
            self.assertIn("up_close_danmu: boolean | number;", api)

    def test_upload_template_boolean_flags_round_trip_correctly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_safety_upstream(root)
            result = self.run_script("fix_override_streamer_fields.py", root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            edit = (root / "app/(app)/upload-manager/edit/page.tsx").read_text(encoding="utf-8")
            self.assertIn("Boolean(data.up_close_danmu)", edit)
            self.assertIn("Boolean(data.up_close_reply)", edit)
            self.assertIn("Boolean(data.up_selection_reply)", edit)
            self.assertNotIn("up_close_danmu === 1", edit)
            self.assertNotIn("up_close_reply === 1", edit)
            self.assertNotIn("up_selection_reply === 1", edit)

    def test_missing_upload_template_preserves_recording_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_safety_upstream(root)
            result = self.run_script("restore_segment_mp4.py", root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            upload = (root / "crates/biliup-cli/src/server/common/upload.rs").read_text(encoding="utf-8")
            self.assertIn("biliup-custom:preserve-files-without-upload-template:v1", upload)
            self.assertIn("No upload template is bound; preserving local recording files", upload)
            self.assertEqual(upload.count("execute_postprocessor(paths, ctx).await"), 1)
            self.assertIn("Some(config) if config.is_noop_uploader()", upload)
            self.assertIn("process_without_upload(inspect, &ctx, true).await", upload)
            self.assertIn("None => process_without_upload(inspect, &ctx, false).await,", upload)


if __name__ == "__main__":
    unittest.main()
