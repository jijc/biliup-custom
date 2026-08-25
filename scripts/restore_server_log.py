#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

MARKER = "// biliup-custom:server-log:v1"
NATIVE_REVIEW = 42
SERVER_REL = Path("crates/stream-gears/src/server.rs")


class ModifyError(RuntimeError):
    pass


WEB_LOG_LAYER = r'''    // biliup-custom:server-log:v1
    // The Docker/Python entrypoint runs stream_gears.main_loop -> server::_main,
    // not crates/biliup-cli/src/main.rs. Keep the existing download.log writer
    // and add the file expected by the WebUI's main-program log viewer.
    let web_log_appender = tracing_appender::rolling::never("", "ds_update.log");
    let (web_log_writer, _web_log_guard) = tracing_appender::non_blocking(web_log_appender);
    let web_log_layer = tracing_subscriber::fmt::layer()
        .with_writer(web_log_writer)
        .with_timer(local_time.clone())
        .with_target(true)
        .with_thread_ids(true)
        .with_file(true)
        .with_line_number(true)
        .with_ansi(false);

'''


def modify(upstream: Path) -> None:
    server_path = upstream / SERVER_REL
    if not server_path.is_file():
        raise ModifyError(f"required upstream file missing: {server_path}")

    server = server_path.read_text(encoding="utf-8")
    if MARKER in server:
        print("already-modified")
        return

    # If upstream begins writing the WebUI's expected log itself, stop instead
    # of stacking a duplicate writer on top of a native fix.
    if '"ds_update.log"' in server:
        print("native-review: stream-gears server already references ds_update.log")
        raise SystemExit(NATIVE_REVIEW)

    layer_anchor = "    let file_layer = tracing_subscriber::fmt::layer()"
    layer_idx = server.find(layer_anchor)
    if layer_idx < 0:
        raise ModifyError("stream-gears file_layer anchor changed")
    server = server[:layer_idx] + WEB_LOG_LAYER + server[layer_idx:]

    subscriber_start = server.find("    let subscriber = tracing_subscriber::registry()")
    subscriber_end = server.find("    subscriber.init();", subscriber_start)
    if subscriber_start < 0 or subscriber_end < 0:
        raise ModifyError("stream-gears subscriber block changed")

    subscriber_block = server[subscriber_start:subscriber_end]
    chain_end = subscriber_block.rfind(");")
    if chain_end < 0:
        raise ModifyError("stream-gears subscriber chain terminator changed")
    if ".with(web_log_layer)" in subscriber_block:
        raise ModifyError("unexpected partial server-log modification detected")

    subscriber_block = (
        subscriber_block[:chain_end]
        + ")\n        .with(web_log_layer);"
        + subscriber_block[chain_end + 2 :]
    )
    server = server[:subscriber_start] + subscriber_block + server[subscriber_end:]

    server_path.write_text(server, encoding="utf-8")
    print("modified")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: restore_server_log.py <upstream-dir>", file=sys.stderr)
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
