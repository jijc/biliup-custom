#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "biliup-custom:preserve-streamer-fields:v1"
NATIVE_REVIEW = 42
OVERRIDE_MODAL_REL = Path("app/ui/OverrideModal.tsx")


class ModifyError(RuntimeError):
    pass


def modify(upstream: Path) -> None:
    modal_path = upstream / OVERRIDE_MODAL_REL
    if not modal_path.is_file():
        raise ModifyError(f"required upstream file missing: {modal_path}")

    text = modal_path.read_text(encoding="utf-8")
    if MARKER in text:
        print("already-modified")
        return

    anchor = "    const entityFields = new Set([\n"
    start = text.find(anchor)
    if start < 0:
        raise ModifyError("OverrideModal entityFields anchor changed")
    end = text.find("    ])", start)
    if end < 0:
        raise ModifyError("OverrideModal entityFields closing anchor changed")
    end += len("    ])")

    block = text[start:end]
    old_filename = "      'filename',"
    old_upload_id = "      'upload_id',"
    new_filename = "      'filename_prefix',"
    new_upload_id = "      'upload_streamers_id',"

    if (
        new_filename in block
        and new_upload_id in block
        and old_filename not in block
        and old_upload_id not in block
    ):
        print("native-review: upstream already preserves current streamer field names")
        raise SystemExit(NATIVE_REVIEW)

    if block.count(old_filename) != 1:
        raise ModifyError(
            f"expected one legacy filename field in OverrideModal, found {block.count(old_filename)}"
        )
    if block.count(old_upload_id) != 1:
        raise ModifyError(
            f"expected one legacy upload_id field in OverrideModal, found {block.count(old_upload_id)}"
        )

    block = block.replace(old_filename, new_filename, 1)
    block = block.replace(old_upload_id, new_upload_id, 1)
    block = block.replace(anchor, f"    // {MARKER}\n" + anchor, 1)
    text = text[:start] + block + text[end:]

    modal_path.write_text(text, encoding="utf-8")
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
