#!/usr/bin/env python3
from __future__ import annotations

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

    connect_start_old = '''  const connectWebSocket = () => {\n    setIsLoading(true)\n    setLogs([])\n\n    // 关闭现有连接\n'''
    connect_start_new = '''  const connectWebSocket = () => {\n    if (reconnectTimerRef.current !== null) {\n      window.clearTimeout(reconnectTimerRef.current)\n      reconnectTimerRef.current = null\n    }\n\n    setIsLoading(true)\n    setLogs([])\n\n    // 关闭现有连接\n'''
    text = _replace_once(text, connect_start_old, connect_start_new, "logviewer connect start")

    onopen_old = '''    ws.onopen = () => {\n      setIsConnected(true)\n      setIsLoading(false)\n      Toast.success('日志连接已建立')\n    }\n'''
    onopen_new = '''    ws.onopen = () => {\n      if (wsRef.current !== ws) return\n      setIsConnected(true)\n      setIsLoading(false)\n      Toast.success('日志连接已建立')\n    }\n'''
    text = _replace_once(text, onopen_old, onopen_new, "logviewer onopen")

    onmessage_old = '''    ws.onmessage = (event) => {\n      setLogs(prev => [...prev, event.data])\n    }\n'''
    onmessage_new = '''    ws.onmessage = (event) => {\n      if (wsRef.current !== ws) return\n      setLogs(prev => [...prev, event.data])\n    }\n'''
    text = _replace_once(text, onmessage_old, onmessage_new, "logviewer onmessage")

    onerror_old = '''    ws.onerror = (error) => {\n      console.error('WebSocket错误:', error)\n\n      // 检查是否是连接建立前WebSocket已关闭的错误\n      // 这种情况通常发生在组件卸载或用户切换标签时\n      if (ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {\n        console.log('WebSocket在连接建立前已关闭')\n      } else {\n        // 其他错误仍然显示Toast提示\n        Toast.error('连接错误，请重试')\n      }\n\n      setIsLoading(false)\n    }\n'''
    onerror_new = '''    ws.onerror = (error) => {\n      if (wsRef.current !== ws) return\n      console.error('WebSocket错误:', error)\n      // onclose 负责统一调度自动重连，避免 error/close 双重重连。\n      setIsLoading(false)\n    }\n'''
    text = _replace_once(text, onerror_old, onerror_new, "logviewer onerror")

    onclose_old = '''    ws.onclose = () => {\n      setIsConnected(false)\n      console.log('WebSocket连接已关闭')\n    }\n'''
    onclose_new = '''    ws.onclose = () => {\n      // 已经被新连接替代的旧 socket 不得触发重连。\n      if (wsRef.current !== ws) return\n\n      setIsConnected(false)\n      setIsLoading(false)\n      console.log('WebSocket连接已关闭')\n\n      if (!allowReconnectRef.current) return\n      reconnectTimerRef.current = window.setTimeout(() => {\n        reconnectTimerRef.current = null\n        if (allowReconnectRef.current && wsRef.current === ws) {\n          console.log('重新连接实时日志 WebSocket')\n          connectWebSocket()\n        }\n      }, 3000)\n    }\n'''
    text = _replace_once(text, onclose_old, onclose_new, "logviewer onclose")

    effect_old = '''  useEffect(() => {\n    connectWebSocket()\n\n    return () => {\n      // 组件卸载时关闭WebSocket连接\n      if (wsRef.current) {\n        console.log('主动关闭WebSocket连接')\n        wsRef.current.close()\n      }\n    }\n    // eslint-disable-next-line react-hooks/exhaustive-deps\n  }, [activeTab])\n'''
    effect_new = '''  useEffect(() => {\n    allowReconnectRef.current = true\n    connectWebSocket()\n\n    return () => {\n      // 切换日志文件或组件卸载时，不应由旧连接触发自动重连。\n      allowReconnectRef.current = false\n      if (reconnectTimerRef.current !== null) {\n        window.clearTimeout(reconnectTimerRef.current)\n        reconnectTimerRef.current = null\n      }\n\n      const currentWs = wsRef.current\n      wsRef.current = null\n      if (currentWs) {\n        console.log('主动关闭WebSocket连接')\n        currentWs.close()\n      }\n    }\n    // eslint-disable-next-line react-hooks/exhaustive-deps\n  }, [activeTab])\n'''
    text = _replace_once(text, effect_old, effect_new, "logviewer effect cleanup")
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
