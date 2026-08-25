import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORIES_RS = r'''use crate::server::errors::{AppError, AppResult};
use crate::server::infrastructure::connection_pool::ConnectionPool;

pub async fn del_streamer(pool: &ConnectionPool, id: i64) -> AppResult<LiveStreamer> {
    let streamer = get_streamer(pool, id).await?;
    streamer.clone().delete(pool).await.change_context(AppError::Unknown)?;
    Ok(streamer)
}

pub async fn get_all_streamer(pool: &ConnectionPool) -> AppResult<Vec<LiveStreamer>> {
    LiveStreamer::select().fetch_all(pool).await.change_context(AppError::Unknown)
}
'''

ENDPOINTS_RS = r'''pub async fn put_streamers_endpoint(
    State(service_register): State<ServiceRegister>,
    State(managers): State<Arc<DownloadManager>>,
    State(pool): State<ConnectionPool>,
    Json(payload): Json<LiveStreamer>,
) -> Result<Json<LiveStreamer>, Response> {
    let streamer = payload.update_all_fields(&pool).await.unwrap();
    let id = streamer.id;
    managers.del_room(id).await;
    let upload_config = get_upload_config(&pool, id).await.unwrap();
    managers.add_room(service_register.worker(streamer.clone(), upload_config)).await;
    Ok(Json(streamer))
}

pub async fn pause_streamers_endpoint(
    State(managers): State<Arc<DownloadManager>>,
    Path(id): Path<i64>,
) -> Result<Json<()>, Response> {
    let worker = managers.get_room_by_id(id).await;
    if let Some(w) = worker {
        let worker_status = w.downloader_status.read().unwrap().clone();
        match worker_status {
            WorkerStatus::Working(_) | WorkerStatus::Pending | WorkerStatus::Idle => {
                w.change_status(Stage::Download, WorkerStatus::Pause).await;
                managers.make_waker(id).await;
            }
            WorkerStatus::Pause => {
                w.change_status(Stage::Download, WorkerStatus::Idle).await;
                managers.wake_waker(id).await;
            }
        }
    }
    Ok(Json(()))
}
'''

LIB_RS = r'''async fn import_config_streamers(path: &Path, service_register: &ServiceRegister) -> AppResult<()> {
    let streamers = vec![];
    for (remark, url, streamer, global_uploader) in streamers {
        let upload_config = None;
        let live_streamer = repositories::upsert_live_streamer_by_url(&service_register.pool, payload).await?;
        service_register.managers.add_room(service_register.worker(live_streamer.clone(), upload_config)).await;
    }
    Ok(())
}

async fn import_database_streamers(service_register: &ServiceRegister) -> AppResult<()> {
    let streamers = repositories::get_all_streamer(&service_register.pool).await?;
    for live_streamer in streamers {
        let upload_config = repositories::get_upload_config(&service_register.pool, live_streamer.id).await?;
        service_register.managers.add_room(service_register.worker(live_streamer.clone(), upload_config)).await;
    }
    Ok(())
}
'''

MONITOR_RS = r'''impl Monitor {
    pub async fn add(
        self: &Arc<Self>,
        worker: Arc<Worker>,
    ) -> Option<Arc<dyn LivePlugin + Send + Sync>> {
        let (send, recv) = oneshot::channel();
        let msg = ActorMessage::Add(send, worker.clone());
        let _ = self.sender.send(msg).await;
        let plugin = recv.await.expect("Actor task has been killed")?;
        self.rooms_handle_pool(plugin.clone());
        Some(plugin)
    }
}
'''

JOB_TSX = r'''const columns = [
  {
    title: '名称',
    dataIndex: 'name',
  },
  {
    title: '标题',
    dataIndex: 'title',
  },
  {
    title: '链接',
    dataIndex: 'url',
  },
  {
    title: '封面',
    dataIndex: 'live_cover_path',
  },
  {
    title: '更新日期',
    dataIndex: 'date',
  },
]
'''

STATUS_TSX = r'''return (
  <Content style={{ paddingLeft: 12, paddingRight: 12 }}>
    <main>
      <JSONTree data={data} />
    </main>
  </Content>
)
'''


def make_upstream(root: Path) -> None:
    files = {
        "crates/biliup-cli/src/server/infrastructure/repositories.rs": REPOSITORIES_RS,
        "crates/biliup-cli/src/server/api/endpoints.rs": ENDPOINTS_RS,
        "crates/biliup-cli/src/lib.rs": LIB_RS,
        "crates/biliup-cli/src/server/core/monitor.rs": MONITOR_RS,
        "app/(app)/job/page.tsx": JOB_TSX,
        "app/(app)/status/page.tsx": STATUS_TSX,
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class ProductCustomizationTests(unittest.TestCase):
    def run_modifier(self, root: Path):
        return subprocess.run(
            [sys.executable, "scripts/apply_product_customizations.py", str(root)],
            text=True,
            capture_output=True,
        )

    def test_pause_state_is_persisted_and_restored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            repositories = (root / "crates/biliup-cli/src/server/infrastructure/repositories.rs").read_text(encoding="utf-8")
            endpoints = (root / "crates/biliup-cli/src/server/api/endpoints.rs").read_text(encoding="utf-8")
            lib = (root / "crates/biliup-cli/src/lib.rs").read_text(encoding="utf-8")
            monitor = (root / "crates/biliup-cli/src/server/core/monitor.rs").read_text(encoding="utf-8")

            self.assertIn('BILIUP_CUSTOM_PAUSED_STREAMER_KEY', repositories)
            self.assertIn('set_streamer_paused', repositories)
            self.assertIn('is_streamer_paused', repositories)
            self.assertIn('State(pool): State<ConnectionPool>', endpoints)
            self.assertIn('set_streamer_paused(&pool, id, true)', endpoints)
            self.assertIn('set_streamer_paused(&pool, id, false)', endpoints)
            self.assertIn('is_streamer_paused(&service_register.pool, live_streamer.id)', lib)
            self.assertIn('WorkerStatus::Pause', lib)
            self.assertIn('initially_paused', monitor)
            self.assertIn('self.make_waker(worker.id()).await', monitor)

    def test_live_history_columns_and_task_platform_height(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            job = (root / "app/(app)/job/page.tsx").read_text(encoding="utf-8")
            status = (root / "app/(app)/status/page.tsx").read_text(encoding="utf-8")

            self.assertIn("title: '名称',\n    width: 180,", job)
            self.assertIn("title: '标题',\n    width: 360,", job)
            self.assertIn("title: '封面',\n    width: 120,", job)
            self.assertIn("<main style={{ height: '100%' }}>", status)
            self.assertIn('.semi-layout-content > main > ul', status)
            self.assertIn('height: 100%', status)

    def test_modifier_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            first = self.run_modifier(root)
            second = self.run_modifier(root)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

            repositories = (root / "crates/biliup-cli/src/server/infrastructure/repositories.rs").read_text(encoding="utf-8")
            self.assertEqual(repositories.count("biliup-custom:persistent-pause:v1"), 1)


if __name__ == "__main__":
    unittest.main()
