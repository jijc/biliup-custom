#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "biliup-custom:manual-upload-feedback:v1"
NATIVE_REVIEW = 42
UPLOAD_MANAGER_REL = Path("app/(app)/upload-manager/page.tsx")
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
            if ch == '"' or ch == "'" or ch == "`":
                state = ch
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
        elif state in ('"', "'", "`"):
            if ch == "\\":
                i += 1
            elif ch == state:
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
        raise ModifyError(f"opening brace not found: {signature}")
    close_idx = _matching_brace(text, open_idx)
    end = close_idx + 1
    return start, end


def _modify_upload_manager(text: str) -> str:
    if MARKER in text:
        return text
    if "上传任务已提交" in text or "Notification.warning" in text[text.find("const handleOk = async"):text.find("const handleCancel")]:
        print("native-review: upstream manual upload UI already has feedback")
        raise SystemExit(NATIVE_REVIEW)

    start, end = _function_span(text, "const handleOk = async () =>")
    indent = text[start:start + len(text[start:]) - len(text[start:].lstrip())]
    replacement = f'''{indent}// {MARKER}
{indent}const handleOk = async () => {{
{indent}  if (selectFiles.length === 0) {{
{indent}    Notification.warning({{
{indent}      title: '请选择录像文件',
{indent}      content: '请至少选择一个要手动投稿的文件',
{indent}      position: 'top',
{indent}      duration: 3,
{indent}    }})
{indent}    return
{indent}  }}

{indent}  const fileCount = selectFiles.length
{indent}  try {{
{indent}    await sendRequest('/v1/uploads', {{
{indent}      arg: {{
{indent}        files: selectFiles.map(String),
{indent}        template_id: selectEntity?.id,
{indent}      }},
{indent}    }})
{indent}    Notification.success({{
{indent}      title: '上传任务已提交',
{indent}      content: `已将 ${{fileCount}} 个文件提交到后台上传，可在实时日志查看进度`,
{indent}      position: 'top',
{indent}      duration: 4,
{indent}    }})
{indent}    setVisibleModal(false)
{indent}    setSelectFiles([])
{indent}    setTransferData([])
{indent}  }} catch (e: any) {{
{indent}    Notification.error({{
{indent}      title: '手动上传失败',
{indent}      content: e?.message || '上传请求失败',
{indent}      position: 'top',
{indent}      duration: 6,
{indent}    }})
{indent}  }}
{indent}}}'''
    return text[:start] + replacement + text[end:]


def _modify_endpoints(text: str) -> str:
    if f"// {MARKER}" in text:
        return text

    start, end = _function_span(text, "pub async fn post_uploads")
    function = text[start:end]
    if "该投稿模板上传器为 Noop，不能手动上传" in function:
        print("native-review: upstream manual upload endpoint already fails loudly")
        raise SystemExit(NATIVE_REVIEW)

    noop_old = '''    if upload_config.is_noop_uploader() {
        info!(
            uploader = ?upload_config.uploader,
            "Skipping page upload because uploader is Noop"
        );
        return Ok(Json(json!({})));
    }
'''
    if function.count(noop_old) != 1:
        raise ModifyError("manual upload Noop anchor changed")
    noop_new = f'''    // {MARKER}
    if upload_config.is_noop_uploader() {{
        tracing::warn!(
            template_id = upload_config.id,
            uploader = ?upload_config.uploader,
            "Manual page upload rejected because uploader is Noop"
        );
        return Err((
            StatusCode::BAD_REQUEST,
            "该投稿模板上传器为 Noop，不能手动上传",
        )
            .into_response());
    }}
'''
    function = function.replace(noop_old, noop_new, 1)

    info_old = '    info!("通过页面开始上传");\n'
    if function.count(info_old) != 1:
        raise ModifyError("manual upload start log anchor changed")
    info_new = '''    let template_id = upload_config.id;
    let file_count = files.len();
    info!(template_id, file_count, "通过页面开始上传");
'''
    function = function.replace(info_old, info_new, 1)

    error_old = '            tracing::error!(template_id = upload_config.id, "页面上传失败");\n'
    if function.count(error_old) != 1:
        raise ModifyError("manual upload error log anchor changed")
    function = function.replace(
        error_old,
        '            tracing::error!(template_id, file_count, "页面上传失败");\n',
        1,
    )

    response_old = "    Ok(Json(serde_json::json!({})))\n"
    if function.count(response_old) != 1:
        raise ModifyError("manual upload accepted response anchor changed")
    response_new = '''    Ok(Json(serde_json::json!({
        "accepted": true,
        "template_id": template_id,
        "file_count": file_count,
    })))
'''
    function = function.replace(response_old, response_new, 1)

    return text[:start] + function + text[end:]


def modify(upstream: Path) -> None:
    page_path = upstream / UPLOAD_MANAGER_REL
    endpoints_path = upstream / ENDPOINTS_REL
    for path in (page_path, endpoints_path):
        if not path.is_file():
            raise ModifyError(f"required upstream file missing: {path}")

    page = page_path.read_text(encoding="utf-8")
    endpoints = endpoints_path.read_text(encoding="utf-8")
    page_marked = MARKER in page
    endpoint_marked = f"// {MARKER}" in endpoints
    if page_marked and endpoint_marked:
        print("already-modified")
        return
    if page_marked != endpoint_marked:
        raise ModifyError("partial previous manual-upload-feedback modification detected")

    page = _modify_upload_manager(page)
    endpoints = _modify_endpoints(endpoints)
    page_path.write_text(page, encoding="utf-8")
    endpoints_path.write_text(endpoints, encoding="utf-8")
    print("modified")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: fix_manual_upload_feedback.py <upstream-dir>", file=sys.stderr)
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
