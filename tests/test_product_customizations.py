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

ENDPOINTS_RS = r'''use tokio::fs;
use std::time::UNIX_EPOCH;

pub async fn put_streamers_endpoint(
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

pub async fn get_videos() -> Result<Json<Vec<serde_json::Value>>, Response> {
    let media_extensions = [".mp4", ".flv", ".3gp", ".webm", ".mkv", ".ts"];
    let blacklist = ["next-env.d.ts"];
    let mut file_list = Vec::new();
    let mut index = 1;
    if let Ok(mut entries) = fs::read_dir(".").await {
        while let Ok(Some(entry)) = entries.next_entry().await {
            let path = entry.path();
            let file_name = entry.file_name().to_string_lossy().into_owned();
            if blacklist.contains(&file_name.as_str()) {
                continue;
            }
            if let Some(ext) = path.extension().and_then(|e| e.to_str())
                && media_extensions.iter().any(|allowed| ext == allowed.trim_start_matches('.'))
                && let Ok(metadata) = entry.metadata().await
            {
                let mtime = metadata.modified().ok().and_then(|time| time.duration_since(UNIX_EPOCH).ok()).map(|d| d.as_secs()).unwrap_or(0);
                file_list.push(serde_json::json!({"key": index, "name": file_name, "updateTime": mtime, "size": metadata.len()}));
                index += 1;
            }
        }
    }
    Ok(Json(file_list))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PostUploads {
    files: Vec<String>,
    template_id: i64,
}

pub async fn post_uploads(
    State(config): State<Arc<RwLock<Config>>>,
    State(pool): State<ConnectionPool>,
    Json(json_data): Json<PostUploads>,
) -> Result<Json<serde_json::Value>, Response> {
    let upload_config = UploadStreamer::select().where_("id = ?").bind(json_data.template_id).fetch_optional(&pool).await.unwrap().unwrap();
    let root = std::env::current_dir().unwrap();
    let files = json_data.files.iter().map(|file| {
        crate::server::router::resolve_media_path(&root, file).map_err(|_| (StatusCode::BAD_REQUEST, "bad").into_response())
    }).collect::<Result<Vec<_>, Response>>()?;
    let _ = (config, upload_config, files);
    Ok(Json(serde_json::json!({})))
}
'''

ROUTER_RS = r'''use axum::Router;
use axum::routing::get;
use axum::extract::Path;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use axum::response::{IntoResponse, Response};
use tower::ServiceExt;
use tower_http::services::ServeFile;

const ALLOWED_MEDIA_EXTENSIONS: &[&str] = &["mp4", "flv", "3gp", "webm", "mkv", "ts"];

pub fn router(service_register: ServiceRegister) -> Router<()> {
    Router::new()
        .route("/v1/videos", get(get_videos))
        .route("/static/{path}", get(using_serve_file_from_a_route))
        .with_state(service_register)
}

async fn using_serve_file_from_a_route(
    axum::extract::Path(path): axum::extract::Path<String>,
    request: Request<Body>,
) -> Response {
    let root = match std::env::current_dir() {
        Ok(root) => root,
        Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    };
    let path = match resolve_media_path(&root, &path) {
        Ok(path) => path,
        Err(StaticPathError::Invalid) => return StatusCode::BAD_REQUEST.into_response(),
        Err(StaticPathError::NotFound) => return StatusCode::NOT_FOUND.into_response(),
    };
    ServeFile::new(path).oneshot(request).await.into_response()
}

#[derive(Debug, PartialEq, Eq)]
pub(crate) enum StaticPathError { Invalid, NotFound }

pub(crate) fn resolve_media_path(
    root: &std::path::Path,
    requested: &str,
) -> Result<std::path::PathBuf, StaticPathError> {
    use std::path::{Component, Path};
    if requested.is_empty() || requested.contains('/') || requested.contains('\\') {
        return Err(StaticPathError::Invalid);
    }
    let requested_path = Path::new(requested);
    let mut components = requested_path.components();
    if !matches!(components.next(), Some(Component::Normal(_))) || components.next().is_some() {
        return Err(StaticPathError::Invalid);
    }
    let allowed = requested_path.extension().and_then(|extension| extension.to_str()).is_some_and(|extension| {
        ALLOWED_MEDIA_EXTENSIONS.iter().any(|allowed| extension.eq_ignore_ascii_case(allowed))
    });
    if !allowed { return Err(StaticPathError::Invalid); }
    let root = root.canonicalize().map_err(|_| StaticPathError::NotFound)?;
    let canonical = root.join(requested).canonicalize().map_err(|_| StaticPathError::NotFound)?;
    if !canonical.starts_with(&root) || !canonical.is_file() { return Err(StaticPathError::Invalid); }
    Ok(canonical)
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

JOB_TSX = r'''  const columns = [
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
    <>
      <Content
        style={{
          paddingLeft: 12,
          paddingRight: 12,
        }}
      >
        <main>
          <JSONTree data={data} />
        </main>
      </Content>
    </>
)
'''

STREAMERS_TSX = r'''  const data: LiveStreamerEntity[] | undefined = streamers?.map(live => {
    let statusTag
    switch (live.status) {
      case 'Working':
        statusTag = <Tag color="red">直播中</Tag>
        break
      case 'Idle':
        statusTag = <Tag color="green">空闲</Tag>
        break
      case 'Pending':
        statusTag = <Tag color="indigo">检测中</Tag>
        break
      case 'OutOfSchedule':
        statusTag = <Tag color="green">非录播时间</Tag>
        break
      case 'TitleExcluded':
        statusTag = <Tag color="orange">标题已排除</Tag>
        break
      case 'Pause':
        statusTag = <Tag color="pink">暂停中</Tag>
        break
    }
    return { ...handleEntityPostprocessor(live), statusTag }
  })
'''

HISTORY_TSX = r'''export default function Home() {
  const { data: data } = useSWR<FileList[]>('/v1/videos', fetcher)
  const [fileName, setFileName] = useState<string>()
  const showDialog = (name: string) => {
    setVisible(true)
    setFileName(name)
  }
  return (
    <>
      <Table size="small" columns={columns} dataSource={data} />
      <Players url={(process.env.NEXT_PUBLIC_API_SERVER ?? '') + '/static/' + fileName}></Players>
    </>
  )
}
'''

UPLOAD_MANAGER_TSX = r'''export default function Union() {
  const { data: fileList } = useSWR<FileList[]>('/v1/videos', fetcher)
  const data = fileList?.map(v => {
    return {
      label: v.name,
      value: v.name,
      disabled: false,
      key: v.key,
    }
  })
  return <Transfer dataSource={data} />
}
'''


def make_upstream(root: Path) -> None:
    files = {
        "crates/biliup-cli/src/server/infrastructure/repositories.rs": REPOSITORIES_RS,
        "crates/biliup-cli/src/server/api/endpoints.rs": ENDPOINTS_RS,
        "crates/biliup-cli/src/server/router.rs": ROUTER_RS,
        "crates/biliup-cli/src/lib.rs": LIB_RS,
        "crates/biliup-cli/src/server/core/monitor.rs": MONITOR_RS,
        "app/(app)/job/page.tsx": JOB_TSX,
        "app/(app)/status/page.tsx": STATUS_TSX,
        "app/(app)/streamers/page.tsx": STREAMERS_TSX,
        "app/(app)/history/page.tsx": HISTORY_TSX,
        "app/(app)/upload-manager/page.tsx": UPLOAD_MANAGER_TSX,
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
            self.assertIn('is_streamer_paused(&service_register.pool, worker.id())', lib)
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

            self.assertIn("title: '名称',\n      width: 180,", job)
            self.assertIn("title: '标题',\n      width: 360,", job)
            self.assertIn("title: '封面',\n      width: 120,", job)
            self.assertIn("<main style={{ height: '100%' }}>", status)
            self.assertIn('.semi-layout-content > main > ul', status)
            self.assertIn('height: 100%', status)

    def test_streamer_status_tags_match_recording_meaning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            streamers = (root / "app/(app)/streamers/page.tsx").read_text(encoding="utf-8")
            self.assertIn('biliup-custom:streamer-status-tags:v1', streamers)
            self.assertIn('<Tag color="red">录制中</Tag>', streamers)
            self.assertIn('<Tag color="blue">空闲</Tag>', streamers)
            self.assertIn('<Tag color="green">检测中</Tag>', streamers)
            self.assertIn('<Tag color="grey">暂停中</Tag>', streamers)
            self.assertNotIn('<Tag color="red">直播中</Tag>', streamers)

    def test_recordings_subdirectories_are_the_single_media_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            endpoints = (root / "crates/biliup-cli/src/server/api/endpoints.rs").read_text(encoding="utf-8")
            router = (root / "crates/biliup-cli/src/server/router.rs").read_text(encoding="utf-8")
            history = (root / "app/(app)/history/page.tsx").read_text(encoding="utf-8")
            upload_manager = (root / "app/(app)/upload-manager/page.tsx").read_text(encoding="utf-8")

            self.assertIn('biliup-custom:recordings-browser:v1', endpoints)
            self.assertIn('const BILIUP_CUSTOM_RECORDINGS_ROOT: &str = "/recordings";', endpoints)
            self.assertIn('let mut pending_dirs = vec![recordings_root.to_path_buf()];', endpoints)
            self.assertNotIn('fs::read_dir(".")', endpoints)
            self.assertIn('resolve_recording_media_path', endpoints)
            self.assertNotIn('std::env::current_dir()', endpoints)

            self.assertIn('biliup-custom:recordings-static:v1', router)
            self.assertIn('.route("/static/{*path}"', router)
            self.assertIn('Path::new("/recordings")', router)
            self.assertIn('Component::ParentDir', router)
            self.assertIn('canonical.starts_with(&root)', router)

            self.assertIn('biliup-custom:recordings-history:v1', history)
            self.assertIn("'/static/' + encodeURI(fileName ?? '')", history)
            self.assertIn('biliup-custom:recordings-upload-picker:v1', upload_manager)
            self.assertIn('label: v.name', upload_manager)
            self.assertIn('value: v.name', upload_manager)

    def test_modifier_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            first = self.run_modifier(root)
            second = self.run_modifier(root)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

            repositories = (root / "crates/biliup-cli/src/server/infrastructure/repositories.rs").read_text(encoding="utf-8")
            streamers = (root / "app/(app)/streamers/page.tsx").read_text(encoding="utf-8")
            endpoints = (root / "crates/biliup-cli/src/server/api/endpoints.rs").read_text(encoding="utf-8")
            self.assertEqual(repositories.count("biliup-custom:persistent-pause:v1"), 1)
            self.assertEqual(streamers.count("biliup-custom:streamer-status-tags:v1"), 1)
            self.assertLessEqual(endpoints.count("biliup-custom:recordings-browser:v1"), 1)


if __name__ == "__main__":
    unittest.main()