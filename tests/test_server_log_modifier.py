import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SERVER_RS = r'''use time::macros::format_description;
use tracing_appender::rolling::Rotation;
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;

#[tokio::main]
pub(crate) async fn _main(args: &[String]) -> AppResult<()> {
    let local_time = tracing_subscriber::fmt::time::LocalTime::new(format_description!(
        "[year]-[month]-[day] [hour]:[minute]:[second]"
    ));

    let console_layer = tracing_subscriber::fmt::layer()
        .with_target(false)
        .with_timer(local_time.clone());

    let file_appender = tracing_appender::rolling::RollingFileAppender::builder()
        .rotation(Rotation::DAILY)
        .rotation(Rotation::NEVER)
        .filename_prefix("biliup")
        .filename_prefix("download")
        .filename_suffix("log")
        .build("")
        .expect("initializing rolling file appender failed");
    let (non_blocking, _guard) = tracing_appender::non_blocking(file_appender);

    let file_layer = tracing_subscriber::fmt::layer()
        .with_writer(non_blocking)
        .with_timer(local_time)
        .with_ansi(false);

    let subscriber = tracing_subscriber::registry()
        .with(filter_layer)
        .with(console_layer)
        .with(file_layer);

    subscriber.init();
    run_server().await
}
'''


def make_upstream(root: Path) -> None:
    path = root / "crates/stream-gears/src/server.rs"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SERVER_RS, encoding="utf-8")


class ServerLogModifierTests(unittest.TestCase):
    def run_modifier(self, root: Path):
        return subprocess.run(
            [sys.executable, "scripts/restore_server_log.py", str(root)],
            text=True,
            capture_output=True,
        )

    def test_server_keeps_download_log_and_adds_ds_update_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            result = self.run_modifier(root)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            server = (root / "crates/stream-gears/src/server.rs").read_text(encoding="utf-8")
            self.assertIn("biliup-custom:server-log:v1", server)
            self.assertIn('rolling::never("", "ds_update.log")', server)
            self.assertIn(".with(web_log_layer)", server)
            self.assertIn('.filename_prefix("download")', server)

    def test_modifier_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            make_upstream(root)
            first = self.run_modifier(root)
            second = self.run_modifier(root)
            self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
            self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

            server = (root / "crates/stream-gears/src/server.rs").read_text(encoding="utf-8")
            self.assertEqual(server.count("biliup-custom:server-log:v1"), 1)


if __name__ == "__main__":
    unittest.main()
