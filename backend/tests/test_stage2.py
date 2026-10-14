#!/usr/bin/env python3
"""
RaheeCraft Stage 2 Tests
========================
Covers:
  - Action relay queueing (pending → running → terminal)
  - Duplicate/conflicting action rejection
  - Invalid action types and malformed payloads
  - Action timeouts
  - Endpoint authorization (authenticated vs not, admin vs user)
  - Console command allowlist
  - Log buffer streaming with reconnect-safe sequence numbers
  - Secret redaction in log lines
"""

import io
import json
import os
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

os.environ.setdefault("ADMIN_PASSWORD", "test-password-123")

from backend.command_handler import (
    command_handler, ActionState, ACTION_TIMEOUT_SECONDS
)
from backend.log_buffer import LogBuffer
from backend.auth import redact_text


# ---------------------------------------------------------------------------
# Minimal WSGI client (avoids adding a WebTest dependency)
# ---------------------------------------------------------------------------

def _call_app(path, method="GET", body=None, cookies=None, headers=None,
              query=""):
    """Call the Bottle app directly via WSGI. Returns (status, dict)."""
    from backend.app import app

    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "SERVER_NAME": "test",
        "SERVER_PORT": "80",
        "SERVER_PROTOCOL": "HTTP/1.1",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": "http",
        "wsgi.input": io.BytesIO(body if isinstance(body, bytes)
                                  else (body or "").encode()),
        "wsgi.errors": io.StringIO(),
        "wsgi.multithread": False,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        "CONTENT_LENGTH": str(len(body or "")),
    }
    if method in ("POST", "PUT") and "Content-Type" not in (headers or {}):
        environ["CONTENT_TYPE"] = "application/json"
    if cookies:
        environ["HTTP_COOKIE"] = "; ".join(
            f"{k}={v}" for k, v in cookies.items())
    for k, v in (headers or {}).items():
        environ["HTTP_" + k.upper().replace("-", "_")] = v

    captured = {}

    def start_response(status, response_headers, exc_info=None):
        captured["status"] = status
        captured["headers"] = dict(response_headers)

    chunks = app(environ, start_response)
    raw = b"".join(
        c if isinstance(c, bytes) else c.encode() for c in chunks)
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {"_raw": raw.decode(errors="replace")}
    code = int(captured.get("status", "500").split()[0])
    return code, parsed


def login_cookies():
    """Configure the admin hash (as main() does) and log in."""
    from backend.config import config as cfg
    from backend.auth import hash_secret
    # main() sets this at startup; replicate for tests (setattr: the class
    # attribute only exists once assigned)
    if not getattr(cfg, "ADMIN_PASSWORD_HASH", None):
        cfg.ADMIN_PASSWORD_HASH = hash_secret("test-password-123")
    code, resp = _call_app(
        "/api/auth/login", "POST",
        json.dumps({"username": "admin", "password": "test-password-123"}))
    assert code == 200, resp
    session = resp["session_id"]
    return {"raheecraft_session": session}


# ---------------------------------------------------------------------------
# Relay queue tests
# ---------------------------------------------------------------------------

class TestActionRelay(unittest.TestCase):
    """Queueing and lifecycle of agent-relayed actions."""

    def setUp(self):
        command_handler.actions.clear()

    def test_queue_valid_action(self):
        a = command_handler.queue_for_agent("start_server", {},
                                            agent_id="agent-abc")
        self.assertEqual(a["state"], "pending")
        self.assertEqual(a["action_type"], "start_server")
        self.assertEqual(a["agent_id"], "agent-abc")
        self.assertIn("action_id", a)

    def test_queue_rejects_unknown_type(self):
        with self.assertRaises(ValueError):
            command_handler.queue_for_agent("format_disk", {},
                                            agent_id="agent-abc")

    def test_duplicate_inflight_action_returns_existing(self):
        a1 = command_handler.queue_for_agent("start_server", {},
                                             agent_id="agent-abc")
        a2 = command_handler.queue_for_agent("start_server", {},
                                             agent_id="agent-abc")
        self.assertEqual(a1["action_id"], a2["action_id"])

    def test_pop_marks_running(self):
        command_handler.queue_for_agent("stop_server", {},
                                        agent_id="agent-abc")
        popped = command_handler.pop_for_agent("agent-abc")
        self.assertEqual(len(popped), 1)
        self.assertEqual(popped[0]["state"], "running")
        # Second pop returns nothing (no duplicates)
        self.assertEqual(command_handler.pop_for_agent("agent-abc"), [])

    def test_pop_only_delivers_to_addressed_agent(self):
        command_handler.queue_for_agent("start_server", {},
                                        agent_id="agent-one")
        self.assertEqual(command_handler.pop_for_agent("agent-two"), [])

    def test_complete_from_agent_success(self):
        a = command_handler.queue_for_agent("start_server", {},
                                            agent_id="agent-abc")
        command_handler.pop_for_agent("agent-abc")
        done = command_handler.complete_from_agent(
            a["action_id"], "success", result={"pid": 42})
        self.assertEqual(done["state"], "success")
        self.assertEqual(done["result"]["pid"], 42)

    def test_complete_is_idempotent(self):
        a = command_handler.queue_for_agent("stop_server", {},
                                            agent_id="agent-abc")
        command_handler.pop_for_agent("agent-abc")
        command_handler.complete_from_agent(a["action_id"], "success")
        # Replay with different state does not overwrite
        again = command_handler.complete_from_agent(
            a["action_id"], "failed", error="late report")
        self.assertEqual(again["state"], "success")

    def test_complete_rejects_invalid_state(self):
        a = command_handler.queue_for_agent("start_server", {},
                                            agent_id="agent-abc")
        with self.assertRaises(ValueError):
            command_handler.complete_from_agent(a["action_id"], "awesome")

    def test_complete_unknown_action_raises(self):
        with self.assertRaises(KeyError):
            command_handler.complete_from_agent("nope", "success")

    def test_timeout_sweep(self):
        a = command_handler.queue_for_agent("start_server", {},
                                            agent_id="agent-abc")
        command_handler.pop_for_agent("agent-abc")
        # Backdate the dispatch beyond the timeout
        command_handler.actions[a["action_id"]]["dispatched_at"] = (
            time.time() - ACTION_TIMEOUT_SECONDS - 5)
        n = command_handler.sweep_timeouts()
        self.assertEqual(n, 1)
        self.assertEqual(command_handler.get_action(a["action_id"])["state"],
                         "timeout")

    def test_pending_actions_do_not_time_out(self):
        a = command_handler.queue_for_agent("start_server", {},
                                            agent_id="agent-abc")
        command_handler.actions[a["action_id"]]["created_at"] = (
            time.time() - ACTION_TIMEOUT_SECONDS - 5)
        command_handler.sweep_timeouts()
        self.assertEqual(command_handler.get_action(a["action_id"])["state"],
                         "pending")

    def test_stop_when_already_stopped_is_success(self):
        """Agent-side semantic: stop with nothing running succeeds."""
        from agent.actions import ActionExecutor
        from unittest.mock import patch as p
        ex = ActionExecutor(server_dir="/nonexistent-server-dir")
        with p.object(ActionExecutor, "find_paper_pid", return_value=None):
            r = ex.execute({"action_type": "stop_server"})
        self.assertEqual(r["state"], "success")
        self.assertTrue(r["result"]["already_stopped"])

    def test_start_when_already_running_is_idempotent(self):
        """Agent-side: start with a live process returns success, no dup."""
        from agent.actions import ActionExecutor
        from unittest.mock import patch as p
        ex = ActionExecutor(server_dir="/nonexistent-server-dir")
        with p.object(ActionExecutor, "find_paper_pid", return_value=999):
            r = ex.execute({"action_type": "start_server"})
        self.assertEqual(r["state"], "success")
        self.assertTrue(r["result"]["already_running"])

    def test_restart_stops_then_starts(self):
        """Agent-side restart sequence: stop phase verified before start."""
        from agent.actions import ActionExecutor
        from unittest.mock import patch as p
        ex = ActionExecutor(server_dir="/nonexistent-server-dir")
        # Nothing running → stop phase idempotent-succeeds, start fails
        # because paper.jar is missing: sequence still ordered correctly.
        with p.object(ActionExecutor, "find_paper_pid", return_value=None):
            r = ex.execute({"action_type": "restart_server"})
        self.assertEqual(r["state"], "failed")
        self.assertIn("paper.jar", r["error"])
        # stop sub-result proves the stop phase ran first
        self.assertIn("stop", r["result"])
        self.assertTrue(r["result"]["stop"]["already_stopped"])


# ---------------------------------------------------------------------------
# Endpoint authorization tests
# ---------------------------------------------------------------------------

class TestEndpointAuth(unittest.TestCase):
    """Power/console endpoints require an authenticated admin session."""

    @classmethod
    def setUpClass(cls):
        cls.cookies = login_cookies()

    def test_status_requires_auth(self):
        code, resp = _call_app("/api/status")
        self.assertEqual(code, 401)

    def test_power_start_requires_auth(self):
        code, resp = _call_app("/api/admin/server/start", "POST", "{}")
        self.assertEqual(code, 401)

    def test_power_start_with_session(self):
        """Admin session + no agent online → 503, never fake success."""
        code, resp = _call_app("/api/admin/server/start", "POST", "{}",
                               cookies=self.cookies)
        self.assertEqual(code, 503)
        self.assertEqual(resp.get("state"), "agent_offline")

    def test_power_stop_requires_auth(self):
        code, _ = _call_app("/api/admin/server/stop", "POST", "{}")
        self.assertEqual(code, 401)

    def test_power_restart_requires_auth(self):
        code, _ = _call_app("/api/admin/server/restart", "POST", "{}")
        self.assertEqual(code, 401)

    def test_console_requires_auth(self):
        code, _ = _call_app("/api/admin/server/console", "POST",
                            json.dumps({"command": "list"}))
        self.assertEqual(code, 401)

    def test_console_rejects_disallowed_command(self):
        code, resp = _call_app(
            "/api/admin/server/console", "POST",
            json.dumps({"command": "rm -rf /"}),
            cookies=self.cookies)
        # Either 400 (allowlist) or 503 (no agent) — never queued
        self.assertIn(code, (400, 503))
        if code == 400:
            self.assertIn("not allowed", resp.get("error", ""))

    def test_console_rejects_empty_command(self):
        code, resp = _call_app(
            "/api/admin/server/console", "POST",
            json.dumps({"command": ""}),
            cookies=self.cookies)
        self.assertEqual(code, 400)

    def test_agent_poll_requires_agent_auth(self):
        code, _ = _call_app("/api/agent/poll", "POST", "{}")
        self.assertEqual(code, 401)

    def test_agent_log_push_requires_agent_auth(self):
        code, _ = _call_app("/api/agent/logs", "POST",
                            json.dumps({"lines": ["hello"]}))
        self.assertEqual(code, 401)

    def test_monitoring_requires_auth(self):
        code, _ = _call_app("/api/admin/monitoring")
        self.assertEqual(code, 401)

    def test_console_tail_requires_auth(self):
        code, _ = _call_app("/api/admin/console/tail")
        self.assertEqual(code, 401)

    def test_monitoring_with_session_returns_real_shape(self):
        code, resp = _call_app("/api/admin/monitoring",
                               cookies=self.cookies)
        self.assertEqual(code, 200)
        for key in ("process", "players", "cpu", "memory", "agent",
                    "log_activity", "playit_detail", "backup"):
            self.assertIn(key, resp)
        # No agent registered → measured must be False, not invented
        self.assertFalse(resp["process"]["measured"])


# ---------------------------------------------------------------------------
# Log buffer tests
# ---------------------------------------------------------------------------

class TestLogBuffer(unittest.TestCase):
    """Sequence-based streaming with reconnect safety."""

    def setUp(self):
        self.buf = LogBuffer(max_lines=50)

    def test_sequences_increment(self):
        r1 = self.buf.append("line one")
        r2 = self.buf.append("line two")
        self.assertEqual(r2["seq"], r1["seq"] + 1)

    def test_tail_since_returns_only_new(self):
        for i in range(10):
            self.buf.append(f"line {i}")
        first = self.buf.tail(since=0, limit=100)
        self.assertEqual(len(first["lines"]), 10)
        seq = first["seq"]
        # No new lines → empty (reconnect with no dupes)
        again = self.buf.tail(since=seq, limit=100)
        self.assertEqual(again["lines"], [])
        self.buf.append("new line")
        delta = self.buf.tail(since=seq, limit=100)
        self.assertEqual(len(delta["lines"]), 1)
        self.assertEqual(delta["lines"][0]["text"], "new line")

    def test_level_filter(self):
        self.buf.append("all good")
        self.buf.append("[Server thread/ERROR]: boom")
        self.buf.append("[Server thread/WARN]: careful")
        errors = self.buf.tail(since=0, level="error")
        self.assertEqual(len(errors["lines"]), 1)
        warns = self.buf.tail(since=0, level="warn")
        self.assertEqual(len(warns["lines"]), 1)

    def test_search_filter(self):
        self.buf.append("Steve joined the game")
        self.buf.append("Notch left the game")
        found = self.buf.tail(since=0, search="steve")
        self.assertEqual(len(found["lines"]), 1)

    def test_ring_buffer_caps(self):
        for i in range(200):
            self.buf.append(f"line {i}")
        tail = self.buf.tail(since=0, limit=500)
        self.assertLessEqual(len(tail["lines"]), 50)

    def test_redaction_on_store(self):
        self.buf.append("token=abc123def456secret789 connecting")
        stored = self.buf.tail(since=0)["lines"][0]["text"]
        self.assertNotIn("abc123def456secret789", stored)
        self.assertIn("***", stored)

    def test_long_line_truncated(self):
        self.buf.append("x" * 2000)
        stored = self.buf.tail(since=0)["lines"][0]["text"]
        self.assertLessEqual(len(stored), 500)

    def test_extend_accepts_dicts_and_strings(self):
        n = self.buf.extend([
            "plain line",
            {"text": "dict line", "level": "warn"},
        ])
        self.assertEqual(n, 2)


class TestRedactText(unittest.TestCase):
    """Pattern-based redaction for free-form log text."""

    def test_redacts_password_assignment(self):
        out = redact_text("login password=hunter22 ok")
        self.assertNotIn("hunter22", out)

    def test_redacts_kaggle_token(self):
        out = redact_text("auth KGAT_533e3a3edd4ecdb4366439b479b8d374 done")
        self.assertNotIn("533e3a3edd4ecdb4366439b479b8d374", out)

    def test_redacts_long_hex(self):
        out = redact_text("key a8ee6c52b0aae85c1e5c2e1a3d4d5e6f7a8b9c0d")
        self.assertNotIn("a8ee6c52b0aae85c1e5c2e1a3d4d5e6f7a8b9c0d", out)

    def test_redacts_bearer(self):
        out = redact_text("header Bearer abc.def-123 sent")
        self.assertNotIn("abc.def-123", out)

    def test_keeps_normal_log_lines(self):
        line = "Steve joined the game"
        self.assertEqual(redact_text(line), line)


if __name__ == "__main__":
    unittest.main(verbosity=2)
