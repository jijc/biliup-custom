import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MODIFIER = "scripts/add_daily_seq_wxpusher.py"


def make_upstream(root: Path) -> None:
    common = root / "crates/biliup-cli/src/server/common"
    common.mkdir(parents=True, exist_ok=True)
    (common / "mod.rs").write_text(
        "pub mod download;\npub mod upload;\npub mod util;\n",
        encoding="utf-8",
    )
    (common / "download.rs").write_text(
        r'''use crate::server::common::recording_policy;

impl DownloadTask {
    pub(self) async fn execute(
        &self,
        ctx: &Context,
        sender: Sender<UploaderMessage>,
        plugin: Arc<dyn LivePlugin + Send + Sync>,
        rooms_handle: Arc<Monitor>,
    ) -> AppResult<()> {
        let mut retry_count = 0;
        let max_retries = 3;
        let result = loop {
            let components = self.download().await;
            if self.token.is_cancelled() {
                break components;
            }
            match plugin.check_stream(live_request(ctx.worker())).await {
                Ok(LiveStatus::Live { stream: next_stream }) => {
                    stream = *next_stream;
                    retry_count = 0;
                }
                Ok(LiveStatus::Offline) => {
                    retry_count += 1;
                }
                Err(e) => {
                    retry_count += 1;
                }
            }
            if retry_count >= max_retries {
                break components;
            }
        };
        Ok(())
    }
}

pub async fn start_download_workflow(
    downloader: Arc<dyn LivePlugin + Send + Sync>,
    ctx: Context,
    sender: Sender<UploaderMessage>,
    rooms_handle: Arc<Monitor>,
) {
    let task = Arc::new(DownloadTask::new(downloader_runtime(
        ctx.config().downloader,
        ctx.live_stream(),
    )));
    ctx.change_status(Stage::Download, WorkerStatus::Working(task.clone()))
        .await;
    let _ = task.execute(&ctx, sender, downloader, rooms_handle).await;
}
''',
        encoding="utf-8",
    )
    (common / "upload.rs").write_text(
        r'''// biliup-custom:auto-mp4:v1
async fn remux_completed_flv_to_mp4(src: &std::path::Path) -> AppResult<std::path::PathBuf> {
    Ok(src.with_extension("mp4"))
}

pub async fn process_with_upload<F>(
    rx: Inspect<Receiver<SegmentInfo>, F>,
    ctx: &Context,
    upload_config: &UploadStreamer,
) -> AppResult<()>
where
    F: FnMut(&SegmentInfo),
{
    let upload_context = initialize_upload_context(&ctx.config(), &ctx.stateless_client(), upload_config).await?;
    let segment_processors: Vec<HookStep> = ctx.live_streamer().segment_processor.clone().unwrap_or_default();
    let uploaded_videos = pipeline_upload_videos(rx, &upload_context, &segment_processors).await?;
    if !uploaded_videos.videos.is_empty() {
        let mut recorder = ctx.recorder(ctx.streamer_info().clone()).clone();
        recorder.filename_prefix = upload_config.title.clone();
        let studio = build_studio(&upload_config, &upload_context.bilibili, uploaded_videos.videos, &recorder).await?;
        let submit_api = ctx.config().submit_api.clone();
        submit_to_bilibili(&upload_context.bilibili, &studio, submit_api.as_deref()).await?;
    }
    Ok(())
}

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
        let mut event_paths = segment_paths(&event);
        if let Some(video_path) = event_paths.first_mut() {
            match remux_completed_flv_to_mp4(&event.prev_file_path).await {
                Ok(converted) => {
                    *video_path = converted;
                }
                Err(e) => {
                    error!(file = ?event.prev_file_path, error = ?e, "自动转换 MP4 失败，保留原 FLV");
                }
            }
        }
        paths.extend(event_paths);
    }
    execute_postprocessor(paths, ctx).await
}

async fn pipeline_upload_videos<F>(
    rx: Inspect<Receiver<SegmentInfo>, F>,
    context: &UploadContext,
    segment_processors: &[HookStep],
) -> AppResult<UploadedVideos>
where
    F: FnMut(&SegmentInfo),
{
    let mut uploaded = UploadedVideos::default();
    pin!(rx);
    while let Some(event) = rx.next().await {
        let mut paths = segment_paths(&event);
        if !segment_processors.is_empty()
            && let Err(e) = process_video_paths(&mut paths, segment_processors).await
        {
            continue;
        }
        let upload_path = paths.first().cloned().unwrap_or_else(|| event.prev_file_path.clone());
        match upload_single_file(&upload_path, context).await {
            Ok(video) => {
                uploaded.videos.push(video);
                uploaded.paths.extend(paths);
            }
            Err(e) => {
                error!(file = ?upload_path, "upload_single_file failed, skipping segment: {:?}", e);
            }
        }
    }
    Ok(uploaded)
}
''',
        encoding="utf-8",
    )


class DailySeqWxPusherModifierTests(unittest.TestCase):
    def run_modifier(self, root: Path):
        return subprocess.run(
            [sys.executable, MODIFIER, str(root)],
            text=True,
            capture_output=True,
        )

    def test_sequence_is_assigned_after_mp4_conversion_and_before_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            upload = (root / "crates/biliup-cli/src/server/common/upload.rs").read_text(encoding="utf-8")
            self.assertIn('const DAILY_SEQ_TOKEN: &str = "{daily_seq}";', upload)
            self.assertIn("finalize_daily_sequence(&mut paths).await", upload)
            self.assertLess(
                upload.index("remux_completed_flv_to_mp4"),
                upload.index("finalize_daily_sequence(&mut paths).await"),
            )
            self.assertIn("pipeline_upload_videos(rx, &upload_context, &segment_processors, ctx)", upload)

    def test_wxpusher_hooks_cover_required_events_without_config_db_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            common = root / "crates/biliup-cli/src/server/common"
            mod_rs = (common / "mod.rs").read_text(encoding="utf-8")
            download = (common / "download.rs").read_text(encoding="utf-8")
            upload = (common / "upload.rs").read_text(encoding="utf-8")
            wx = (common / "wxpusher.rs").read_text(encoding="utf-8")
            self.assertIn("pub mod wxpusher;", mod_rs)
            self.assertIn("WXPUSHER_APP_TOKEN", wx)
            self.assertIn("WXPUSHER_UIDS", wx)
            self.assertIn("开播", download)
            self.assertIn("停播", download)
            self.assertIn("上传完成", upload)
            self.assertIn("上传错误", upload)
            self.assertIn("转换失败", upload)


if __name__ == "__main__":
    unittest.main()
