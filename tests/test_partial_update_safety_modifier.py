import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ENDPOINTS = r'''use crate::server::infrastructure::repositories::{
    del_streamer, delete_bilibili_cookie, get_all_streamer, get_upload_config,
    register_bilibili_cookie,
};
use serde::Deserialize;

pub async fn put_streamers_endpoint(
    State(service_register): State<ServiceRegister>,
    State(managers): State<Arc<DownloadManager>>,
    State(pool): State<ConnectionPool>,
    Json(payload): Json<LiveStreamer>,
) -> Result<Json<LiveStreamer>, Response> {
    let streamer = payload
        .update_all_fields(&pool)
        .await
        .change_context(AppError::Unknown)
        .map_err(report_to_response)?;

    let id = streamer.id;
    managers.del_room(id).await;
    let upload_config = get_upload_config(&pool, id).await.map_err(report_to_response)?;
    managers.add_room(service_register.worker(streamer.clone(), upload_config)).await
        .ok_or(AppError::Unknown).map_err(report_to_response)?;
    Ok(Json(streamer))
}

pub async fn put_configuration(
    State(config): State<Arc<RwLock<Config>>>,
    State(pool): State<ConnectionPool>,
    State(log_handle): State<LogHandle>,
    Json(json_data): Json<Config>,
) -> Result<Json<Config>, Response> {
    let mut json_data = json_data;
    json_data.normalize_segment_limits();
    json_data.validate_segment_limits().map_err(report_to_response)?;
    let value_txt = serde_json::to_string(&json_data)
        .change_context(AppError::Unknown)
        .map_err(report_to_response)?;
    Ok(Json(json_data))
}

pub async fn add_upload_streamer_endpoint(
    State(pool): State<ConnectionPool>,
    Json(upload_streamer): Json<InsertUploadStreamer>,
) -> Result<Json<serde_json::Value>, Response> {
    if upload_streamer.id.is_none() {
        Ok(Json(serde_json::to_value(
            ormlite::Insert::insert(upload_streamer, &pool).await
                .change_context(AppError::Unknown).map_err(report_to_response)?,
        ).change_context(AppError::Unknown).map_err(report_to_response)?))
    } else {
        Ok(Json(serde_json::to_value(
            upload_streamer.update_all_fields(&pool).await
                .change_context(AppError::Unknown).map_err(report_to_response)?,
        ).change_context(AppError::Unknown).map_err(report_to_response)?))
    }
}
'''


def make_upstream(root: Path) -> None:
    path = root / "crates/biliup-cli/src/server/api/endpoints.rs"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ENDPOINTS, encoding="utf-8")


class PartialUpdateSafetyModifierTests(unittest.TestCase):
    def run_modifier(self, root: Path):
        return subprocess.run(
            [sys.executable, "scripts/fix_partial_update_safety.py", str(root)],
            text=True,
            capture_output=True,
        )

    def test_mutable_settings_use_patch_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            text = (root / "crates/biliup-cli/src/server/api/endpoints.rs").read_text(encoding="utf-8")

            self.assertIn("biliup-custom:partial-update-safety:v1", text)
            self.assertIn("Json(patch): Json<serde_json::Value>", text)
            self.assertIn("merge_json_patch", text)
            self.assertIn("get_streamer(&pool, id)", text)
            self.assertIn("serde_json::from_value::<LiveStreamer>", text)
            self.assertIn("config.read().unwrap().clone()", text)
            self.assertIn("serde_json::from_value::<Config>", text)
            self.assertIn("UploadStreamer::select()", text)
            self.assertIn("normalize_upload_template_patch", text)
            self.assertNotIn("Json(payload): Json<LiveStreamer>", text)
            self.assertNotIn("Json(json_data): Json<Config>", text)
            self.assertNotIn("Json(upload_streamer): Json<InsertUploadStreamer>", text)

    def test_modifier_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            first = self.run_modifier(root)
            second = self.run_modifier(root)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
            text = (root / "crates/biliup-cli/src/server/api/endpoints.rs").read_text(encoding="utf-8")
            self.assertEqual(text.count("biliup-custom:partial-update-safety:v1"), 1)


if __name__ == "__main__":
    unittest.main()
