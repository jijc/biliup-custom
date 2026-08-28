import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


UPLOAD_MANAGER_TSX = r'''export default function Union() {
  const [visibleModal, setVisibleModal] = useState(false)
  const [selectFiles, setSelectFiles] = useState<(string | number)[]>([])
  const [selectEntity, setSelectEntity] = useState<StudioEntity>()
  const [transferData, setTransferData] = useState<(string | number)[]>([])

  const handleOk = async () => {
    await sendRequest('/v1/uploads', {
      arg: {
        files: selectFiles.map(String),
        template_id: selectEntity?.id,
      },
    })
    setVisibleModal(false)
  }
}
'''


ENDPOINTS_RS = r'''pub async fn post_uploads(
    State(config): State<Arc<RwLock<Config>>>,
    State(pool): State<ConnectionPool>,
    Json(json_data): Json<PostUploads>,
) -> Result<Json<serde_json::Value>, Response> {
    let upload_config = UploadStreamer::select()
        .where_("id = ?")
        .bind(json_data.template_id)
        .fetch_optional(&pool)
        .await
        .change_context(AppError::Unknown)
        .map_err(report_to_response)?
        .ok_or_else(|| (StatusCode::NOT_FOUND, "上传模板不存在").into_response())?;
    if upload_config.is_noop_uploader() {
        info!(
            uploader = ?upload_config.uploader,
            "Skipping page upload because uploader is Noop"
        );
        return Ok(Json(json!({})));
    }

    let files = json_data.files.iter().map(PathBuf::from).collect::<Vec<_>>();
    if files.is_empty() {
        return Err((StatusCode::BAD_REQUEST, "至少选择一个媒体文件").into_response());
    }
    info!("通过页面开始上传");
    tokio::spawn(async move {
        let result = async {
            Ok::<_, Report<AppError>>(())
        }
        .await;
        if result.is_err() {
            tracing::error!(template_id = upload_config.id, "页面上传失败");
        }
    });

    Ok(Json(serde_json::json!({})))
}
'''


def make_upstream(root: Path) -> None:
    files = {
        "app/(app)/upload-manager/page.tsx": UPLOAD_MANAGER_TSX,
        "crates/biliup-cli/src/server/api/endpoints.rs": ENDPOINTS_RS,
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class ManualUploadFeedbackModifierTests(unittest.TestCase):
    def run_modifier(self, root: Path):
        return subprocess.run(
            [sys.executable, "scripts/fix_manual_upload_feedback.py", str(root)],
            text=True,
            capture_output=True,
        )

    def test_manual_upload_is_never_silent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            page = (root / "app/(app)/upload-manager/page.tsx").read_text(encoding="utf-8")
            endpoints = (root / "crates/biliup-cli/src/server/api/endpoints.rs").read_text(encoding="utf-8")

            self.assertIn("biliup-custom:manual-upload-feedback:v1", page)
            self.assertIn("if (selectFiles.length === 0)", page)
            self.assertIn("Notification.warning", page)
            self.assertIn("Notification.success", page)
            self.assertIn("Notification.error", page)
            self.assertIn("上传任务已提交", page)
            self.assertIn("setSelectFiles([])", page)
            self.assertIn("setTransferData([])", page)

            self.assertIn("biliup-custom:manual-upload-feedback:v1", endpoints)
            self.assertIn("该投稿模板上传器为 Noop，不能手动上传", endpoints)
            self.assertNotIn("return Ok(Json(json!({})));", endpoints.split("if upload_config.is_noop_uploader()", 1)[1].split("}", 1)[0])
            self.assertIn("file_count = files.len()", endpoints)
            self.assertIn('"accepted": true', endpoints)

    def test_modifier_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            first = self.run_modifier(root)
            second = self.run_modifier(root)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)


if __name__ == "__main__":
    unittest.main()
