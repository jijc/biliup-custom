#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "biliup-custom:submit-pipeline-recovery:v1"
NATIVE_REVIEW = 42
UPLOAD_REL = Path("crates/biliup-cli/src/server/common/upload.rs")


class ModifyError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ModifyError(f"{label} anchor changed: expected 1 match, got {count}")
    return text.replace(old, new, 1)


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
    raise ModifyError("unbalanced braces")


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


SUBMIT_RECOVERY = r'''// biliup-custom:submit-pipeline-recovery:v1
const COVER_UPLOAD_TIMEOUT_SECS: u64 = 30;
const SUBMIT_VERIFY_TIMEOUT_SECS: u64 = 10;
const SUBMIT_VERIFY_ATTEMPTS: u8 = 3;
const SUBMIT_VERIFY_DELAY_SECS: u64 = 5;

#[derive(Debug)]
enum SubmissionCheck {
    Confirmed { aid: u64, bvid: String },
    NotFound,
    Unknown,
}

async fn submit_once(
    bilibili: &BiliBili,
    studio: &Studio,
    submit_option: &SubmitOption,
) -> AppResult<ResponseData> {
    match submit_option {
        SubmitOption::BCutAndroid => bilibili
            .submit_by_bcut_android(studio, None)
            .await
            .change_context(AppError::Unknown),
        SubmitOption::Web => bilibili
            .submit_by_web(studio, None)
            .await
            .change_context(AppError::Unknown),
        _ => bilibili
            .submit_by_app(studio, None)
            .await
            .change_context(AppError::Unknown),
    }
}

async fn confirm_recent_submission(
    bilibili: &BiliBili,
    title: &str,
    submit_started_at: u64,
) -> SubmissionCheck {
    let mut successful_checks = 0_u8;

    for check in 1..=SUBMIT_VERIFY_ATTEMPTS {
        info!(
            check,
            title,
            timeout_secs = SUBMIT_VERIFY_TIMEOUT_SECS,
            "投稿响应不确定，检查B站远端稿件列表"
        );

        let result = tokio::time::timeout(
            std::time::Duration::from_secs(SUBMIT_VERIFY_TIMEOUT_SECS),
            bilibili.recent_archives("is_pubing,pubed,not_pubed", 1, Some(2)),
        )
        .await;

        match result {
            Ok(Ok(archives)) => {
                successful_checks += 1;
                if let Some(archive) = archives.into_iter().find(|archive| {
                    archive.title == title
                        && archive.ctime >= submit_started_at.saturating_sub(120)
                }) {
                    return SubmissionCheck::Confirmed {
                        aid: archive.aid,
                        bvid: archive.bvid,
                    };
                }
            }
            Ok(Err(err)) => {
                warn!(check, error = ?err, "查询B站远端稿件列表失败");
            }
            Err(_) => {
                warn!(check, "查询B站远端稿件列表超时");
            }
        }

        if check < SUBMIT_VERIFY_ATTEMPTS {
            tokio::time::sleep(std::time::Duration::from_secs(
                SUBMIT_VERIFY_DELAY_SECS,
            ))
            .await;
        }
    }

    if successful_checks >= 2 {
        SubmissionCheck::NotFound
    } else {
        SubmissionCheck::Unknown
    }
}

fn recovered_submit_response(aid: u64, bvid: &str) -> AppResult<ResponseData> {
    serde_json::from_value(serde_json::json!({
        "code": 0,
        "data": {
            "aid": aid,
            "bvid": bvid,
        },
        "message": "OK",
        "ttl": 1,
    }))
    .change_context(AppError::Unknown)
}

pub async fn submit_to_bilibili(
    bilibili: &BiliBili,
    studio: &Studio,
    submit_api: Option<&str>,
) -> AppResult<ResponseData> {
    let submit_option = match submit_api {
        Some(submit) => SubmitOption::from_str(submit).unwrap_or(SubmitOption::App),
        _ => SubmitOption::App,
    };
    let submit_api_name = match &submit_option {
        SubmitOption::BCutAndroid => "bcut_android",
        SubmitOption::Web => "web",
        _ => "app",
    };
    let submit_started_at = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();

    for attempt in 1..=2 {
        info!(
            attempt,
            submit_api = submit_api_name,
            title = %studio.title,
            part_count = studio.videos.len(),
            timeout_secs = SUBMIT_TIMEOUT_SECS,
            "开始提交B站稿件"
        );

        match tokio::time::timeout(
            std::time::Duration::from_secs(SUBMIT_TIMEOUT_SECS),
            submit_once(bilibili, studio, &submit_option),
        )
        .await
        {
            Ok(Ok(result)) => {
                info!(attempt, "Submit successful");
                return Ok(result);
            }
            Ok(Err(err)) => return Err(err),
            Err(_) => {
                error!(
                    attempt,
                    submit_api = submit_api_name,
                    title = %studio.title,
                    part_count = studio.videos.len(),
                    timeout_secs = SUBMIT_TIMEOUT_SECS,
                    "B站最终投稿请求超时"
                );
            }
        }

        match confirm_recent_submission(bilibili, &studio.title, submit_started_at).await {
            SubmissionCheck::Confirmed { aid, bvid } => {
                info!(
                    aid,
                    bvid = %bvid,
                    title = %studio.title,
                    "已从B站稿件列表确认稿件存在"
                );
                let result = recovered_submit_response(aid, &bvid)?;
                info!("Submit successful");
                return Ok(result);
            }
            SubmissionCheck::NotFound if attempt == 1 => {
                warn!(
                    title = %studio.title,
                    part_count = studio.videos.len(),
                    "未发现新稿件，复用已上传视频信息重试最终投稿一次"
                );
            }
            SubmissionCheck::NotFound => {
                return Err(error_stack::Report::new(AppError::Custom(format!(
                    "B站最终投稿连续两次超时，已确认远端未出现新稿件；{} 个视频分P已上传但稿件未提交成功",
                    studio.videos.len()
                ))));
            }
            SubmissionCheck::Unknown => {
                error!(
                    title = %studio.title,
                    "远端稿件状态无法确认，不自动重试"
                );
                return Err(error_stack::Report::new(AppError::Custom(format!(
                    "B站最终投稿请求超时，且远端稿件状态无法确认；为避免重复稿件未自动重试，{} 个视频分P已上传",
                    studio.videos.len()
                ))));
            }
        }
    }

    Err(error_stack::Report::new(AppError::Custom(
        "B站最终投稿未完成".to_string(),
    )))
}
'''


def _modify_submit(text: str) -> str:
    start, end = _function_span(text, "pub async fn submit_to_bilibili")
    current = text[start:end]
    if "biliup-custom:submit-timeout:v1" not in text:
        raise ModifyError("submit timeout modifier must run before submit pipeline recovery")
    if "confirm_recent_submission" in current or "for attempt in 1..=2" in current:
        print("native-review: upstream/final submit already has recovery handling")
        raise SystemExit(NATIVE_REVIEW)

    required = [
        "SUBMIT_TIMEOUT_SECS",
        "SubmitOption::BCutAndroid",
        "SubmitOption::Web",
        "submit_by_bcut_android(studio, None)",
        "submit_by_web(studio, None)",
        "submit_by_app(studio, None)",
        '"B站最终投稿请求超时"',
    ]
    missing = [item for item in required if item not in current]
    if missing:
        raise ModifyError(f"submit_to_bilibili shape changed: missing {missing}")

    return text[:start] + SUBMIT_RECOVERY + text[end:]


def _modify_build_studio(text: str) -> str:
    start, end = _function_span(text, "pub(crate) async fn build_studio")
    function = text[start:end]
    if "COVER_UPLOAD_TIMEOUT_SECS" in function or "B站封面上传超时" in function:
        print("native-review: upstream build_studio already bounds cover upload")
        raise SystemExit(NATIVE_REVIEW)

    open_idx = function.find("{")
    if open_idx < 0:
        raise ModifyError("build_studio opening brace missing")
    insert_at = open_idx + 1
    function = (
        function[:insert_at]
        + '\n    info!(part_count = videos.len(), "开始构建B站稿件信息");'
        + function[insert_at:]
    )

    cover_start = function.find("    if !studio.cover.is_empty()")
    if cover_start < 0:
        raise ModifyError("build_studio cover upload start changed")
    cover_open = function.find("{", cover_start)
    if cover_open < 0:
        raise ModifyError("build_studio cover upload opening brace missing")
    cover_close = _matching_brace(function, cover_open)
    cover_end = cover_close + 1
    if cover_end < len(function) and function[cover_end] == ";":
        cover_end += 1
    if cover_end < len(function) and function[cover_end] == "\n":
        cover_end += 1
    cover_current = function[cover_start:cover_end]
    required_cover_tokens = [
        "std::fs::read(&studio.cover)",
        "bilibili.cover_up(c).await",
        "studio.cover = url;",
    ]
    missing = [token for token in required_cover_tokens if token not in cover_current]
    if missing:
        raise ModifyError(f"build_studio cover upload shape changed: missing {missing}")

    cover_new = '''    if !studio.cover.is_empty() {
        let cover_path = studio.cover.clone();
        info!(timeout_secs = COVER_UPLOAD_TIMEOUT_SECS, "开始上传B站封面");
        match std::fs::read(&cover_path) {
            Ok(cover_bytes) => {
                match tokio::time::timeout(
                    std::time::Duration::from_secs(COVER_UPLOAD_TIMEOUT_SECS),
                    bilibili.cover_up(&cover_bytes),
                )
                .await
                {
                    Ok(Ok(url)) => {
                        studio.cover = url;
                        info!("B站封面上传成功");
                    }
                    Ok(Err(err)) => {
                        warn!(error = ?err, "B站封面上传失败，将继续无封面投稿");
                        studio.cover.clear();
                    }
                    Err(_) => {
                        warn!(
                            timeout_secs = COVER_UPLOAD_TIMEOUT_SECS,
                            "B站封面上传超时，将继续无封面投稿"
                        );
                        studio.cover.clear();
                    }
                }
            }
            Err(err) => {
                warn!(error = ?err, "读取投稿封面失败，将继续无封面投稿");
                studio.cover.clear();
            }
        }
    }
    info!(has_cover = !studio.cover.is_empty(), "B站稿件信息构建完成");
'''
    function = function[:cover_start] + cover_new + function[cover_end:]
    return text[:start] + function + text[end:]


def modify(upstream: Path) -> None:
    path = upstream / UPLOAD_REL
    if not path.is_file():
        raise ModifyError(f"required upstream file missing: {path}")

    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print("already-modified")
        return

    if "use tracing::{error, info};" in text:
        text = _replace_once(
            text,
            "use tracing::{error, info};",
            "use tracing::{error, info, warn};",
            "tracing imports",
        )
    elif "use tracing::{error, info, warn};" not in text:
        raise ModifyError("tracing import shape changed")

    text = _modify_submit(text)
    text = _modify_build_studio(text)
    path.write_text(text, encoding="utf-8")
    print("modified")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: fix_submit_pipeline_recovery.py <upstream-dir>", file=sys.stderr)
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
