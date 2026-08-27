#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "// biliup-custom:partial-update-safety:v1"
NATIVE_REVIEW = 42
ENDPOINTS_REL = Path("crates/biliup-cli/src/server/api/endpoints.rs")


class ModifyError(RuntimeError):
    pass


def _matching_brace(text: str, open_idx: int) -> int:
    depth = 0
    i = open_idx
    state = "code"
    block_depth = 0
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if state == "code":
            if ch == '"':
                state = "string"
            elif ch == "'":
                state = "char"
            elif ch == "/" and nxt == "/":
                state = "line_comment"
                i += 1
            elif ch == "/" and nxt == "*":
                state = "block_comment"
                block_depth = 1
                i += 1
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
        elif state == "string":
            if ch == "\\":
                i += 1
            elif ch == '"':
                state = "code"
        elif state == "char":
            if ch == "\\":
                i += 1
            elif ch == "'":
                state = "code"
        elif state == "line_comment":
            if ch == "\n":
                state = "code"
        elif state == "block_comment":
            if ch == "/" and nxt == "*":
                block_depth += 1
                i += 1
            elif ch == "*" and nxt == "/":
                block_depth -= 1
                i += 1
                if block_depth == 0:
                    state = "code"
        i += 1
    raise ModifyError("unbalanced braces while locating function")


def _function_span(text: str, signature: str) -> tuple[int, int]:
    idx = text.find(signature)
    if idx < 0:
        raise ModifyError(f"expected function signature not found: {signature}")
    start = text.rfind("\n", 0, idx) + 1
    open_idx = text.find("{", idx + len(signature))
    if open_idx < 0:
        raise ModifyError(f"opening brace not found for: {signature}")
    close_idx = _matching_brace(text, open_idx)
    end = close_idx + 1
    if end < len(text) and text[end] == "\n":
        end += 1
    return start, end


def _replace_function(text: str, signature: str, replacement: str) -> str:
    start, end = _function_span(text, signature)
    return text[:start] + replacement.rstrip() + "\n" + text[end:]


HELPERS = r'''// biliup-custom:partial-update-safety:v1
fn merge_json_patch(
    mut base: serde_json::Value,
    patch: serde_json::Value,
) -> Result<serde_json::Value, Response> {
    let Some(base_obj) = base.as_object_mut() else {
        return Err((StatusCode::INTERNAL_SERVER_ERROR, "stored object is not JSON object").into_response());
    };
    let Some(patch_obj) = patch.as_object() else {
        return Err((StatusCode::BAD_REQUEST, "update payload must be a JSON object").into_response());
    };
    for (key, value) in patch_obj {
        base_obj.insert(key.clone(), value.clone());
    }
    Ok(base)
}

fn normalize_upload_template_patch(mut patch: serde_json::Value) -> serde_json::Value {
    if let Some(obj) = patch.as_object_mut() {
        for key in ["up_selection_reply", "up_close_reply", "up_close_danmu"] {
            let Some(value) = obj.get_mut(key) else {
                continue;
            };
            if value.as_i64() == Some(0) {
                *value = serde_json::Value::Bool(false);
            } else if value.as_i64() == Some(1) {
                *value = serde_json::Value::Bool(true);
            }
        }
    }
    patch
}
'''


PUT_STREAMER = r'''pub async fn put_streamers_endpoint(
    State(service_register): State<ServiceRegister>,
    State(managers): State<Arc<DownloadManager>>,
    State(pool): State<ConnectionPool>,
    Json(patch): Json<serde_json::Value>,
) -> Result<Json<LiveStreamer>, Response> {
    let id = patch
        .get("id")
        .and_then(serde_json::Value::as_i64)
        .ok_or_else(|| (StatusCode::BAD_REQUEST, "Missing or invalid streamer id").into_response())?;
    let existing = get_streamer(&pool, id).await.map_err(report_to_response)?;
    let merged = merge_json_patch(
        serde_json::to_value(existing)
            .change_context(AppError::Unknown)
            .map_err(report_to_response)?,
        patch,
    )?;
    let payload = serde_json::from_value::<LiveStreamer>(merged)
        .change_context(AppError::Unknown)
        .map_err(report_to_response)?;

    let streamer = payload
        .update_all_fields(&pool)
        .await
        .change_context(AppError::Unknown)
        .map_err(report_to_response)?;

    let id = streamer.id;
    managers.del_room(id).await;

    let upload_config = get_upload_config(&pool, id)
        .await
        .map_err(report_to_response)?;

    managers
        .add_room(service_register.worker(streamer.clone(), upload_config))
        .await
        .ok_or(AppError::Unknown)
        .map_err(report_to_response)?;

    info!(id = id, "successfully update live streamers");
    Ok(Json(streamer))
}
'''


ADD_UPLOAD_STREAMER = r'''pub async fn add_upload_streamer_endpoint(
    State(pool): State<ConnectionPool>,
    Json(patch): Json<serde_json::Value>,
) -> Result<Json<serde_json::Value>, Response> {
    let Some(id) = patch.get("id").and_then(serde_json::Value::as_i64) else {
        let upload_streamer = serde_json::from_value::<InsertUploadStreamer>(patch)
            .change_context(AppError::Unknown)
            .map_err(report_to_response)?;
        return Ok(Json(
            serde_json::to_value(
                ormlite::Insert::insert(upload_streamer, &pool)
                    .await
                    .change_context(AppError::Unknown)
                    .map_err(report_to_response)?,
            )
            .change_context(AppError::Unknown)
            .map_err(report_to_response)?,
        ));
    };

    let existing = UploadStreamer::select()
        .where_("id = ?")
        .bind(id)
        .fetch_one(&pool)
        .await
        .change_context(AppError::Unknown)
        .map_err(report_to_response)?;
    let patch = normalize_upload_template_patch(patch);
    let merged = merge_json_patch(
        serde_json::to_value(existing)
            .change_context(AppError::Unknown)
            .map_err(report_to_response)?,
        patch,
    )?;
    let upload_streamer = serde_json::from_value::<UploadStreamer>(merged)
        .change_context(AppError::Unknown)
        .map_err(report_to_response)?;

    Ok(Json(
        serde_json::to_value(
            upload_streamer
                .update_all_fields(&pool)
                .await
                .change_context(AppError::Unknown)
                .map_err(report_to_response)?,
        )
        .change_context(AppError::Unknown)
        .map_err(report_to_response)?,
    ))
}
'''


def _modify_configuration(text: str) -> str:
    signature_old = "    Json(json_data): Json<Config>,\n"
    signature_new = "    Json(patch): Json<serde_json::Value>,\n"
    if text.count(signature_old) != 1:
        raise ModifyError(f"expected one Config PUT JSON signature, found {text.count(signature_old)}")
    text = text.replace(signature_old, signature_new, 1)

    start_old = "    let mut json_data = json_data;\n    json_data.normalize_segment_limits();\n"
    start_new = r'''    let existing = config.read().unwrap().clone();
    let merged = merge_json_patch(
        serde_json::to_value(existing)
            .change_context(AppError::Unknown)
            .map_err(report_to_response)?,
        patch,
    )?;
    let mut json_data = serde_json::from_value::<Config>(merged)
        .change_context(AppError::Unknown)
        .map_err(report_to_response)?;
    json_data.normalize_segment_limits();
'''
    if text.count(start_old) != 1:
        raise ModifyError(f"expected one Config PUT body anchor, found {text.count(start_old)}")
    return text.replace(start_old, start_new, 1)


def modify(upstream: Path) -> None:
    endpoints_path = upstream / ENDPOINTS_REL
    if not endpoints_path.is_file():
        raise ModifyError(f"required upstream file missing: {endpoints_path}")

    text = endpoints_path.read_text(encoding="utf-8")
    if MARKER in text:
        print("already-modified")
        return

    # Stop for review if upstream already moved to partial JSON update semantics.
    if "Json(patch): Json<serde_json::Value>" in text or "merge_json_patch" in text:
        print("native-review: upstream already contains partial update semantics")
        raise SystemExit(NATIVE_REVIEW)

    import_old = (
        "    del_streamer, delete_bilibili_cookie, get_all_streamer, get_upload_config,\n"
        "    register_bilibili_cookie,\n"
    )
    import_new = (
        "    del_streamer, delete_bilibili_cookie, get_all_streamer, get_streamer, get_upload_config,\n"
        "    register_bilibili_cookie,\n"
    )
    if text.count(import_old) != 1:
        raise ModifyError("repository import anchor changed")
    text = text.replace(import_old, import_new, 1)

    put_start, _ = _function_span(text, "pub async fn put_streamers_endpoint")
    text = text[:put_start] + HELPERS + "\n\n" + text[put_start:]
    text = _replace_function(text, "pub async fn put_streamers_endpoint", PUT_STREAMER)
    text = _modify_configuration(text)
    text = _replace_function(text, "pub async fn add_upload_streamer_endpoint", ADD_UPLOAD_STREAMER)

    endpoints_path.write_text(text, encoding="utf-8")
    print("modified")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: fix_partial_update_safety.py <upstream-dir>", file=sys.stderr)
        return 2
    try:
        modify(Path(argv[1]))
        return 0
    except SystemExit as exc:
        return int(exc.code)
    except ModifyError as exc:
        print(f"modifier-error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
