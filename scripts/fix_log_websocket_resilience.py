#!/usr/bin/env python3
from __future__ import annotations

import re
import sys
from pathlib import Path

MARKER = "biliup-custom:log-websocket-resilience:v1"
NATIVE_REVIEW = 42
WS_REL = Path("crates/biliup-cli/src/server/api/ws.rs")
LOGVIEWER_REL = Path("app/(app)/logviewer/page.tsx")


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
            if ch in ('"', "'", "`"):
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
    return start, close_idx + 1


def _modify_ws(text: str) -> str:
    if MARKER in text:
        return text

    if "is_expected_log_ws_disconnect" in text or "Duration::from_secs(25)" in text:
        print("native-review: upstream log websocket already has resilience handling")
        raise SystemExit(NATIVE_REVIEW)

    helper_anchor = "async fn websocket_logs(mut ws: WebSocket, query: LogsQuery) {\n"
    helper = f'''// {MARKER}\nfn is_expected_log_ws_disconnect(error: &str) -> bool {{\n    error.contains("without closing handshake")\n        || error.contains("Connection reset")\n        || error.contains("Connection closed")\n}}\n\n{helper_anchor}'''
    text = _replace_once(text, helper_anchor, helper, "websocket helper")

    tick_old = '''    // 心跳/轮询间隔\n    let mut tick = interval(Duration::from_millis(500));\n    tick.set_missed_tick_behavior(MissedTickBehavior::Skip);\n'''
    tick_new = '''    // 日志文件轮询间隔\n    let mut tick = interval(Duration::from_millis(500));\n    tick.set_missed_tick_behavior(MissedTickBehavior::Skip);\n\n    // WebSocket 协议级心跳，防止反向代理或中间网络设备清理空闲连接。\n    let mut heartbeat = interval(Duration::from_secs(25));\n    heartbeat.set_missed_tick_behavior(MissedTickBehavior::Skip);\n    // tokio::time::interval 的第一次 tick 会立即完成，先消费掉，\n    // 让第一枚 Ping 在 25 秒后发送。\n    heartbeat.tick().await;\n'''
    text = _replace_once(text, tick_old, tick_new, "websocket poll interval")

    error_old = '''                    Some(Err(e)) => {\n                        error!("WebSocket连接错误: {}", e);\n                        break;\n                    }\n'''
    error_new = '''                    Some(Err(e)) => {\n                        let error_text = e.to_string();\n                        if is_expected_log_ws_disconnect(&error_text) {\n                            debug!("WebSocket日志连接已断开: {}", error_text);\n                        } else {\n                            error!("WebSocket连接错误: {}", error_text);\n                        }\n                        break;\n                    }\n'''
    text = _replace_once(text, error_old, error_new, "websocket receive error")

    tick_branch = '''            _ = tick.tick() => {\n'''
    heartbeat_branch = '''            _ = heartbeat.tick() => {\n                if let Err(e) = ws.send(Message::Ping(Vec::new().into())).await {\n                    debug!("WebSocket心跳发送失败，连接已断开: {}", e);\n                    break;\n                }\n            }\n\n            _ = tick.tick() => {\n'''
    text = _replace_once(text, tick_branch, heartbeat_branch, "websocket select heartbeat")
    return text


def _modify_logviewer(text: str) -> str:
    if MARKER in text:
        return text

    if "reconnectTimerRef" in text or "allowReconnectRef" in text:
        print("native-review: upstream log viewer already has reconnect handling")
        raise SystemExit(NATIVE_REVIEW)

    refs_old = '''  const wsRef = useRef<WebSocket | null>(null)\n  const logContainerRef = useRef<HTMLDivElement>(null)\n'''
    refs_new = f'''  const wsRef = useRef<WebSocket | null>(null)\n  // {MARKER}\n  const reconnectTimerRef = useRef<number | null>(null)\n  const allowReconnectRef = useRef(true)\n  const logContainerRef = useRef<HTMLDivElement>(null)\n'''
    text = _replace_once(text, refs_old, refs_new, "logviewer refs")

    start, end = _function_span(text, "const connectWebSocket = () =>")
    current = text[start:end]
    required_tokens = (
        "new WebSocket(wsUrl)",
        "ws.onopen = () =>",
        "ws.onmessage = (event) =>",
        "ws.onerror = (error) =>",
        "ws.onclose = () =>",
        "NEXT_PUBLIC_API_SERVER",
        "/v1/ws/logs?file=${activeTab}.log",
    )
    missing = [token for token in required_tokens if token not in current]
    if missing:
        raise ModifyError(f"logviewer connectWebSocket shape changed: missing {missing}")

    connect_replacement = '''  const connectWebSocket = () => {
    if (reconnectTimerRef.current !== null) {
      window.clearTimeout(reconnectTimerRef.current)
      reconnectTimerRef.current = null
    }

    setIsLoading(true)
    setLogs([])

    // 关闭现有连接。旧 socket 的 onclose 会通过身份检查被忽略。
    if (wsRef.current) {
      wsRef.current.close()
    }

    const isDev = process.env.NODE_ENV === 'development';
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const server = isDev
      ? process.env.NEXT_PUBLIC_API_SERVER?.replace(/^http/, 'ws')
      : `${protocol}//${window.location.host}`;
    const wsUrl = `${server}/v1/ws/logs?file=${activeTab}.log`;

    const ws = new WebSocket(wsUrl)
    wsRef.current = ws

    ws.onopen = () => {
      if (wsRef.current !== ws) return
      setIsConnected(true)
      setIsLoading(false)
      Toast.success('日志连接已建立')
    }

    ws.onmessage = (event) => {
      if (wsRef.current !== ws) return
      setLogs(prev => [...prev, event.data])
    }

    ws.onerror = (error) => {
      if (wsRef.current !== ws) return
      console.error('WebSocket错误:', error)
      // onclose 负责统一调度自动重连，避免 error/close 双重重连。
      setIsLoading(false)
    }

    ws.onclose = () => {
      // 已经被新连接替代的旧 socket 不得触发重连。
      if (wsRef.current !== ws) return

      setIsConnected(false)
      setIsLoading(false)
      console.log('WebSocket连接已关闭')

      if (!allowReconnectRef.current) return
      reconnectTimerRef.current = window.setTimeout(() => {
        reconnectTimerRef.current = null
        if (allowReconnectRef.current && wsRef.current === ws) {
          console.log('重新连接实时日志 WebSocket')
          connectWebSocket()
        }
      }, 3000)
    }
  }'''
    text = text[:start] + connect_replacement + text[end:]

    effect_pattern = re.compile(
        r"  useEffect\(\(\) => \{\n    connectWebSocket\(\)\n.*?  \}, \[activeTab\]\)\n",
        re.DOTALL,
    )
    matches = list(effect_pattern.finditer(text))
    if len(matches) != 1:
        raise ModifyError(f"logviewer activeTab effect changed: expected 1 match, got {len(matches)}")
    effect_replacement = '''  useEffect(() => {
    allowReconnectRef.current = true
    connectWebSocket()

    return () => {
      // 切换日志文件或组件卸载时，不应由旧连接触发自动重连。
      allowReconnectRef.current = false
      if (reconnectTimerRef.current !== null) {
        window.clearTimeout(reconnectTimerRef.current)
        reconnectTimerRef.current = null
      }

      const currentWs = wsRef.current
      wsRef.current = null
      if (currentWs) {
        console.log('主动关闭WebSocket连接')
        currentWs.close()
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeTab])
'''
    text = effect_pattern.sub(effect_replacement, text, count=1)
    return text


def modify(upstream: Path) -> None:
    ws_path = upstream / WS_REL
    page_path = upstream / LOGVIEWER_REL
    for path in (ws_path, page_path):
        if not path.is_file():
            raise ModifyError(f"required upstream file missing: {path}")

    ws_text = ws_path.read_text(encoding="utf-8")
    page_text = page_path.read_text(encoding="utf-8")
    ws_marked = MARKER in ws_text
    page_marked = MARKER in page_text
    if ws_marked and page_marked:
        print("already-modified")
        return
    if ws_marked != page_marked:
        raise ModifyError("partial previous log-websocket-resilience modification detected")

    ws_path.write_text(_modify_ws(ws_text), encoding="utf-8")
    page_path.write_text(_modify_logviewer(page_text), encoding="utf-8")
    print("modified")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: fix_log_websocket_resilience.py <upstream-dir>", file=sys.stderr)
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
