#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

PATCHABLE = 0
NATIVE_REVIEW = 42
ERROR = 2

CONFIG_NEEDLES = (
    "recording_output_dir",
    "recording_dir",
    "output_directory",
)


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "upstream").resolve()
    util = root / "crates/biliup-cli/src/server/common/util.rs"

    if not util.is_file():
        print(f"probe-error: missing {util}", file=sys.stderr)
        return ERROR

    util_text = util.read_text(encoding="utf-8")

    if "{record_date}" in util_text:
        print("native-review: record_date found in server/common/util.rs")
        return NATIVE_REVIEW

    # Current upstream's flat filename sanitizer explicitly converts '/'.
    # If that invariant changes, stop automatic publishing and review rather
    # than guessing whether the custom path patch is still appropriate.
    if "| '/' |" not in util_text:
        print("native-review: slash sanitization changed")
        return NATIVE_REVIEW

    server_root = root / "crates/biliup-cli/src/server"
    if server_root.is_dir():
        for source in server_root.rglob("*.rs"):
            try:
                text = source.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if any(needle in text for needle in CONFIG_NEEDLES):
                print(f"native-review: recording output config found in {source.relative_to(root)}")
                return NATIVE_REVIEW

    print("patchable: no equivalent upstream recording-path capability detected")
    return PATCHABLE


if __name__ == "__main__":
    raise SystemExit(main())
