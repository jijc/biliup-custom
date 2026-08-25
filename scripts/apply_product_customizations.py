#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

NATIVE_REVIEW = 42
PAUSE_MARKER = "biliup-custom:persistent-pause:v1"
HISTORY_MARKER = "biliup-custom:live-history-layout:v1"
STATUS_MARKER = "biliup-custom:task-platform-height:v1"
STREAMER_STATUS_MARKER = "biliup-custom:streamer-status-tags:v1"

REPOSITORIES_REL = Path("crates/biliup-cli/src/server/infrastructure/repositories.rs")
ENDPOINTS_REL = Path("crates/biliup-cli/src/server/api/endpoints.rs")
LIB_REL = Path("crates/biliup-cli/src/lib.rs")
MONITOR_REL = Path("crates/biliup-cli/src/server/core/monitor.rs")
JOB_REL = Path("app/(app)/job/page.tsx")
STATUS_REL = Path("app/(app)/status/page.tsx")
STREAMERS_REL = Path("app/(app)/streamers/page.tsx")


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


PAUSE_REPOSITORY_HELPERS = r'''// biliup-custom:persistent-pause:v1
const BILIUP_CUSTOM_PAUSED_STREAMER_KEY: &str = "biliup-custom:paused-streamer";

/// Persistent pause state lives in the existing generic configuration table.
/// This deliberately avoids altering the upstream livestreamers schema or
/// adding a custom migration version that could collide with future upstream
/// migrations.
pub async fn is_streamer_paused(pool: &ConnectionPool, id: i64) -> AppResult<bool> {
    let value = id.to_string();
    let exists: i64 = sqlx::query_scalar(
        "SELECT EXISTS(SELECT 1 FROM configuration WHERE key = ?1 AND value = ?2 LIMIT 1)",
    )
    .bind(BILIUP_CUSTOM_PAUSED_STREAMER_KEY)
    .bind(&value)
    .fetch_one(pool)
    .await
    .change_context(AppError::Unknown)?;
    Ok(exists != 0)
}

pub async fn set_streamer_paused(
    pool: &ConnectionPool,
    id: i64,
    paused: bool,
) -> AppResult<()> {
    let value = id.to_string();
    if paused {
        sqlx::query(
            r#"
            INSERT INTO configuration (key, value)
            SELECT ?1, ?2
            WHERE NOT EXISTS (
                SELECT 1 FROM configuration WHERE key = ?1 AND value = ?2
            )
            "#,
        )
        .bind(BILIUP_CUSTOM_PAUSED_STREAMER_KEY)
        .bind(&value)
        .execute(pool)
        .await
        .change_context(AppError::Unknown)?;
    } else {
        sqlx::query("DELETE FROM configuration WHERE key = ?1 AND value = ?2")
            .bind(BILIUP_CUSTOM_PAUSED_STREAMER_KEY)
            .bind(&value)
            .execute(pool)
            .await
            .change_context(AppError::Unknown)?;
    }
    Ok(())
}
'''

DEL_STREAMER = r'''pub async fn del_streamer(pool: &ConnectionPool, id: i64) -> AppResult<LiveStreamer> {
    let streamer = get_streamer(pool, id).await?;
    // Do not leave stale pause rows behind: SQLite may reuse an integer id
    // after the highest row is deleted.
    set_streamer_paused(pool, id, false).await?;
    streamer
        .clone()
        .delete(pool)
        .await
        .change_context(AppError::Unknown)?;
    Ok(streamer)
}
'''

PAUSE_ENDPOINT = r'''// biliup-custom:persistent-pause:v1
pub async fn pause_streamers_endpoint(
    State(managers): State<Arc<DownloadManager>>,
    State(pool): State<ConnectionPool>,
    Path(id): Path<i64>,
) -> Result<Json<()>, Response> {
    let worker = managers.get_room_by_id(id).await;
    if let Some(w) = worker {
        let worker_status = w.downloader_status.read().unwrap().clone();
        match worker_status {
            WorkerStatus::Working(_) | WorkerStatus::Pending | WorkerStatus::Idle => {
                // Persist first. If the database write fails, the runtime state
                // is left untouched so restart behavior cannot disagree with UI.
                crate::server::infrastructure::repositories::set_streamer_paused(&pool, id, true)
                    .await
                    .map_err(report_to_response)?;
                w.change_status(Stage::Download, WorkerStatus::Pause).await;
                managers.make_waker(id).await;
                info!(url=?&w.live_streamer.url, "successfully pause live streamers (persisted)");
            }
            WorkerStatus::Pause => {
                crate::server::infrastructure::repositories::set_streamer_paused(&pool, id, false)
                    .await
                    .map_err(report_to_response)?;
                w.change_status(Stage::Download, WorkerStatus::Idle).await;
                managers.wake_waker(id).await;
                info!(url=?&w.live_streamer.url, "successfully start live streamers (persisted)");
            }
        };
    }

    Ok(Json(()))
}
'''

LIB_HELPER = r'''// biliup-custom:persistent-pause:v1
async fn biliup_custom_worker_with_persisted_pause(
    service_register: &ServiceRegister,
    live_streamer: crate::server::infrastructure::models::live_streamer::LiveStreamer,
    upload_streamer: Option<UploadStreamer>,
) -> AppResult<crate::server::infrastructure::context::Worker> {
    let worker = service_register.worker(live_streamer, upload_streamer);
    if repositories::is_streamer_paused(&service_register.pool, worker.id()).await? {
        *worker.downloader_status.write().unwrap() =
            crate::server::infrastructure::context::WorkerStatus::Pause;
    }
    Ok(worker)
}
'''

MONITOR_PAUSE_BLOCK = r'''        // biliup-custom:persistent-pause:v1
        // A persisted Pause worker must remain visible in all_workers for the UI,
        // but it must be removed from the active polling queue before the monitor
        // task is spawned. This avoids a startup race that could probe once.
        let initially_paused = matches!(
            *worker.downloader_status.read().unwrap(),
            WorkerStatus::Pause
        );
        if initially_paused {
            self.make_waker(worker.id()).await;
        }
'''


def _modify_backend(upstream: Path) -> None:
    paths = [
        upstream / REPOSITORIES_REL,
        upstream / ENDPOINTS_REL,
        upstream / LIB_REL,
        upstream / MONITOR_REL,
    ]
    for path in paths:
        if not path.is_file():
            raise ModifyError(f"required upstream file missing: {path}")

    repositories, endpoints, lib, monitor = [p.read_text(encoding="utf-8") for p in paths]
    marker_states = [PAUSE_MARKER in text for text in (repositories, endpoints, lib, monitor)]
    if any(marker_states):
        if all(marker_states):
            return
        raise ModifyError("partial persistent-pause modification detected")

    if "pub async fn is_streamer_paused" in repositories:
        print("native-review: upstream appears to have persistent pause support")
        raise SystemExit(NATIVE_REVIEW)

    # Repository helpers + stale-state cleanup on delete.
    anchor = "pub async fn get_all_streamer"
    idx = repositories.find(anchor)
    if idx < 0:
        raise ModifyError("get_all_streamer anchor not found")
    line_start = repositories.rfind("\n", 0, idx) + 1
    repositories = repositories[:line_start] + PAUSE_REPOSITORY_HELPERS + "\n" + repositories[line_start:]
    repositories = _replace_function(repositories, "pub async fn del_streamer", DEL_STREAMER)

    # Pause endpoint toggles both runtime state and durable state.
    endpoints = _replace_function(endpoints, "pub async fn pause_streamers_endpoint", PAUSE_ENDPOINT)

    # Editing a paused streamer currently recreates its Worker. Preserve Pause.
    put_start, put_end = _function_span(endpoints, "pub async fn put_streamers_endpoint")
    put_fn = endpoints[put_start:put_end]
    put_pattern = re.compile(
        r"(?P<indent>\s*)managers\s*\.add_room\(service_register\.worker\(streamer\.clone\(\), upload_config\)\)\s*\.await"
    )
    put_match = put_pattern.search(put_fn)
    if not put_match:
        raise ModifyError("put_streamers add_room shape changed")
    indent = put_match.group("indent")
    replacement = (
        f"{indent}let worker = service_register.worker(streamer.clone(), upload_config);\n"
        f"{indent}if crate::server::infrastructure::repositories::is_streamer_paused(&pool, id)\n"
        f"{indent}    .await\n"
        f"{indent}    .map_err(report_to_response)?\n"
        f"{indent}{{\n"
        f"{indent}    *worker.downloader_status.write().unwrap() = WorkerStatus::Pause;\n"
        f"{indent}}}\n"
        f"{indent}managers.add_room(worker).await"
    )
    put_fn = put_fn[: put_match.start()] + replacement + put_fn[put_match.end() :]
    endpoints = endpoints[:put_start] + put_fn + endpoints[put_end:]

    # Central helper means both config-file startup and database startup restore
    # Pause without changing the upstream LiveStreamer schema.
    import_anchor = "async fn import_config_streamers"
    idx = lib.find(import_anchor)
    if idx < 0:
        raise ModifyError("import_config_streamers anchor not found")
    line_start = lib.rfind("\n", 0, idx) + 1
    lib = lib[:line_start] + LIB_HELPER + "\n" + lib[line_start:]
    worker_expr = "service_register.worker(live_streamer.clone(), upload_config)"
    count = lib.count(worker_expr)
    if count != 2:
        raise ModifyError(f"expected 2 startup worker constructions, found {count}")
    lib = lib.replace(
        worker_expr,
        "biliup_custom_worker_with_persisted_pause(service_register, live_streamer.clone(), upload_config).await?",
    )

    # Monitor.add sends ActorMessage::Add first, but does not spawn the polling
    # task until rooms_handle_pool. Remove persisted Pause workers from the queue
    # in that gap so restart cannot perform one accidental probe.
    add_start, add_end = _function_span(monitor, "pub async fn add(")
    add_fn = monitor[add_start:add_end]
    pool_call = "        self.rooms_handle_pool(plugin.clone());"
    if pool_call not in add_fn:
        raise ModifyError("Monitor.add rooms_handle_pool anchor changed")
    add_fn = add_fn.replace(pool_call, MONITOR_PAUSE_BLOCK + pool_call, 1)
    monitor = monitor[:add_start] + add_fn + monitor[add_end:]

    for path, text in zip(paths, (repositories, endpoints, lib, monitor)):
        path.write_text(text, encoding="utf-8")


def _insert_column_width(text: str, title: str, width: int) -> str:
    pattern = re.compile(
        rf"(?P<title>title:\s*'{re.escape(title)}',\s*\n)(?P<indent>\s*)(?P<data>dataIndex:)"
    )
    match = pattern.search(text)
    if not match:
        raise ModifyError(f"history column anchor changed: {title}")
    indent = match.group("indent")
    replacement = match.group("title") + indent + f"width: {width},\n" + indent + match.group("data")
    return text[: match.start()] + replacement + text[match.end() :]


def _modify_streamer_status_tags(text: str) -> str:
    if STREAMER_STATUS_MARKER in text:
        return text

    desired = (
        '<Tag color="red">录制中</Tag>',
        '<Tag color="blue">空闲</Tag>',
        '<Tag color="green">检测中</Tag>',
        '<Tag color="grey">暂停中</Tag>',
    )
    if all(item in text for item in desired):
        print("native-review: upstream appears to have desired streamer status tags")
        raise SystemExit(NATIVE_REVIEW)

    replacements = (
        ('statusTag = <Tag color="red">直播中</Tag>', 'statusTag = <Tag color="red">录制中</Tag>'),
        ('statusTag = <Tag color="green">空闲</Tag>', 'statusTag = <Tag color="blue">空闲</Tag>'),
        ('statusTag = <Tag color="indigo">检测中</Tag>', 'statusTag = <Tag color="green">检测中</Tag>'),
        ('statusTag = <Tag color="pink">暂停中</Tag>', 'statusTag = <Tag color="grey">暂停中</Tag>'),
    )
    for old, new in replacements:
        count = text.count(old)
        if count != 1:
            raise ModifyError(f"streamer status tag anchor changed: {old} (found {count})")
        text = text.replace(old, new, 1)

    anchor = "    let statusTag\n"
    if anchor not in text:
        raise ModifyError("streamer statusTag declaration anchor changed")
    return text.replace(
        anchor,
        f"    // {STREAMER_STATUS_MARKER}\n" + anchor,
        1,
    )


def _modify_ui(upstream: Path) -> None:
    job_path = upstream / JOB_REL
    status_path = upstream / STATUS_REL
    streamers_path = upstream / STREAMERS_REL
    for path in (job_path, status_path, streamers_path):
        if not path.is_file():
            raise ModifyError(f"required upstream UI file missing: {path}")

    job = job_path.read_text(encoding="utf-8")
    status = status_path.read_text(encoding="utf-8")
    streamers = streamers_path.read_text(encoding="utf-8")

    job_marked = HISTORY_MARKER in job
    status_marked = STATUS_MARKER in status
    if job_marked != status_marked:
        raise ModifyError("partial UI customization detected")
    if not job_marked:
        columns_anchor = "  const columns = ["
        if columns_anchor not in job:
            raise ModifyError("live history columns anchor changed")
        job = job.replace(
            columns_anchor,
            "  // biliup-custom:live-history-layout:v1\n" + columns_anchor,
            1,
        )
        job = _insert_column_width(job, "名称", 180)
        job = _insert_column_width(job, "标题", 360)
        job = _insert_column_width(job, "封面", 120)

        main_anchor = "        <main>"
        if main_anchor not in status:
            raise ModifyError("task platform main anchor changed")
        status = status.replace(
            main_anchor,
            "        {/* biliup-custom:task-platform-height:v1 */}\n"
            "        <main style={{ height: '100%' }}>",
            1,
        )
        content_close = "      </Content>"
        if content_close not in status:
            raise ModifyError("task platform Content closing anchor changed")
        css = r'''        <style>{`
          .semi-layout-content > main > ul {
            height: 100%;
            box-sizing: border-box;
          }
        `}</style>
'''
        status = status.replace(content_close, css + content_close, 1)

    streamers = _modify_streamer_status_tags(streamers)

    job_path.write_text(job, encoding="utf-8")
    status_path.write_text(status, encoding="utf-8")
    streamers_path.write_text(streamers, encoding="utf-8")


def modify(upstream: Path) -> None:
    _modify_backend(upstream)
    _modify_ui(upstream)
    print("modified")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: apply_product_customizations.py <upstream-dir>", file=sys.stderr)
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
