import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ENDPOINTS_RS = r'''use tokio::fs;
use std::time::UNIX_EPOCH;

pub async fn get_videos() -> Result<Json<Vec<serde_json::Value>>, Response> {
    let media_extensions = [".mp4", ".flv", ".3gp", ".webm", ".mkv", ".ts"];
    let blacklist = ["next-env.d.ts"];
    let mut file_list = Vec::new();
    let mut index = 1;
    if let Ok(mut entries) = fs::read_dir(".").await {
        while let Ok(Some(entry)) = entries.next_entry().await {
            let path = entry.path();
            let file_name = entry.file_name().to_string_lossy().into_owned();
            if blacklist.contains(&file_name.as_str()) { continue; }
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

HISTORY_TSX = r'''export default function Home() {
  const { data: data } = useSWR<FileList[]>('/v1/videos', fetcher)
  const [fileName, setFileName] = useState<string>()
  return (
    <Players url={(process.env.NEXT_PUBLIC_API_SERVER ?? '') + '/static/' + fileName}></Players>
  )
}
'''

UPLOAD_MANAGER_TSX = r'''export default function Union() {
  const { data: fileList } = useSWR<FileList[]>('/v1/videos', fetcher)
  const data = fileList?.map(v => ({ label: v.name, value: v.name, key: v.key }))
  return <Transfer dataSource={data} />
}
'''


def make_upstream(root: Path) -> None:
    files = {
        "crates/biliup-cli/src/server/api/endpoints.rs": ENDPOINTS_RS,
        "crates/biliup-cli/src/server/router.rs": ROUTER_RS,
        "app/(app)/history/page.tsx": HISTORY_TSX,
        "app/(app)/upload-manager/page.tsx": UPLOAD_MANAGER_TSX,
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class RecordingsBrowserModifierTests(unittest.TestCase):
    def run_modifier(self, root: Path):
        return subprocess.run(
            [sys.executable, "scripts/fix_recordings_browser.py", str(root)],
            text=True,
            capture_output=True,
        )

    def test_recordings_tree_is_used_for_listing_upload_and_playback(self):
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
            self.assertIn("fileName?.split('/').map(encodeURIComponent).join('/') ?? ''", history)
            self.assertNotIn("encodeURI(fileName", history)
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

            endpoints = (root / "crates/biliup-cli/src/server/api/endpoints.rs").read_text(encoding="utf-8")
            router = (root / "crates/biliup-cli/src/server/router.rs").read_text(encoding="utf-8")
            self.assertEqual(endpoints.count("biliup-custom:recordings-browser:v1"), 1)
            self.assertEqual(router.count("biliup-custom:recordings-static:v1"), 1)


if __name__ == "__main__":
    unittest.main()
