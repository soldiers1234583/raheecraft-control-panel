"""
Tests for the Cloudflare connector helper (cloudflare_tunnel.py).

Everything here runs against *fake* cloudflared scripts and a local HTTP
server, so the tests never contact Cloudflare, never need a real tunnel token,
and never start a real connector. They cover the parts that decide what the
notebook reports: refusing without a token, never putting the token on the
command line, redacting it from logs, honest readiness/failure reporting, and
clean shutdown.
"""

import http.server
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from cloudflare_tunnel import (CloudflareTunnel, CloudflareError,
                               CONNECTOR_TOKEN_VAR, LAUNCHER_VAR, TOKEN_VAR)

FAKE_TOKEN = "eyJfake-token-value-that-must-never-be-printed-0123456789"


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib naming)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "healthy"}')

    def log_message(self, *args):
        pass


class FakeCloudflaredMixin:
    """Helpers for writing throwaway cloudflared stand-ins."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)
        # Keep downloads out of the repository/real HOME during tests.
        self._old_install_dir = os.environ.get("CLOUDFLARED_DIR")
        os.environ["CLOUDFLARED_DIR"] = str(self.tmpdir / "install")
        self._old_token = os.environ.pop(TOKEN_VAR, None)
        self._old_launcher = os.environ.pop(LAUNCHER_VAR, None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.pop(TOKEN_VAR, None)
        if self._old_token is not None:
            os.environ[TOKEN_VAR] = self._old_token
        os.environ.pop(LAUNCHER_VAR, None)
        if self._old_launcher is not None:
            os.environ[LAUNCHER_VAR] = self._old_launcher
        if self._old_install_dir is None:
            os.environ.pop("CLOUDFLARED_DIR", None)
        else:
            os.environ["CLOUDFLARED_DIR"] = self._old_install_dir

    def write_wrapper(self, argv_file: Path, name: str = "wrapper.sh") -> str:
        """A launcher stand-in: records its argv, then runs the cloudflared it
        was handed (the first executable file argument, i.e. the connector)."""
        return self.write_fake(
            name,
            'BIN=""\n'
            'for a in "$@"; do if [ -f "$a" ] && [ -x "$a" ]; then '
            'BIN="$a"; break; fi; done\n'
            f'printf "%s\\n" "$@" > "{argv_file}"\n'
            'exec "$BIN" tunnel --no-autoupdate run\n')

    def write_fake(self, name: str, body: str) -> str:
        path = self.tmpdir / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o700)
        return str(path)

    def make_tunnel(self, binary: str = None, token: str = FAKE_TOKEN,
                    port: int = 8080, **kwargs) -> CloudflareTunnel:
        return CloudflareTunnel(token=token, local_port=port,
                                binary_path=binary, **kwargs)


class TestTokenHandling(FakeCloudflaredMixin, unittest.TestCase):

    def test_without_a_token_start_refuses_and_downloads_nothing(self):
        tunnel = self.make_tunnel(token=None, binary=None)
        self.assertFalse(tunnel.has_token())
        self.assertFalse(tunnel.start())
        self.assertIsNone(tunnel.process)
        self.assertIn(TOKEN_VAR, tunnel.last_error)
        # No binary was fetched behind our back.
        self.assertEqual(list(Path(os.environ["CLOUDFLARED_DIR"]).glob("*")), [])

    def test_env_token_is_picked_up_and_never_put_on_argv(self):
        binary = self.write_fake("report.sh",
                                 'echo "argv:$@"\n'
                                 'if [ -n "$CLOUDFLARE_TUNNEL_TOKEN" ]; then '
                                 'echo "env-token:present"; else echo "env-token:absent"; fi\n'
                                 # cloudflared reads TUNNEL_TOKEN itself: without
                                 # it `tunnel run` refuses the command line.
                                 f'if [ -n "${CONNECTOR_TOKEN_VAR}" ]; then '
                                 'echo "connector-token:present"; else '
                                 'echo "connector-token:absent"; fi\n'
                                 'sleep 30\n')
        tunnel = self.make_tunnel(binary=binary, token=None)
        os.environ[TOKEN_VAR] = FAKE_TOKEN
        self.assertTrue(tunnel.has_token())
        self.assertTrue(tunnel.start())
        self.addCleanup(tunnel.stop)
        result = tunnel.wait_for_connection(timeout=5)
        output = "\n".join(tunnel.tail(20))
        self.assertIn("env-token:present", output)
        self.assertIn("connector-token:present", output)
        self.assertIn("tunnel --no-autoupdate run", output)
        self.assertNotIn(FAKE_TOKEN, output)
        self.assertFalse(result["connected"])  # silent connector: no fake success

    def test_token_is_redacted_from_buffer_and_log_file(self):
        binary = self.write_fake("echo-token.sh",
                                 f'echo "leaking $CLOUDFLARE_TUNNEL_TOKEN"\n'
                                 f'echo "INF Registered tunnel connection connIndex=0"\n'
                                 f'sleep 30\n')
        tunnel = self.make_tunnel(binary=binary)
        self.assertTrue(tunnel.start())
        self.addCleanup(tunnel.stop)
        self.assertTrue(tunnel.wait_for_connection(timeout=10)["connected"])
        buffer = "\n".join(tunnel.tail(50))
        self.assertNotIn(FAKE_TOKEN, buffer)
        self.assertIn("***", buffer)
        logged = Path(tunnel.log_path()).read_text()
        self.assertNotIn(FAKE_TOKEN, logged)
        self.assertIn("***", logged)


class TestReadiness(FakeCloudflaredMixin, unittest.TestCase):

    def test_ready_marker_is_reported_as_connected(self):
        binary = self.write_fake("ready.sh",
                                 'echo "INF Registered tunnel connection connIndex=0 '
                                 'connection=abc location=sin01 protocol=quic"\n'
                                 'sleep 30\n')
        tunnel = self.make_tunnel(binary=binary)
        self.assertTrue(tunnel.start())
        self.addCleanup(tunnel.stop)
        result = tunnel.wait_for_connection(timeout=10)
        self.assertTrue(result["connected"], result)
        self.assertEqual(result["connections"], 1)
        status = tunnel.status()
        self.assertTrue(status["running"])
        self.assertTrue(status["connected"])
        self.assertIsInstance(status["pid"], int)
        self.assertTrue(status["token_present"])

    def test_fatal_marker_is_reported_not_swallowed(self):
        binary = self.write_fake("bad-token.sh",
                                 'echo "ERR Invalid tunnel secret. Check the token."\n'
                                 'exit 1\n')
        tunnel = self.make_tunnel(binary=binary)
        self.assertTrue(tunnel.start())
        result = tunnel.wait_for_connection(timeout=15)
        self.assertFalse(result["connected"])
        self.assertIn("Invalid tunnel secret", result["reason"])
        status = tunnel.status()
        self.assertFalse(status["running"])
        self.assertFalse(status["connected"])
        self.assertEqual(status["exit_code"], 1)
        self.assertIn("Invalid tunnel secret", status["last_error"])

    def test_silent_connector_is_not_reported_as_connected(self):
        binary = self.write_fake("silent.sh", "sleep 30\n")
        tunnel = self.make_tunnel(binary=binary)
        self.assertTrue(tunnel.start())
        self.addCleanup(tunnel.stop)
        result = tunnel.wait_for_connection(timeout=3)
        self.assertFalse(result["connected"])
        self.assertIn("no connection registered", result["reason"])

    def test_missing_binary_is_reported_without_downloading(self):
        # dry_run refuses *before* any network access, so this asserts the
        # failure path without downloading the real connector.
        tunnel = self.make_tunnel(binary=str(self.tmpdir / "does-not-exist"),
                                  dry_run=True)
        self.assertIsNone(tunnel.find_binary())
        with self.assertRaises(CloudflareError) as ctx:
            tunnel.download_binary()
        self.assertIn("dry_run", str(ctx.exception))

    def test_dry_run_refuses_to_start(self):
        binary = self.write_fake("ready2.sh",
                                 'echo "INF Registered tunnel connection connIndex=0"\n'
                                 'sleep 30\n')
        tunnel = self.make_tunnel(binary=binary, dry_run=True)
        self.assertFalse(tunnel.start())
        self.assertIn("dry_run", tunnel.last_error)


class TestLocalServiceCheck(FakeCloudflaredMixin, unittest.TestCase):

    def test_verifies_the_thing_the_tunnel_points_at(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]

        tunnel = self.make_tunnel(port=port, token=None)
        self.assertTrue(tunnel.verify_local_service())
        self.assertFalse(self.make_tunnel(port=port + 1, token=None)
                         .verify_local_service(timeout=2))


class TestPublicHostnameCheck(FakeCloudflaredMixin, unittest.TestCase):
    """A connected connector is not the same as a working public URL."""

    def setUp(self):
        super().setUp()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.port = self.server.server_address[1]

    def test_reports_ok_when_the_public_hostname_answers(self):
        tunnel = self.make_tunnel(port=self.port, token=None)
        result = tunnel.verify_public_service(f"127.0.0.1:{self.port}",
                                              scheme="http", timeout=5)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["url"], f"http://127.0.0.1:{self.port}/health")
        self.assertEqual(result["status"], 200)

    def test_reports_failure_when_nothing_answers_there(self):
        tunnel = self.make_tunnel(port=self.port + 1, token=None)
        result = tunnel.verify_public_service(f"127.0.0.1:{self.port + 1}",
                                              scheme="http", timeout=5)
        self.assertFalse(result["ok"])
        self.assertIn("127.0.0.1", result["url"])
        self.assertTrue(result["detail"])

    def test_no_hostname_is_reported_rather_than_invented(self):
        # A blank hostname means "nothing configured": report it, and never
        # fall back to a hostname this module made up.
        tunnel = self.make_tunnel(token=None)
        tunnel.hostname = ""
        result = tunnel.verify_public_service(timeout=5)
        self.assertFalse(result["ok"])
        self.assertIsNone(result["url"])
        self.assertIn("no hostname", result["detail"])


class TestLauncherWrapper(FakeCloudflaredMixin, unittest.TestCase):
    """Hosts where a Go binary cannot read /etc/resolv.conf (Android/Termux).

    The connector is launched through a wrapper (``proot -b ...``), so these
    tests assert the wrapper is really used, that quoted wrapper arguments
    survive, and that the token still reaches the child through the
    environment instead of the command line.
    """

    def test_wrapper_is_used_and_token_stays_in_the_environment(self):
        argv_file = self.tmpdir / "argv.txt"
        wrapper = self.write_wrapper(argv_file)
        binary = self.write_fake(
            "connector.sh",
            'if [ -n "$CLOUDFLARE_TUNNEL_TOKEN" ]; then echo "env-token:present"; '
            'else echo "env-token:absent"; fi\n'
            f'if [ -n "${CONNECTOR_TOKEN_VAR}" ]; then echo "connector-token:present"; '
            'else echo "connector-token:absent"; fi\n'
            'echo "INF Registered tunnel connection connIndex=0"\n'
            'sleep 30\n')
        # The resolver file path contains a space on purpose: shlex must keep it
        # as one argument, or the wrapper would receive a broken bind.
        os.environ[LAUNCHER_VAR] = f'{wrapper} -b "/tmp/a b/resolv.conf:/etc/resolv.conf" -r /'

        tunnel = self.make_tunnel(binary=binary)
        self.assertTrue(tunnel.start(), tunnel.last_error)
        self.addCleanup(tunnel.stop)
        self.assertTrue(tunnel.wait_for_connection(timeout=10)["connected"])

        recorded = argv_file.read_text().splitlines()
        self.assertIn("/tmp/a b/resolv.conf:/etc/resolv.conf", recorded)
        self.assertIn("-b", recorded)
        self.assertIn(binary, recorded)
        self.assertIn("tunnel", recorded)
        self.assertIn("--no-autoupdate", recorded)
        self.assertIn("run", recorded)
        self.assertNotIn(FAKE_TOKEN, "\n".join(recorded))
        output = "\n".join(tunnel.tail(20))
        self.assertIn("env-token:present", output)
        self.assertIn("connector-token:present", output)
        self.assertIn(wrapper, tunnel.status()["launcher"])

    def test_malformed_launcher_refuses_to_start_without_running_anything(self):
        marker = self.tmpdir / "ran.txt"
        binary = self.write_fake("connector.sh", f'touch "{marker}"\nsleep 30\n')
        os.environ[LAUNCHER_VAR] = 'proot -b "unterminated'
        tunnel = self.make_tunnel(binary=binary)
        self.assertFalse(tunnel.start())
        self.assertIsNone(tunnel.process)
        self.assertIn(LAUNCHER_VAR, tunnel.last_error)
        self.assertFalse(marker.exists())


class TestShutdown(FakeCloudflaredMixin, unittest.TestCase):

    def test_stop_terminates_and_reaps_the_connector(self):
        binary = self.write_fake("long-lived.sh",
                                 'echo "INF Registered tunnel connection connIndex=0"\n'
                                 'sleep 300\n')
        tunnel = self.make_tunnel(binary=binary)
        self.assertTrue(tunnel.start())
        self.assertTrue(tunnel.wait_for_connection(timeout=10)["connected"])
        pid = tunnel.process.pid
        self.assertTrue(tunnel.stop())
        self.assertIsNone(tunnel.process)
        self.assertFalse(tunnel.status()["running"])
        self.assertFalse(tunnel.status()["connected"])
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_stop_without_a_process_is_a_no_op(self):
        tunnel = self.make_tunnel(binary=None, token=None)
        self.assertTrue(tunnel.stop())

    def test_stop_terminates_the_connector_started_through_a_wrapper(self):
        argv_file = self.tmpdir / "argv2.txt"
        wrapper = self.write_wrapper(argv_file, "wrapper2.sh")
        binary = self.write_fake("wrapped-connector.sh",
                                 'echo "INF Registered tunnel connection connIndex=0"\n'
                                 'exec sleep 300\n')
        os.environ[LAUNCHER_VAR] = wrapper
        tunnel = self.make_tunnel(binary=binary)
        self.assertTrue(tunnel.start(), tunnel.last_error)
        self.assertTrue(tunnel.wait_for_connection(timeout=10)["connected"])
        pid = tunnel.process.pid
        self.assertTrue(tunnel.stop())
        self.assertFalse(tunnel.status()["running"])
        self.assertFalse(tunnel.status()["connected"])
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
