#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

MODAL_MARKER = "biliup-custom:preserve-streamer-fields:v1"
SCHEMA_MARKER = "biliup-custom:live-streamer-schema:v1"
UPLOAD_TEMPLATE_MARKER = "biliup-custom:upload-template-bool-roundtrip:v1"
NATIVE_REVIEW = 42
OVERRIDE_MODAL_REL = Path("app/ui/OverrideModal.tsx")
API_STREAMER_REL = Path("app/lib/api-streamer.ts")
UPLOAD_TEMPLATE_EDIT_REL = Path("app/(app)/upload-manager/edit/page.tsx")


class ModifyError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ModifyError(f"expected one {label} anchor, found {count}")
    return text.replace(old, new, 1)


def _modify_override_modal(text: str) -> str:
    anchor = "    const entityFields = new Set([\n"
    start = text.find(anchor)
    if start < 0:
        raise ModifyError("OverrideModal entityFields anchor changed")
    end = text.find("    ])", start)
    if end < 0:
        raise ModifyError("OverrideModal entityFields closing anchor changed")
    end += len("    ])")

    block = text[start:end]
    replacements = {
        "      'filename',": "      'filename_prefix',",
        "      'upload_id',": "      'upload_streamers_id',",
    }
    for old, new in replacements.items():
        if block.count(old) != 1:
            raise ModifyError(f"expected one legacy field {old.strip()} in OverrideModal")
        block = block.replace(old, new, 1)

    for stale in ("      'split_time',\n", "      'split_size',\n"):
        if block.count(stale) != 1:
            raise ModifyError(f"expected one stale OverrideModal field {stale.strip()}")
        block = block.replace(stale, "", 1)

    # Keep response/UI-only fields out of override_cfg. The streamers page drops
    # them before PUT; if they are treated as override keys they silently pollute
    # ConfigPatch instead.
    status_anchor = "      'status',\n"
    if status_anchor not in block:
        raise ModifyError("OverrideModal status field anchor changed")
    block = block.replace(
        status_anchor,
        status_anchor + "      'upload_status',\n      'statusTag',\n",
        1,
    )

    block = block.replace(anchor, f"    // {MODAL_MARKER}\n" + anchor, 1)
    text = text[:start] + block + text[end:]

    # Fail-safe against future top-level fields being added by the backend.
    # The override dialog edits only a subset, so untouched fields must be
    # carried forward from the original API entity instead of disappearing from
    # the full-row PUT payload.
    text = _replace_once(
        text,
        "      await onOk(cleanValues)",
        "      await onOk({ ...entity, ...cleanValues })",
        "OverrideModal safe entity merge",
    )
    return text


def _modify_api_schema(text: str) -> str:
    legacy_streamer_fields = (
        "\tfilename: string;\n"
        "\tsplit_time?: number;\n"
        "\tsplit_size?: number;\n"
        "\tupload_id?: number;"
    )
    if legacy_streamer_fields not in text:
        legacy_streamer_fields = (
            "  filename: string;\n"
            "  split_time?: number;\n"
            "  split_size?: number;\n"
            "  upload_id?: number;"
        )
    if legacy_streamer_fields not in text:
        raise ModifyError("LiveStreamerEntity legacy field block changed")

    indent = "\t" if legacy_streamer_fields.startswith("\t") else "  "
    replacement = (
        f"{indent}// {SCHEMA_MARKER}\n"
        f"{indent}filename_prefix?: string | null;\n"
        f"{indent}upload_streamers_id?: number | null;"
    )
    text = text.replace(legacy_streamer_fields, replacement, 1)

    # The GET model serializes these flags as booleans, while the current create
    # and update payloads still send 0/1 for the InsertUploadStreamer API. Keep
    # the shared UI type compatible with both shapes and normalize reads in the
    # edit page below.
    for field in ("up_selection_reply", "up_close_reply", "up_close_danmu"):
        old_tab = f"\t{field}: number;"
        old_spaces = f"  {field}: number;"
        if old_tab in text:
            text = text.replace(old_tab, f"\t{field}: boolean | number;", 1)
        elif old_spaces in text:
            text = text.replace(old_spaces, f"  {field}: boolean | number;", 1)
        else:
            raise ModifyError(f"StudioEntity field type changed: {field}")
    return text


def _modify_upload_template_edit(text: str) -> str:
    if "let uploadStreamers = {" not in text:
        raise ModifyError("upload template edit initializer changed")

    replacements = {
        "data.up_close_danmu === 1": "Boolean(data.up_close_danmu)",
        "data.up_close_reply === 1": "Boolean(data.up_close_reply)",
        "data.up_selection_reply === 1": "Boolean(data.up_selection_reply)",
    }
    for old, new in replacements.items():
        text = _replace_once(text, old, new, f"upload template boolean {old}")

    text = text.replace(
        "  let uploadStreamers = {",
        f"  // {UPLOAD_TEMPLATE_MARKER}\n  let uploadStreamers = {{",
        1,
    ) if "  let uploadStreamers = {" in text else text.replace(
        "let uploadStreamers = {",
        f"// {UPLOAD_TEMPLATE_MARKER}\nlet uploadStreamers = {{",
        1,
    )
    return text


def modify(upstream: Path) -> None:
    paths = {
        "modal": upstream / OVERRIDE_MODAL_REL,
        "schema": upstream / API_STREAMER_REL,
        "upload_edit": upstream / UPLOAD_TEMPLATE_EDIT_REL,
    }
    for path in paths.values():
        if not path.is_file():
            raise ModifyError(f"required upstream file missing: {path}")

    modal = paths["modal"].read_text(encoding="utf-8")
    schema = paths["schema"].read_text(encoding="utf-8")
    upload_edit = paths["upload_edit"].read_text(encoding="utf-8")

    marked = (
        MODAL_MARKER in modal,
        SCHEMA_MARKER in schema,
        UPLOAD_TEMPLATE_MARKER in upload_edit,
    )
    if all(marked):
        print("already-modified")
        return
    if any(marked):
        raise ModifyError("partial previous frontend data-safety modification detected")

    if "filename_prefix?: string | null;" in schema or "Boolean(data.up_close_danmu)" in upload_edit:
        print("native-review: upstream frontend data model changed")
        raise SystemExit(NATIVE_REVIEW)

    modal = _modify_override_modal(modal)
    schema = _modify_api_schema(schema)
    upload_edit = _modify_upload_template_edit(upload_edit)

    paths["modal"].write_text(modal, encoding="utf-8")
    paths["schema"].write_text(schema, encoding="utf-8")
    paths["upload_edit"].write_text(upload_edit, encoding="utf-8")
    print("modified")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: fix_override_streamer_fields.py <upstream-dir>", file=sys.stderr)
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
