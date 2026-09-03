import unittest

from scripts.add_daily_seq_wxpusher import _modify_download


NEW_UPSTREAM_DOWNLOAD = r'''use crate::server::common::recording_policy;
use crate::server::common::util::FileValidator;

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
        let mut last_committed = 0;
        let result = loop {
            let components = self.download().await;
            if self.token.is_cancelled() {
                break components;
            }
            match plugin.check_stream(live_request(ctx.worker())).await {
                Ok(LiveStatus::Live { stream: next_stream }) => {
                    stream = *next_stream;
                    let progressed = committed > last_committed;
                    last_committed = committed;
                    if progressed {
                        retry_count = 0;
                    } else {
                        retry_count += 1;
                    }
                }
                Ok(LiveStatus::Offline) => {
                    retry_count += 1;
                }
                Err(e) => {
                    retry_count += 1;
                }
            }
            if retry_count >= max_retries {
                warn!("stopping");
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
'''


class WxPusherNewUpstreamRetryCompatTests(unittest.TestCase):
    def test_live_branch_marks_last_check_online_before_progress_split(self):
        modified = _modify_download(NEW_UPSTREAM_DOWNLOAD)
        live_start = modified.index("Ok(LiveStatus::Live")
        offline_start = modified.index("Ok(LiveStatus::Offline)", live_start)
        live_block = modified[live_start:offline_start]

        self.assertIn("if progressed", live_block)
        self.assertIn("last_check_offline = false;", live_block)
        self.assertLess(
            live_block.index("last_check_offline = false;"),
            live_block.index("if progressed"),
            "the whole Live branch must mark the check as online before either retry path",
        )


if __name__ == "__main__":
    unittest.main()
