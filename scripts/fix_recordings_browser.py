#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

NATIVE_REVIEW = 42
ENDPOINTS_MARKER = "// biliup-custom:recordings-browser:v1"
ROUTER_MARKER = "// biliup-custom:recordings-static:v1"
HISTORY_MARKER = "{/* biliup-custom:recordings-history:v1 */}"
UPLOAD_PICKER_MARKER = "// biliup-custom:recordings-upload-picker:v1"

ENDPOINTS_REL = Path("crates/biliup-cli/src/server/api/endpoints.rs")
ROUTER_REL = Path("crates/biliup-cli/src/server/router.rs")
HISTORY_REL = Path("app/(app)/history/page.tsx")
UPLOAD_MANAGER_REL = Path("app/(app)/upload-manager/page.tsx")


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


GET_VIDEOS = r'''// biliup-custom:recordings-browser:v1
const BILIUP_CUSTOM_RECORDINGS_ROOT: &str = "/recordings";

pub async fn get_videos() -> Result<Json<Vec<serde_json::Value>>, Response> {
    let media_extensions = ["mp4", "flv", "3gp", "webm", "mkv", "ts"];
    let recordings_root = std::path::Path::new(BILIUP_CUSTOM_RECORDINGS_ROOT);
    let mut file_list = Vec::new();
    let mut pending_dirs = vec![recordings_root.to_path_buf()];

    // Recordings are stored as /recordings/<streamer>/<logical-date>/file.
    // Walk that tree iteratively so deeply nested files remain visible without
    // using recursive async functions. Symlinks are never traversed.
    while let Some(dir) = pending_dirs.pop() {
        let Ok(mut entries) = fs::read_dir(&dir).await else {
            continue;
        };
        while let Ok(Some(entry)) = entries.next_entry().await {
            let Ok(file_type) = entry.file_type().await else {
                continue;
            };
            if file_type.is_symlink() {
                continue;
            }
            let path = entry.path();
            if file_type.is_dir() {
                pending_dirs.push(path);
                continue;
            }
            if !file_type.is_file() {
                continue;
            }

            let allowed = path
                .extension()
                .and_then(|ext| ext.to_str())
                .is_some_and(|ext| {
                    media_extensions
                        .iter()
                        .any(|allowed| ext.eq_ignore_ascii_case(allowed))
                });
            if !allowed {
                // .part and every non-media file naturally land here.
                continue;
            }

            let Ok(relative) = path.strip_prefix(recordings_root) else {
                continue;
            };
            let name = relative.to_string_lossy().replace('\\', "/");
            let Ok(metadata) = entry.metadata().await else {
                continue;
            };
            let mtime = metadata
                .modified()
                .ok()
                .and_then(|time| time.duration_since(UNIX_EPOCH).ok())
                .map(|d| d.as_secs())
                .unwrap_or(0);

            file_list.push(serde_json::json!({
                "key": name,
                "name": name,
                "updateTime": mtime,
                "size": metadata.len(),
            }));
        }
    }

    // Relative paths sort naturally as streamer -> YYYY-MM-DD -> filename.
    file_list.sort_by(|a, b| {
        a.get("name")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .cmp(b.get("name").and_then(|v| v.as_str()).unwrap_or_default())
    });
    Ok(Json(file_list))
}
'''


STATIC_HANDLER = r'''// biliup-custom:recordings-static:v1
async fn using_serve_file_from_a_route(
    axum::extract::Path(path): axum::extract::Path<String>,
    request: Request<Body>,
) -> Response {
    // Keep the upstream log-serving behavior for known root log files, while
    // all media paths are resolved exclusively inside /recordings.
    let resolved = if ALLOWED_LOG_FILES.contains(&path.as_str()) {
        let root = match std::env::current_dir() {
            Ok(root) => root,
            Err(_) => return StatusCode::INTERNAL_SERVER_ERROR.into_response(),
        };
        resolve_static_path(&root, &path)
    } else {
        resolve_recording_media_path(std::path::Path::new("/recordings"), &path)
    };

    let path = match resolved {
        Ok(path) => path,
        Err(StaticPathError::Invalid) => return StatusCode::BAD_REQUEST.into_response(),
        Err(StaticPathError::NotFound) => return StatusCode::NOT_FOUND.into_response(),
    };

    ServeFile::new(path).oneshot(request).await.into_response()
}
'''


RECORDINGS_RESOLVER = r'''// Resolve a media path relative to the dedicated recordings root. Nested
// streamer/date components are allowed, but traversal, absolute paths, unsafe
// backslash separators, non-media files, and symlink escapes are rejected.
pub(crate) fn resolve_recording_media_path(
    root: &std::path::Path,
    requested: &str,
) -> Result<std::path::PathBuf, StaticPathError> {
    use std::path::{Component, Path};

    if requested.is_empty() || requested.contains('\\') {
        return Err(StaticPathError::Invalid);
    }
    let requested_path = Path::new(requested);
    if requested_path.is_absolute() {
        return Err(StaticPathError::Invalid);
    }

    let mut saw_component = false;
    for component in requested_path.components() {
        match component {
            Component::Normal(_) => saw_component = true,
            Component::ParentDir | Component::CurDir | Component::RootDir | Component::Prefix(_) => {
                return Err(StaticPathError::Invalid);
            }
        }
    }
    if !saw_component {
        return Err(StaticPathError::Invalid);
    }

    let allowed = requested_path
        .extension()
        .and_then(|extension| extension.to_str())
        .is_some_and(|extension| {
            ALLOWED_MEDIA_EXTENSIONS
                .iter()
                .any(|allowed| extension.eq_ignore_ascii_case(allowed))
        });
    if !allowed {
        return Err(StaticPathError::Invalid);
    }

    let root = root.canonicalize().map_err(|_| StaticPathError::NotFound)?;
    let canonical = root
        .join(requested_path)
        .canonicalize()
        .map_err(|_| StaticPathError::NotFound)?;
    if !canonical.starts_with(&root) || !canonical.is_file() {
        return Err(StaticPathError::Invalid);
    }
    Ok(canonical)
}

#[cfg(test)]
mod biliup_custom_recordings_path_tests {
    use super::{StaticPathError, resolve_recording_media_path};
    use std::fs;

    #[test]
    fn nested_recording_media_is_allowed_but_escape_paths_are_rejected() {
        let root = tempfile::tempdir().unwrap();
        let day = root.path().join("梦俊/2026-08-26");
        fs::create_dir_all(&day).unwrap();
        let video = day.join("01-test.mp4");
        fs::write(&video, b"video").unwrap();

        assert_eq!(
            resolve_recording_media_path(root.path(), "梦俊/2026-08-26/01-test.mp4").unwrap(),
            video
        );
        for invalid in [
            "../secret.mp4",
            "/etc/passwd.mp4",
            "梦俊/../secret.mp4",
            "梦俊\\2026-08-26\\01-test.mp4",
            "梦俊/2026-08-26/01-test.mp4.part",
        ] {
            assert_eq!(
                resolve_recording_media_path(root.path(), invalid),
                Err(StaticPathError::Invalid)
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn recording_media_rejects_symlink_escape() {
        use std::os::unix::fs::symlink;

        let root = tempfile::tempdir().unwrap();
        let outside = tempfile::NamedTempFile::new().unwrap();
        let nested = root.path().join("主播/2026-08-26");
        fs::create_dir_all(&nested).unwrap();
        let link = nested.join("escape.mp4");
        symlink(outside.path(), &link).unwrap();

        assert_eq!(
            resolve_recording_media_path(root.path(), "主播/2026-08-26/escape.mp4"),
            Err(StaticPathError::Invalid)
        );
    }
}
'''


def _modify_endpoints(text: str) -> str:
    if ENDPOINTS_MARKER in text:
        return text

    if "BILIUP_CUSTOM_RECORDINGS_ROOT" in text or "pending_dirs" in text:
        print("native-review: upstream appears to have recordings-tree browsing")
        raise SystemExit(NATIVE_REVIEW)

    text = _replace_function(text, "pub async fn get_videos", GET_VIDEOS)

    start, end = _function_span(text, "pub async fn post_uploads")
    fn = text[start:end]
    old_root = '''    let root = std::env::current_dir()
        .change_context(AppError::Unknown)
        .map_err(report_to_response)?;
'''
    if old_root not in fn:
        # Small fixtures used by unit tests can use unwrap while upstream uses
        # error-stack. Support both without weakening the real-source guard.
        old_root = "    let root = std::env::current_dir().unwrap();\n"
    if old_root not in fn:
        raise ModifyError("post_uploads current-dir anchor changed")
    fn = fn.replace(
        old_root,
        "    let recordings_root = std::path::Path::new(BILIUP_CUSTOM_RECORDINGS_ROOT);\n",
        1,
    )
    old_resolver = "crate::server::router::resolve_media_path(&root, file)"
    if old_resolver not in fn:
        raise ModifyError("post_uploads media resolver anchor changed")
    fn = fn.replace(
        old_resolver,
        "crate::server::router::resolve_recording_media_path(recordings_root, file)",
        1,
    )
    text = text[:start] + fn + text[end:]
    return text


def _modify_router(text: str) -> str:
    if ROUTER_MARKER in text:
        return text

    if "resolve_recording_media_path" in text:
        print("native-review: upstream appears to support nested recording paths")
        raise SystemExit(NATIVE_REVIEW)

    route_old = '.route("/static/{path}", get(using_serve_file_from_a_route))'
    if route_old not in text:
        raise ModifyError("static route anchor changed")
    text = text.replace(
        route_old,
        '.route("/static/{*path}", get(using_serve_file_from_a_route))',
        1,
    )
    text = _replace_function(text, "async fn using_serve_file_from_a_route", STATIC_HANDLER)

    _, resolver_end = _function_span(text, "pub(crate) fn resolve_media_path")
    text = text[:resolver_end] + "\n" + RECORDINGS_RESOLVER + text[resolver_end:]
    return text


def _modify_history(text: str) -> str:
    if "biliup-custom:recordings-history:v1" in text:
        return text
    old = "'/static/' + fileName"
    if old not in text:
        if "encodeURIComponent" in text and "split('/')" in text:
            print("native-review: upstream history already safely encodes nested media paths")
            raise SystemExit(NATIVE_REVIEW)
        raise ModifyError("history media URL anchor changed")
    safe_path = "(fileName?.split('/').map(encodeURIComponent).join('/') ?? '')"
    text = text.replace(old, "'/static/' + " + safe_path, 1)
    anchor = "          <Players"
    if anchor not in text:
        # Unit fixture uses a compact layout.
        anchor = "    <Players"
    if anchor not in text:
        raise ModifyError("history player anchor changed")
    return text.replace(anchor, f"          {HISTORY_MARKER}\n" + anchor, 1)


def _modify_upload_manager(text: str) -> str:
    if "biliup-custom:recordings-upload-picker:v1" in text:
        return text
    anchor = "  const { data: fileList } = useSWR<FileList[]>('/v1/videos', fetcher)"
    if anchor not in text:
        raise ModifyError("upload-manager file list anchor changed")
    return text.replace(anchor, f"  {UPLOAD_PICKER_MARKER}\n" + anchor, 1)


def modify(upstream: Path) -> None:
    paths = {
        ENDPOINTS_REL: _modify_endpoints,
        ROUTER_REL: _modify_router,
        HISTORY_REL: _modify_history,
        UPLOAD_MANAGER_REL: _modify_upload_manager,
    }
    for rel, modifier in paths.items():
        path = upstream / rel
        if not path.is_file():
            raise ModifyError(f"required upstream file missing: {path}")
        original = path.read_text(encoding="utf-8")
        updated = modifier(original)
        path.write_text(updated, encoding="utf-8")
    print("modified")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: fix_recordings_browser.py <upstream-dir>", file=sys.stderr)
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
