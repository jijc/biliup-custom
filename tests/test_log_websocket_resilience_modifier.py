import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


WS_RS = r'''use axum::extract::ws::{Message, Utf8Bytes, WebSocket};
use std::time::Duration;
use tokio::time::{MissedTickBehavior, interval};
use tracing::{debug, error, info};

async fn websocket_logs(mut ws: WebSocket, query: LogsQuery) {
    let file_param = query.file.unwrap_or_else(|| "ds_update.log".to_string());
    let log_file = PathBuf::from(&file_param);
    let mut file_size = match send_last_lines(&mut ws, &log_file, 50).await {
        Ok(size) => size,
        Err(e) => {
            let _ = ws.send(Message::Close(None)).await;
            return;
        }
    };

    // 心跳/轮询间隔
    let mut tick = interval(Duration::from_millis(500));
    tick.set_missed_tick_behavior(MissedTickBehavior::Skip);

    // 主循环：同时处理客户端消息和文件更新
    loop {
        tokio::select! {
            maybe_msg = ws.recv() => {
                match maybe_msg {
                    Some(Ok(Message::Close(_))) => {
                        let _ = ws.send(Message::Close(None)).await;
                        break;
                    }
                    Some(Ok(Message::Ping(payload))) => {
                        // 回应 PONG
                        let _ = ws.send(Message::Pong(payload)).await;
                    }
                    Some(Ok(_)) => {
                    }
                    Some(Err(e)) => {
                        error!("WebSocket连接错误: {}", e);
                        break;
                    }
                    None => {
                        info!("WebSocket连接已关闭");
                        break;
                    }
                }
            }

            _ = tick.tick() => {
                let meta = match fs::metadata(&log_file).await {
                    Ok(m) => m,
                    Err(e) => {
                        error!("websocket_logs错误: {}", e);
                        break;
                    }
                };
                let current_size = meta.len();
                if current_size > file_size {
                    file_size = current_size;
                }
            }
        }
    }

    let _ = ws.send(Message::Close(None)).await;
    debug!("WebSocket日志会话结束: {}", file_param);
}
'''


LOGVIEWER_TSX = r'''export default function LogViewer() {
  const [logs, setLogs] = useState<string[]>([])
  const [isConnected, setIsConnected] = useState(false)
  const [isLoading, setIsLoading] = useState(true)
  const [activeTab, setActiveTab] = useState('ds_update')
  const wsRef = useRef<WebSocket | null>(null)
  const logContainerRef = useRef<HTMLDivElement>(null)

  const connectWebSocket = () => {
    setIsLoading(true)
    setLogs([])

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
      setIsConnected(true)
      setIsLoading(false)
      Toast.success('日志连接已建立')
    }

    ws.onmessage = (event) => {
      setLogs(prev => [...prev, event.data])
    }

    ws.onerror = (error) => {
      console.error('WebSocket错误:', error)
      if (ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) {
        console.log('WebSocket在连接建立前已关闭')
      } else {
        Toast.error('连接错误，请重试')
      }
      setIsLoading(false)
    }

    ws.onclose = () => {
      setIsConnected(false)
      console.log('WebSocket连接已关闭')
    }
  }

  useEffect(() => {
    connectWebSocket()

    return () => {
      if (wsRef.current) {
        console.log('主动关闭WebSocket连接')
        wsRef.current.close()
      }
    }
  }, [activeTab])
}
'''


def make_upstream(root: Path) -> None:
    files = {
        "crates/biliup-cli/src/server/api/ws.rs": WS_RS,
        "app/(app)/logviewer/page.tsx": LOGVIEWER_TSX,
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


class LogWebSocketResilienceModifierTests(unittest.TestCase):
    def run_modifier(self, root: Path):
        return subprocess.run(
            [sys.executable, "scripts/fix_log_websocket_resilience.py", str(root)],
            text=True,
            capture_output=True,
        )

    def test_adds_server_heartbeat_quiet_disconnect_and_client_reconnect(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            ws_rs = (root / "crates/biliup-cli/src/server/api/ws.rs").read_text(encoding="utf-8")
            page = (root / "app/(app)/logviewer/page.tsx").read_text(encoding="utf-8")

            self.assertIn("biliup-custom:log-websocket-resilience:v1", ws_rs)
            self.assertIn("Duration::from_secs(25)", ws_rs)
            self.assertIn("Message::Ping", ws_rs)
            self.assertIn("is_expected_log_ws_disconnect", ws_rs)
            self.assertIn("without closing handshake", ws_rs)
            self.assertIn('debug!("WebSocket日志连接已断开', ws_rs)

            self.assertIn("biliup-custom:log-websocket-resilience:v1", page)
            self.assertIn("reconnectTimerRef", page)
            self.assertIn("allowReconnectRef", page)
            self.assertIn("window.setTimeout", page)
            self.assertIn("3000", page)
            self.assertIn("wsRef.current !== ws", page)

    def test_modifier_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            first = self.run_modifier(root)
            second = self.run_modifier(root)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)


if __name__ == "__main__":
    unittest.main()
