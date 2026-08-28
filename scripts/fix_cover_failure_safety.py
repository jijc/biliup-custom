#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "biliup-custom:cover-failure-safety:v1"
RECOVERY_MARKER = "biliup-custom:submit-pipeline-recovery:v1"
UPLOAD_REL = Path("crates/biliup-cli/src/server/common/upload.rs")


class ModifyError(RuntimeError):
    pass


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise ModifyError(f"{label} anchor changed: expected 1 match, got {count}")
    return text.replace(old, new, 1)


def modify(upstream: Path) -> None:
    path = upstream / UPLOAD_REL
    if not path.is_file():
        raise ModifyError(f"required upstream file missing: {path}")

    text = path.read_text(encoding="utf-8")
    if MARKER in text:
        print("already-modified")
        return
    if RECOVERY_MARKER not in text:
        raise ModifyError("submit pipeline recovery modifier must run before cover failure safety")

    text = _replace_once(
        text,
        f"// {RECOVERY_MARKER}\n",
        f"// {RECOVERY_MARKER}\n// {MARKER}\n",
        "marker",
    )

    text = _replace_once(
        text,
        '''                    Ok(Err(err)) => {
                        warn!(error = ?err, "B站封面上传失败，将继续无封面投稿");
                        studio.cover.clear();
                    }
''',
        '''                    Ok(Err(err)) => {
                        error!(error = ?err, "B站封面上传失败，停止本次投稿");
                        return Err(error_stack::Report::new(AppError::Custom(
                            "B站封面上传失败，停止本次投稿".to_string(),
                        )));
                    }
''',
        "cover upload failure",
    )

    text = _replace_once(
        text,
        '''                    Err(_) => {
                        warn!(
                            timeout_secs = COVER_UPLOAD_TIMEOUT_SECS,
                            "B站封面上传超时，将继续无封面投稿"
                        );
                        studio.cover.clear();
                    }
''',
        '''                    Err(_) => {
                        error!(
                            timeout_secs = COVER_UPLOAD_TIMEOUT_SECS,
                            "B站封面上传超时，停止本次投稿"
                        );
                        return Err(error_stack::Report::new(AppError::Custom(format!(
                            "B站封面上传超时（{}秒），停止本次投稿",
                            COVER_UPLOAD_TIMEOUT_SECS
                        ))));
                    }
''',
        "cover upload timeout",
    )

    text = _replace_once(
        text,
        '''            Err(err) => {
                warn!(error = ?err, "读取投稿封面失败，将继续无封面投稿");
                studio.cover.clear();
            }
''',
        '''            Err(err) => {
                error!(error = ?err, "读取投稿封面失败，停止本次投稿");
                return Err(error_stack::Report::new(AppError::Custom(
                    "读取投稿封面失败，停止本次投稿".to_string(),
                )));
            }
''',
        "cover read failure",
    )

    if "studio.cover.clear()" in text:
        raise ModifyError("unsafe cover clear remains after safety modifier")

    path.write_text(text, encoding="utf-8")
    print("modified")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: fix_cover_failure_safety.py <upstream-dir>", file=sys.stderr)
        return 2
    try:
        modify(Path(argv[1]))
        return 0
    except ModifyError as exc:
        print(f"modifier-error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
