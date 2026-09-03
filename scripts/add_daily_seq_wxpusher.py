#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

MARKER = "// biliup-custom:daily-seq-wxpusher:v1"
NATIVE_REVIEW = 42
COMMON_REL = Path("crates/biliup-cli/src/server/common")
MOD_REL = COMMON_REL / "mod.rs"
DOWNLOAD_REL = COMMON_REL / "download.rs"
UPLOAD_REL = COMMON_REL / "upload.rs"
WXPUSHER_REL = COMMON_REL / "wxpusher.rs"


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


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ModifyError(f"expected one {label} anchor, found {count}")
    return text.replace(old, new, 1)


WXPUSHER_SOURCE = r'''// biliup-custom:daily-seq-wxpusher:v1
use serde_json::json;
use std::time::Duration;
use tracing::warn;

const ENDPOINT: &str = "https://wxpusher.zjiecode.com/api/send/message";

fn parse_uids(raw: &str) -> Vec<String> {
    raw.split([',', ';', '\n', '\t', ' '])
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect()
}

/// Best-effort notification. Missing configuration, network errors and API
/// failures never affect recording, remuxing or uploading.
pub fn notify_detached(summary: impl Into<String>, content: impl Into<String>) {
    let Ok(app_token) = std::env::var("WXPUSHER_APP_TOKEN") else {
        return;
    };
    let Ok(raw_uids) = std::env::var("WXPUSHER_UIDS") else {
        return;
    };
    let uids = parse_uids(&raw_uids);
    if app_token.trim().is_empty() || uids.is_empty() {
        return;
    }

    let summary = summary.into();
    let content = content.into();
    tokio::spawn(async move {
        let client = match reqwest::Client::builder()
            .timeout(Duration::from_secs(8))
            .build()
        {
            Ok(client) => client,
            Err(e) => {
                warn!(error = ?e, "WxPusher client creation failed");
                return;
            }
        };
        let payload = json!({
            "appToken": app_token,
            "content": content,
            "summary": summary,
            "contentType": 1,
            "uids": uids,
        });
        match client.post(ENDPOINT).json(&payload).send().await {
            Ok(response) if response.status().is_success() => {}
            Ok(response) => {
                warn!(status = %response.status(), "WxPusher returned non-success status");
            }
            Err(e) => {
                warn!(error = ?e, "WxPusher request failed");
            }
        }
    });
}

#[cfg(test)]
mod biliup_custom_wxpusher_tests {
    use super::*;

    #[test]
    fn parses_multiple_uid_separators() {
        assert_eq!(
            parse_uids("UID_a, UID_b;UID_c\nUID_d"),
            vec!["UID_a", "UID_b", "UID_c", "UID_d"]
        );
    }
}
'''


UPLOAD_HELPERS = r'''// biliup-custom:daily-seq-wxpusher:v1
const DAILY_SEQ_TOKEN: &str = "{daily_seq}";

fn parse_daily_sequence(name: &str) -> Option<u32> {
    let (head, _) = name.split_once('-')?;
    if head.len() < 2 || !head.chars().all(|c| c.is_ascii_digit()) {
        return None;
    }
    head.parse().ok()
}

fn next_daily_sequence(parent: &Path) -> std::io::Result<u32> {
    let mut max_seq = 0_u32;
    match std::fs::read_dir(parent) {
        Ok(entries) => {
            for entry in entries.flatten() {
                if let Some(name) = entry.file_name().to_str()
                    && let Some(seq) = parse_daily_sequence(name)
                {
                    max_seq = max_seq.max(seq);
                }
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(1),
        Err(e) => return Err(e),
    }
    Ok(max_seq.saturating_add(1).max(1))
}

fn sequenced_path(path: &Path, seq: u32) -> Option<PathBuf> {
    let name = path.file_name()?.to_str()?;
    if !name.contains(DAILY_SEQ_TOKEN) {
        return None;
    }
    Some(path.with_file_name(name.replace(DAILY_SEQ_TOKEN, &format!("{seq:02}"))))
}

async fn finalize_daily_sequence(paths: &mut [PathBuf]) -> AppResult<()> {
    let Some(video_path) = paths.first().cloned() else {
        return Ok(());
    };
    if video_path
        .file_name()
        .and_then(|s| s.to_str())
        .is_none_or(|name| !name.contains(DAILY_SEQ_TOKEN))
    {
        return Ok(());
    }
    let parent = video_path.parent().ok_or_else(|| {
        error_stack::Report::new(AppError::Custom(format!(
            "daily sequence path has no parent: {}",
            video_path.display()
        )))
    })?;
    let mut seq = next_daily_sequence(parent).change_context(AppError::Custom(format!(
        "failed to scan daily sequence directory {}",
        parent.display()
    )))?;

    loop {
        let target = sequenced_path(&video_path, seq).expect("daily sequence token checked above");
        if tokio::fs::try_exists(&target)
            .await
            .change_context(AppError::Custom(format!(
                "failed to check daily sequence target {}",
                target.display()
            )))?
        {
            seq = seq.saturating_add(1);
            continue;
        }

        tokio::fs::rename(&video_path, &target)
            .await
            .change_context(AppError::Custom(format!(
                "failed to finalize daily sequence {} -> {}",
                video_path.display(),
                target.display()
            )))?;
        paths[0] = target;

        for path in paths.iter_mut().skip(1) {
            let Some(companion_target) = sequenced_path(path, seq) else {
                continue;
            };
            match tokio::fs::rename(&*path, &companion_target).await {
                Ok(()) => *path = companion_target,
                Err(e) if e.kind() == std::io::ErrorKind::NotFound => {}
                Err(e) => {
                    error!(source = %path.display(), target = %companion_target.display(), error = ?e, "failed to rename daily-sequence companion file");
                }
            }
        }
        return Ok(());
    }
}

fn notification_streamer(ctx: &Context) -> String {
    ctx.streamer_info().name.clone()
}

#[cfg(test)]
mod biliup_custom_daily_sequence_tests {
    use super::*;

    #[test]
    fn parses_only_numeric_sequence_prefixes() {
        assert_eq!(parse_daily_sequence("01-demo.mp4"), Some(1));
        assert_eq!(parse_daily_sequence("12-demo.xml"), Some(12));
        assert_eq!(parse_daily_sequence("{daily_seq}-demo.mp4"), None);
        assert_eq!(parse_daily_sequence("1-demo.mp4"), None);
    }

    #[test]
    fn replaces_daily_sequence_token_without_changing_extension() {
        assert_eq!(
            sequenced_path(Path::new("/recordings/a/2026-08-25/{daily_seq}-demo.mp4"), 3),
            Some(PathBuf::from("/recordings/a/2026-08-25/03-demo.mp4"))
        );
    }
}
'''


PROCESS_WITHOUT_UPLOAD = r'''async fn process_without_upload<F>(
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
        let source = event_paths
            .first()
            .cloned()
            .unwrap_or_else(|| event.prev_file_path.clone());
        match remux_completed_flv_to_mp4(&source).await {
            Ok(converted) => {
                if let Some(video_path) = event_paths.first_mut() {
                    *video_path = converted;
                }
                if let Err(e) = finalize_daily_sequence(&mut event_paths).await {
                    error!(file = ?source, error = ?e, "每日编号失败，保留已转换文件");
                }
            }
            Err(e) => {
                wxpusher::notify_detached(
                    format!("⚠️ {} 转换失败", notification_streamer(ctx)),
                    format!("主播：{}\n源文件：{}\n错误：{:?}\n原 FLV 已保留。", notification_streamer(ctx), source.display(), e),
                );
                error!(file = ?source, error = ?e, "自动转换 MP4 失败，保留原 FLV");
                continue;
            }
        }
        paths.extend(event_paths);
    }
    execute_postprocessor(paths, ctx).await
}
'''


PIPELINE_UPLOAD = r'''async fn pipeline_upload_videos<F>(
    rx: Inspect<Receiver<SegmentInfo>, F>,
    context: &UploadContext,
    segment_processors: &[HookStep],
    ctx: &Context,
) -> AppResult<UploadedVideos>
where
    F: FnMut(&SegmentInfo),
{
    let mut uploaded = UploadedVideos::default();
    pin!(rx);
    while let Some(event) = rx.next().await {
        let mut paths = segment_paths(&event);

        // Keep upstream segment processors first. Our automatic MP4 remux and
        // numbering run on their resulting path, still in the uploader actor,
        // so recording the next segment never waits for conversion/upload.
        if !segment_processors.is_empty()
            && let Err(e) = process_video_paths(&mut paths, segment_processors).await
        {
            error!(file = ?event.prev_file_path, "segment_processor failed, skipping segment: {:?}", e);
            continue;
        }

        let source = paths
            .first()
            .cloned()
            .unwrap_or_else(|| event.prev_file_path.clone());
        match remux_completed_flv_to_mp4(&source).await {
            Ok(converted) => {
                if let Some(video_path) = paths.first_mut() {
                    *video_path = converted;
                }
            }
            Err(e) => {
                wxpusher::notify_detached(
                    format!("⚠️ {} 转换失败", notification_streamer(ctx)),
                    format!("主播：{}\n源文件：{}\n错误：{:?}\n原 FLV 已保留，本段未上传。", notification_streamer(ctx), source.display(), e),
                );
                error!(file = ?source, error = ?e, "自动转换 MP4 失败，本段跳过上传并保留原 FLV");
                continue;
            }
        }

        if let Err(e) = finalize_daily_sequence(&mut paths).await {
            error!(file = ?source, error = ?e, "每日编号失败，本段跳过上传");
            continue;
        }

        let upload_path = paths
            .first()
            .cloned()
            .unwrap_or_else(|| event.prev_file_path.clone());
        match upload_single_file(&upload_path, context).await {
            Ok(video) => {
                uploaded.videos.push(video);
                uploaded.paths.extend(paths);
            }
            Err(e) => {
                wxpusher::notify_detached(
                    format!("❌ {} 上传错误", notification_streamer(ctx)),
                    format!("主播：{}\n文件：{}\n错误：{:?}", notification_streamer(ctx), upload_path.display(), e),
                );
                error!(file = ?upload_path, "upload_single_file failed, skipping segment: {:?}", e);
            }
        }
    }
    Ok(uploaded)
}
'''


PROCESS_WITH_UPLOAD = r'''pub async fn process_with_upload<F>(
    rx: Inspect<Receiver<SegmentInfo>, F>,
    ctx: &Context,
    upload_config: &UploadStreamer,
) -> AppResult<()>
where
    F: FnMut(&SegmentInfo),
{
    info!(upload_config=?upload_config, "Starting process with upload");
    let upload_context =
        initialize_upload_context(&ctx.config(), &ctx.stateless_client(), upload_config).await?;

    let segment_processors: Vec<HookStep> = ctx
        .live_streamer()
        .segment_processor
        .clone()
        .unwrap_or_default();
    let uploaded_videos =
        pipeline_upload_videos(rx, &upload_context, &segment_processors, ctx).await?;

    if !uploaded_videos.videos.is_empty() {
        let part_count = uploaded_videos.videos.len();
        let mut recorder = ctx.recorder(ctx.streamer_info().clone()).clone();
        recorder.filename_prefix = upload_config.title.clone();
        let studio = build_studio(
            upload_config,
            &upload_context.bilibili,
            uploaded_videos.videos,
            &recorder,
        )
        .await?;
        let submit_api = ctx.config().submit_api.clone();
        let title = studio.title.clone();
        match submit_to_bilibili(&upload_context.bilibili, &studio, submit_api.as_deref()).await {
            Ok(_) => {
                wxpusher::notify_detached(
                    format!("✅ {} 上传完成", notification_streamer(ctx)),
                    format!("主播：{}\n标题：{}\n分P数量：{}\nB站稿件已提交成功。", notification_streamer(ctx), title, part_count),
                );
            }
            Err(e) => {
                wxpusher::notify_detached(
                    format!("❌ {} 上传错误", notification_streamer(ctx)),
                    format!("主播：{}\n标题：{}\n阶段：B站稿件提交\n错误：{:?}", notification_streamer(ctx), title, e),
                );
                return Err(e);
            }
        }
    }

    if !uploaded_videos.paths.is_empty() {
        execute_postprocessor(uploaded_videos.paths, ctx).await?;
    }
    Ok(())
}
'''


def _modify_upload(upload: str) -> str:
    if "biliup-custom:auto-mp4:v1" not in upload:
        raise ModifyError("automatic MP4 modifier must run before daily sequence modifier")
    if MARKER in upload:
        return upload

    required = [
        "async fn process_without_upload<F>",
        "async fn pipeline_upload_videos<F>",
        "pub async fn process_with_upload<F>",
        "upload_single_file(&upload_path, context).await",
        "submit_to_bilibili(&upload_context.bilibili, &studio, submit_api.as_deref()).await?",
    ]
    missing = [item for item in required if item not in upload]
    if missing:
        print(f"native-review: upload flow changed upstream: {missing}")
        raise SystemExit(NATIVE_REVIEW)

    start, _ = _function_span(upload, "pub async fn process_with_upload<F>")
    upload = upload[:start] + "use crate::server::common::wxpusher;\n\n" + UPLOAD_HELPERS + "\n\n" + upload[start:]
    upload = _replace_function(upload, "pub async fn process_with_upload<F>", PROCESS_WITH_UPLOAD)
    upload = _replace_function(upload, "async fn process_without_upload<F>", PROCESS_WITHOUT_UPLOAD)
    upload = _replace_function(upload, "async fn pipeline_upload_videos<F>", PIPELINE_UPLOAD)
    return upload


def _modify_download(download: str) -> str:
    if MARKER in download:
        return download
    if "pub async fn start_download_workflow" not in download or "retry_count >= max_retries" not in download:
        print("native-review: download workflow changed upstream")
        raise SystemExit(NATIVE_REVIEW)

    if "use crate::server::common::wxpusher;" not in download:
        import_anchor = "use crate::server::common::util::FileValidator;\n"
        if import_anchor not in download:
            import_anchor = "use crate::server::common::recording_policy;\n"
        download = _replace_once(
            download,
            import_anchor,
            import_anchor + "use crate::server::common::wxpusher;\n",
            "download import",
        )

    download = _replace_once(
        download,
        "        let mut retry_count = 0;",
        "        let mut retry_count = 0;\n        let mut last_check_offline: bool;",
        "retry counter",
    )

    live_pattern = re.compile(r"(Ok\(LiveStatus::Live\s*\{.*?\}\)\s*=>\s*\{)", re.S)
    live_match = live_pattern.search(download)
    if not live_match:
        raise ModifyError("live retry branch not found")
    block = live_match.group(1)
    block_new = block + "\n                    last_check_offline = false;"
    download = download[: live_match.start(1)] + block_new + download[live_match.end(1) :]

    offline_pattern = re.compile(r"(Ok\(LiveStatus::Offline\)\s*=>\s*\{\s*retry_count\s*\+=\s*1;)", re.S)
    offline_match = offline_pattern.search(download)
    if not offline_match:
        raise ModifyError("offline retry branch not found")
    block = offline_match.group(1)
    block_new = block + "\n                    last_check_offline = true;"
    download = download[: offline_match.start(1)] + block_new + download[offline_match.end(1) :]

    err_pattern = re.compile(r"(Err\(e\)\s*=>\s*\{\s*retry_count\s*\+=\s*1;)", re.S)
    err_match = err_pattern.search(download)
    if not err_match:
        raise ModifyError("stream check error branch not found")
    block = err_match.group(1)
    block_new = block + "\n                    last_check_offline = false;"
    download = download[: err_match.start(1)] + block_new + download[err_match.end(1) :]

    max_pattern = re.compile(
        r"(?P<indent>\s*)if retry_count >= max_retries \{(?P<body>.*?)break components;(?P<tail>\s*)\}",
        re.S,
    )
    max_match = max_pattern.search(download)
    if not max_match:
        raise ModifyError("max retry block not found")
    indent = max_match.group("indent")
    body = max_match.group("body")
    replacement = (
        f"{indent}if retry_count >= max_retries {{"
        f"{body}"
        f"{indent}    if last_check_offline {{\n"
        f"{indent}        let info = ctx.streamer_info();\n"
        f"{indent}        wxpusher::notify_detached(\n"
        f"{indent}            format!(\"⚫ {{}} 停播\", info.name),\n"
        f"{indent}            format!(\"主播：{{}}\\n直播标题：{{}}\\n直播地址：{{}}\", info.name, info.title, info.url),\n"
        f"{indent}        );\n"
        f"{indent}    }}\n"
        f"{indent}    break components;\n"
        f"{indent}}}"
    )
    download = download[: max_match.start()] + replacement + download[max_match.end() :]

    status_anchor = "    ctx.change_status(Stage::Download, WorkerStatus::Working(task.clone()))\n        .await;"
    if status_anchor not in download:
        raise ModifyError("working status anchor not found")
    start_notify = status_anchor + r'''
    {
        let info = ctx.streamer_info();
        wxpusher::notify_detached(
            format!("🟢 {} 开播", info.name),
            format!("主播：{}\n直播标题：{}\n直播地址：{}", info.name, info.title, info.url),
        );
    }'''
    download = download.replace(status_anchor, start_notify, 1)
    return MARKER + "\n" + download


def modify(upstream: Path) -> None:
    mod_path = upstream / MOD_REL
    download_path = upstream / DOWNLOAD_REL
    upload_path = upstream / UPLOAD_REL
    wx_path = upstream / WXPUSHER_REL
    for path in (mod_path, download_path, upload_path):
        if not path.is_file():
            raise ModifyError(f"required upstream file missing: {path}")

    mod_rs = mod_path.read_text(encoding="utf-8")
    download = download_path.read_text(encoding="utf-8")
    upload = upload_path.read_text(encoding="utf-8")

    already = MARKER in upload and MARKER in download and "pub mod wxpusher;" in mod_rs and wx_path.is_file()
    if already:
        print("already-modified")
        return
    if MARKER in upload or MARKER in download or wx_path.exists():
        raise ModifyError("partial previous daily-seq/WxPusher modification detected")

    if "pub mod wxpusher;" in mod_rs:
        print("native-review: upstream already has wxpusher module")
        raise SystemExit(NATIVE_REVIEW)
    if "pub mod upload;" not in mod_rs:
        raise ModifyError("common mod upload anchor not found")

    upload = _modify_upload(upload)
    download = _modify_download(download)
    mod_rs = mod_rs.replace("pub mod upload;", "pub mod upload;\npub mod wxpusher;", 1)

    mod_path.write_text(mod_rs, encoding="utf-8")
    download_path.write_text(download, encoding="utf-8")
    upload_path.write_text(upload, encoding="utf-8")
    wx_path.write_text(WXPUSHER_SOURCE, encoding="utf-8")
    print("modified")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: add_daily_seq_wxpusher.py <upstream-dir>", file=sys.stderr)
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
