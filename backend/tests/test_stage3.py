#!/usr/bin/env python3
"""
RaheeCraft Stage 3 Tests
========================
Player management, moderation, whitelist, and operator tests.

Covers (per acceptance list):
  - Online-player list: zero / one / multiple players
  - Server offline and agent disconnected
  - Whitelist enable/disable/add/remove
  - Invalid and duplicate usernames
  - Kick, ban, unban and failed actions
  - Unauthorized privilege changes
  - Operator grant/revoke + last-op safeguard
  - LuckPerms present and absent
  - Malformed input and injection attempts
  - Repeated requests (idempotency / conflict guard)
  - Audit-log redaction

All server responses are mocked or fixture-driven; no live server needed.
"""

import io
import json
import os
import re
import sys
import time
import unittest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

os.environ.setdefault("ADMIN_PASSWORD", "test-password-123")

from backend.validation import (
    valid_username, valid_uuid, sanitize_reason, validate_player_request,
)
from backend.agent_handler import agent_handler, _sanitize_snapshot
from backend.command_handler import command_handler
from agent.player_tracker import (
    PlayerTracker, valid_username as agent_valid_username,
    sanitize_reason as agent_sanitize_reason, JOIN_RE, LEAVE_RE, LIST_RE,
)
from agent.actions import (
    ActionExecutor, ALLOWED_ACTION_TYPES, PLAYER_ACTION_COMMANDS,
)


# ---------------------------------------------------------------------------
# WSGI test client (same minimal client as test_stage2)
# ---------------------------------------------------------------------------

def _call_app(path, method="GET", body=None, cookies=None, headers=None,
              query=""):
    from backend.app import app

    # Support "?a=b" embedded in the path (bottle routes on PATH_INFO)
    if "?" in path and not query:
        path, _, query = path.partition("?")

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
    if method in ("POST", "PUT", "DELETE") and \
            "Content-Type" not in (headers or {}):
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
    raw = b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks)
    try:
        parsed = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        parsed = {"_raw": raw.decode(errors="replace")}
    code = int(captured.get("status", "500").split()[0])
    return code, parsed


_LOGIN_CACHE = {}


def login_cookies():
    """Log in as admin, caching the session for the test process.

    Each test only needs some valid admin session; logging in per test would
    trip the login endpoint's rate limit (test-harness artifact, not a
    product behaviour).
    """
    from backend.config import config as cfg
    from backend.auth import hash_secret
    if not getattr(cfg, "ADMIN_PASSWORD_HASH", None):
        cfg.ADMIN_PASSWORD_HASH = hash_secret("test-password-123")
    cached = _LOGIN_CACHE.get("cookies")
    if cached and cfg.validate_session(cached["raheecraft_session"]):
        return dict(cached)
    code, resp = _call_app(
        "/api/auth/login", "POST",
        json.dumps({"username": "admin", "password": "test-password-123"}))
    assert code == 200, resp
    _LOGIN_CACHE["cookies"] = {"raheecraft_session": resp["session_id"]}
    return {"raheecraft_session": resp["session_id"]}


def register_online_agent(agent_id="agent-s3-01", secret="s3-secret-0123456789abcdef"):
    """Register + heartbeat an agent so it appears online."""
    agent_handler.register_agent(agent_id, secret)
    agent_handler.heartbeat(agent_id, secret, {"paper_running": True})
    return agent_id, secret


def inject_snapshot(snapshot, agent_id="agent-s3-01"):
    """Store a player snapshot as if the agent heartbeat carried it."""
    agent_handler.agents[agent_id]["player_snapshot"] = \
        _sanitize_snapshot(snapshot)
    agent_handler.agents[agent_id]["player_snapshot_ts"] = snapshot.get("ts")


# ---------------------------------------------------------------------------
# Validation unit tests
# ---------------------------------------------------------------------------

class TestValidation(unittest.TestCase):
    """Username / UUID / reason validation, both sides."""

    def test_valid_usernames(self):
        for name in ("Steve", "abc", "Player_123", "A" * 16,
                     "herobrine_99"):
            self.assertTrue(valid_username(name), name)
            self.assertTrue(agent_valid_username(name), name)

    def test_invalid_usernames(self):
        for name in ("ab", "A" * 17, "has space", "semi;colon", "back\\slash",
                     "pipe|char", "dollar$ign", "new\nline", "tab\tchar",
                     "quote'", 'dquote"', "<script>", "", None, 123,
                 "../../etc/passwd", "cmd && rm -rf /"):
            self.assertFalse(valid_username(name), repr(name))
            self.assertFalse(agent_valid_username(name), repr(name))

    def test_valid_uuids(self):
        self.assertTrue(valid_uuid("069a79f4-44e9-4726-a5be-fca90e38aaf5"))
        self.assertTrue(valid_uuid("069A79F4-44E9-4726-A5BE-FCA90E38AAF5"))

    def test_invalid_uuids(self):
        for u in ("not-a-uuid", "069a79f4", "", None, 42,
                  "069a79f4-44e9-4726-a5be-fca90e38aaf5-extra"):
            self.assertFalse(valid_uuid(u), repr(u))

    def test_reason_strips_control_chars(self):
        self.assertEqual(sanitize_reason("bad\nreason"), "badreason")
        self.assertEqual(sanitize_reason("bad\r\nreason"), "badreason")
        # Newline cannot inject a second console line
        self.assertNotIn("\n", sanitize_reason("line1\nop Notch"))

    def test_reason_length_cap(self):
        self.assertLessEqual(len(sanitize_reason("x" * 1000)), 100)

    def test_reason_non_string(self):
        self.assertEqual(sanitize_reason(None), "")
        self.assertEqual(sanitize_reason(42), "")
        self.assertEqual(sanitize_reason({"a": 1}), "")

    def test_agent_reason_sanitizer_matches(self):
        self.assertEqual(agent_sanitize_reason("a\nb"), sanitize_reason("a\nb"))
        self.assertLessEqual(len(agent_sanitize_reason("y" * 500)), 100)

    def test_validate_player_request_ok(self):
        u, r, err = validate_player_request(
            {"username": "Steve", "reason": "spam"})
        self.assertIsNone(err)
        self.assertEqual((u, r), ("Steve", "spam"))

    def test_validate_player_request_requires_reason(self):
        u, r, err = validate_player_request({"username": "Steve"},
                                            require_reason=True)
        self.assertIsNotNone(err)

    def test_validate_player_request_invalid_body(self):
        for body in (None, [], "string", 42, {}):
            u, r, err = validate_player_request(body)
            self.assertIsNotNone(err, repr(body))

    def test_validate_player_request_injection_username(self):
        u, r, err = validate_player_request(
            {"username": "Steve; op Notch", "reason": "x"})
        self.assertIsNotNone(err)

    def test_validate_rejects_oversized_raw_reason(self):
        u, r, err = validate_player_request(
            {"username": "Steve", "reason": "x" * 501})
        self.assertIsNotNone(err)


# ---------------------------------------------------------------------------
# Player tracker (agent side, fixture-driven)
# ---------------------------------------------------------------------------

class TrackerFixture(unittest.TestCase):
    """Creates a temp server dir with Paper's JSON files."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="rc-s3-")
        self.tracker = PlayerTracker(server_dir=self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, name, data):
        with open(os.path.join(self.dir, name), "w") as f:
            json.dump(data, f)

    def write_props(self, content):
        with open(os.path.join(self.dir, "server.properties"), "w") as f:
            f.write(content)


class TestPlayerTracker(TrackerFixture):

    def test_zero_players(self):
        self.assertEqual(self.tracker.get_online(), [])

    def test_one_player_join_and_leave(self):
        self.tracker.observe_line(
            "[12:00:00] [Server thread/INFO]: Steve joined the game")
        online = self.tracker.get_online()
        self.assertEqual(len(online), 1)
        self.assertEqual(online[0]["username"], "Steve")
        self.assertIsNotNone(online[0]["session_started_at"])
        self.assertIsNotNone(online[0]["session_seconds"])

        self.tracker.observe_line(
            "[12:01:00] [Server thread/INFO]: Steve left the game")
        self.assertEqual(self.tracker.get_online(), [])

    def test_multiple_players(self):
        for name in ("Steve", "Alex", "Notch"):
            self.tracker.observe_line(
                f"[12:00:00] [Server thread/INFO]: {name} joined the game")
        names = [p["username"] for p in self.tracker.get_online()]
        self.assertEqual(sorted(names), ["Alex", "Notch", "Steve"])

    def test_uuid_resolved_from_usercache(self):
        self.write("usercache.json", [
            {"name": "Steve",
             "uuid": "069a79f4-44e9-4726-a5be-fca90e38aaf5"}])
        self.tracker.observe_line(
            "[12:00:00] [Server thread/INFO]: Steve joined the game")
        online = self.tracker.get_online()
        self.assertEqual(online[0]["uuid"],
                         "069a79f4-44e9-4726-a5be-fca90e38aaf5")

    def test_list_output_reconciles(self):
        # Player observed mid-session via `list` output: session unknown
        self.write("usercache.json", [
            {"name": "Alex",
             "uuid": "8667ba71-b85a-4004-af54-457a9734eed7"}])
        self.tracker.observe_line(
            "[12:00:00] [Server thread/INFO]: There are 2 of a max of 20 "
            "players online: Alex, Steve")
        names = sorted(p["username"] for p in self.tracker.get_online())
        self.assertEqual(names, ["Alex", "Steve"])
        by_name = {p["username"]: p for p in self.tracker.get_online()}
        self.assertIsNone(by_name["Steve"]["session_seconds"])  # unknown

    def test_whitelist_read(self):
        self.write_props("white-list=true\nmax-players=20\n")
        self.write("whitelist.json", [
            {"uuid": "069a79f4-44e9-4726-a5be-fca90e38aaf5", "name": "Steve"},
            {"uuid": "not-a-uuid", "name": "Bad;Name"},   # filtered
        ])
        wl = self.tracker.get_whitelist()
        self.assertTrue(wl["enabled"])
        self.assertEqual(len(wl["entries"]), 1)
        self.assertEqual(wl["entries"][0]["username"], "Steve")

    def test_whitelist_disabled(self):
        self.write_props("white-list=false\n")
        self.assertFalse(self.tracker.get_whitelist()["enabled"])

    def test_whitelist_enabled_unknown_when_no_props(self):
        self.assertIsNone(self.tracker.get_whitelist()["enabled"])

    def test_bans_read_and_sanitized(self):
        self.write("banned-players.json", [
            {"uuid": "069a79f4-44e9-4726-a5be-fca90e38aaf5",
             "name": "Griefer", "created": "2026-01-01 00:00:00 +0000",
             "source": "Bukkit", "reason": "griefing\nop Griefer",
             "expires": "forever", "lastModified": "..."},
        ])
        bans = self.tracker.get_bans()
        self.assertEqual(len(bans), 1)
        self.assertEqual(bans[0]["username"], "Griefer")
        self.assertNotIn("\n", bans[0]["reason"])

    def test_ops_read(self):
        self.write("ops.json", [
            {"uuid": "069a79f4-44e9-4726-a5be-fca90e38aaf5",
             "name": "Admin", "level": 4, "bypassesPlayerLimit": False},
        ])
        ops = self.tracker.get_ops()
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0]["username"], "Admin")
        self.assertEqual(ops[0]["level"], 4)

    def test_malformed_json_does_not_crash(self):
        with open(os.path.join(self.dir, "whitelist.json"), "w") as f:
            f.write("{not json")
        with open(os.path.join(self.dir, "banned-players.json"), "w") as f:
            f.write("")
        self.assertEqual(self.tracker.get_whitelist()["entries"], [])
        self.assertEqual(self.tracker.get_bans(), [])

    def test_luckperms_absent(self):
        lp = self.tracker.detect_luckperms()
        self.assertFalse(lp["installed"])
        self.assertFalse(lp["operational"])

    def test_luckperms_present(self):
        plugins = os.path.join(self.dir, "plugins")
        os.makedirs(plugins)
        with open(os.path.join(plugins, "LuckPerms-Bukkit-5.4.102.jar"),
                  "w") as f:
            f.write("fake")
        lp = self.tracker.detect_luckperms()
        self.assertTrue(lp["installed"])
        self.assertEqual(lp["version"], "5.4.102")

    def test_luckperms_operational(self):
        plugins = os.path.join(self.dir, "plugins")
        os.makedirs(os.path.join(plugins, "LuckPerms"))
        with open(os.path.join(plugins, "LuckPerms-Bukkit-5.4.jar"), "w") as f:
            f.write("fake")
        self.assertTrue(self.tracker.detect_luckperms()["operational"])

    def test_snapshot_shape(self):
        snap = self.tracker.snapshot()
        for key in ("online", "whitelist", "bans", "ops", "luckperms", "ts"):
            self.assertIn(key, snap)

    def test_join_regex_rejects_injection(self):
        # A crafted name in a log line must not match
        self.assertIsNone(JOIN_RE.match(
            "[x]: Steve; rm -rf / joined the game"))


# ---------------------------------------------------------------------------
# Agent executor: player actions against a fixture tracker
# ---------------------------------------------------------------------------

class TestAgentPlayerActions(TrackerFixture):

    def setUp(self):
        super().setUp()
        self.executor = ActionExecutor(server_dir=self.dir)
        # Pretend the server is running and the console channel works
        self._patches = [
            patch.object(ActionExecutor, "find_paper_pid", return_value=1234),
            patch.object(ActionExecutor, "_write_console", return_value=True),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        # Speed up the post-mutation settle sleep
        self._sleep = patch("time.sleep", lambda *a: None)
        self._sleep.start()
        self.addCleanup(self._sleep.stop)
        # Point the module-level tracker at our fixture
        import agent.actions as actions_mod
        import agent.player_tracker as pt_mod
        self._orig_tracker = pt_mod.player_tracker
        pt_mod.player_tracker = self.tracker
        self.addCleanup(setattr, pt_mod, "player_tracker", self._orig_tracker)

    def exec_action(self, atype, params):
        return self.executor.execute({"action_type": atype, "params": params})

    def test_invalid_username_rejected(self):
        r = self.exec_action("ban_player",
                             {"username": "bad;name", "reason": "x"})
        self.assertEqual(r["state"], "failed")
        self.assertIn("Invalid username", r["error"])

    def test_kick_not_online_fails(self):
        r = self.exec_action("kick_player", {"username": "Steve"})
        self.assertEqual(r["state"], "failed")
        self.assertIn("not online", r["error"])

    def test_kick_online_confirmed(self):
        self.tracker.observe_line("[00:00:00] x: Steve joined the game")
        r = self.exec_action("kick_player",
                             {"username": "Steve", "reason": "afk"})
        self.assertEqual(r["state"], "failed")  # still online: no kick happened
        # Now simulate the kick taking effect (leave line observed)
        self.tracker.observe_line("[00:00:01] x: Steve left the game")
        r = self.exec_action("kick_player", {"username": "Steve"})
        self.assertEqual(r["state"], "failed")  # precondition: not online

    def test_ban_confirmation(self):
        # Console accepts the ban; Paper writes the file
        def fake_console(cmd):
            if cmd.startswith("ban Steve"):
                self.write("banned-players.json", [
                    {"uuid": "u", "name": "Steve", "reason": "test",
                     "created": "now", "expires": "forever",
                     "source": "Server"}])
            return True
        with patch.object(ActionExecutor, "_write_console",
                          side_effect=fake_console):
            r = self.exec_action("ban_player",
                                 {"username": "Steve", "reason": "test"})
        self.assertEqual(r["state"], "success")
        self.assertTrue(r["result"].get("confirmed"))

    def test_ban_unconfirmed_fails(self):
        # Console accepts but Paper never records the ban
        r = self.exec_action("ban_player", {"username": "Steve"})
        self.assertEqual(r["state"], "failed")
        self.assertIn("banned-players.json", r["error"])

    def test_ban_already_banned_idempotent(self):
        self.write("banned-players.json", [
            {"uuid": "u", "name": "Steve", "reason": "x"}])
        r = self.exec_action("ban_player", {"username": "Steve"})
        self.assertEqual(r["state"], "success")
        self.assertTrue(r["result"].get("already"))

    def test_unban_not_banned_idempotent(self):
        r = self.exec_action("unban_player", {"username": "Steve"})
        self.assertEqual(r["state"], "success")
        self.assertTrue(r["result"].get("already"))

    def test_unban_confirmation(self):
        self.write("banned-players.json", [{"name": "Steve", "uuid": "u"}])
        def fake_console(cmd):
            if cmd == "pardon Steve":
                self.write("banned-players.json", [])
            return True
        with patch.object(ActionExecutor, "_write_console",
                          side_effect=fake_console):
            r = self.exec_action("unban_player", {"username": "Steve"})
        self.assertEqual(r["state"], "success")

    def test_whitelist_add_confirmation(self):
        def fake_console(cmd):
            if cmd == "whitelist add Steve":
                self.write("whitelist.json",
                           [{"name": "Steve", "uuid": "u"}])
            return True
        with patch.object(ActionExecutor, "_write_console",
                          side_effect=fake_console):
            r = self.exec_action("whitelist_add", {"username": "Steve"})
        self.assertEqual(r["state"], "success")
        self.assertTrue(r["result"].get("confirmed"))

    def test_whitelist_add_unconfirmed_fails(self):
        r = self.exec_action("whitelist_add", {"username": "Steve"})
        self.assertEqual(r["state"], "failed")

    def test_whitelist_add_duplicate_idempotent(self):
        self.write("whitelist.json", [{"name": "Steve", "uuid": "u"}])
        r = self.exec_action("whitelist_add", {"username": "Steve"})
        self.assertEqual(r["state"], "success")
        self.assertTrue(r["result"].get("already"))

    def test_whitelist_enable_confirmation(self):
        self.write_props("white-list=false\n")
        def fake_console(cmd):
            if cmd == "whitelist on":
                self.write_props("white-list=true\n")
            return True
        with patch.object(ActionExecutor, "_write_console",
                          side_effect=fake_console):
            r = self.exec_action("whitelist_enable", {})
        self.assertEqual(r["state"], "success")
        self.assertTrue(r["result"].get("confirmed"))

    def test_whitelist_disable_confirmation(self):
        self.write_props("white-list=true\n")
        def fake_console(cmd):
            if cmd == "whitelist off":
                self.write_props("white-list=false\n")
            return True
        with patch.object(ActionExecutor, "_write_console",
                          side_effect=fake_console):
            r = self.exec_action("whitelist_disable", {})
        self.assertEqual(r["state"], "success")

    def test_op_grant_confirmation(self):
        def fake_console(cmd):
            if cmd == "op Steve":
                self.write("ops.json", [{"name": "Steve", "uuid": "u",
                                         "level": 4}])
            return True
        with patch.object(ActionExecutor, "_write_console",
                          side_effect=fake_console):
            r = self.exec_action("op_grant", {"username": "Steve"})
        self.assertEqual(r["state"], "success")

    def test_op_revoke_confirmation(self):
        self.write("ops.json", [
            {"name": "Admin", "uuid": "u1", "level": 4},
            {"name": "Steve", "uuid": "u2", "level": 4}])
        def fake_console(cmd):
            if cmd == "deop Steve":
                self.write("ops.json", [{"name": "Admin", "uuid": "u1",
                                         "level": 4}])
            return True
        with patch.object(ActionExecutor, "_write_console",
                          side_effect=fake_console):
            r = self.exec_action("op_revoke", {"username": "Steve"})
        self.assertEqual(r["state"], "success")

    def test_op_revoke_last_op_blocked(self):
        self.write("ops.json", [{"name": "Admin", "uuid": "u1", "level": 4}])
        r = self.exec_action("op_revoke", {"username": "Admin"})
        self.assertEqual(r["state"], "failed")
        self.assertIn("last operator", r["error"])

    def test_command_never_contains_newline(self):
        # Reason with newline is sanitized before command construction
        captured = []
        def fake_console(cmd):
            captured.append(cmd)
            return False  # block actual send; we only inspect the command
        with patch.object(ActionExecutor, "_write_console",
                          side_effect=fake_console):
            self.tracker.observe_line("[00:00:00] x: Steve joined the game")
            self.exec_action("kick_player",
                             {"username": "Steve", "reason": "bad\nreason"})
        if captured:
            self.assertNotIn("\n", captured[0])
            # No second command smuggled in
            self.assertEqual(captured[0].count("kick"), 1)

    def test_injection_username_rejected_everywhere(self):
        for atype in PLAYER_ACTION_COMMANDS:
            r = self.exec_action(atype, {"username": "a; op Notch"})
            if atype in ("whitelist_enable", "whitelist_disable"):
                continue  # these don't take usernames
            self.assertEqual(r["state"], "failed", atype)
            self.assertIn("Invalid username", r["error"], atype)

    def test_unknown_action_type_rejected(self):
        r = self.executor.execute({"action_type": "format_disk", "params": {}})
        self.assertEqual(r["state"], "failed")
        self.assertIn("Unknown action type", r["error"])

    def test_all_stage3_types_allowed(self):
        for t in ("kick_player", "ban_player", "unban_player",
                  "whitelist_add", "whitelist_remove", "whitelist_enable",
                  "whitelist_disable", "op_grant", "op_revoke"):
            self.assertIn(t, ALLOWED_ACTION_TYPES)


# ---------------------------------------------------------------------------
# Backend endpoints
# ---------------------------------------------------------------------------

class Stage3EndpointBase(unittest.TestCase):
    """Fresh online agent + admin session per test class."""

    @classmethod
    def setUpClass(cls):
        cls.cookies = login_cookies()
        cls.agent_id, cls.agent_secret = register_online_agent()

    def setUp(self):
        command_handler.actions.clear()


class TestPlayerEndpoints(Stage3EndpointBase):

    def test_players_requires_auth(self):
        code, _ = _call_app("/api/admin/players")
        self.assertEqual(code, 401)

    def test_players_zero(self):
        inject_snapshot({"online": [], "ts": time.time()})
        code, resp = _call_app("/api/admin/players", cookies=self.cookies)
        self.assertEqual(code, 200)
        self.assertTrue(resp["available"])
        self.assertEqual(resp["online"], [])
        self.assertEqual(resp["count"], 0)

    def test_players_multiple(self):
        inject_snapshot({"online": [
            {"username": "Steve", "uuid": "u1", "session_seconds": 10},
            {"username": "Alex", "uuid": None, "session_seconds": None},
        ], "ts": time.time()})
        code, resp = _call_app("/api/admin/players", cookies=self.cookies)
        self.assertEqual(resp["count"], 2)

    def test_players_agent_offline(self):
        agent_handler.agents[self.agent_id]["last_heartbeat"] = \
            time.time() - 600
        try:
            code, resp = _call_app("/api/admin/players", cookies=self.cookies)
            self.assertEqual(code, 200)
            self.assertFalse(resp["available"])
            self.assertEqual(resp["reason"], "agent_offline")
        finally:
            agent_handler.heartbeat(self.agent_id, self.agent_secret, {})

    def test_players_no_snapshot_yet(self):
        agent_handler.agents[self.agent_id]["player_snapshot"] = None
        code, resp = _call_app("/api/admin/players", cookies=self.cookies)
        self.assertFalse(resp["available"])
        self.assertEqual(resp["reason"], "no_snapshot_yet")

    def test_bans_endpoint(self):
        inject_snapshot({"bans": [
            {"username": "Griefer", "uuid": "u", "reason": "grief"}],
            "ts": time.time()})
        code, resp = _call_app("/api/admin/players/bans",
                               cookies=self.cookies)
        self.assertTrue(resp["available"])
        self.assertEqual(resp["bans"][0]["username"], "Griefer")

    def test_whitelist_endpoint(self):
        inject_snapshot({"whitelist": {"enabled": True, "entries": [
            {"username": "Steve", "uuid": "u"}]}, "ts": time.time()})
        code, resp = _call_app("/api/admin/whitelist", cookies=self.cookies)
        self.assertTrue(resp["available"])
        self.assertTrue(resp["enabled"])
        self.assertEqual(len(resp["entries"]), 1)

    def test_operators_endpoint(self):
        inject_snapshot({"ops": [{"username": "Admin", "uuid": "u",
                                  "level": 4}],
                         "luckperms": {"installed": False,
                                       "operational": False},
                         "ts": time.time()})
        code, resp = _call_app("/api/admin/operators", cookies=self.cookies)
        self.assertTrue(resp["available"])
        self.assertEqual(resp["operators"][0]["username"], "Admin")
        self.assertFalse(resp["luckperms"]["installed"])

    def test_snapshot_capped(self):
        big = {"online": [{"username": f"P{i}"} for i in range(500)],
               "ts": time.time()}
        inject_snapshot(big)
        stored = agent_handler.agents[self.agent_id]["player_snapshot"]
        self.assertLessEqual(len(stored["online"]), 200)


class TestModerationEndpoints(Stage3EndpointBase):

    def test_kick_requires_auth(self):
        code, _ = _call_app("/api/admin/players/kick", "POST",
                            json.dumps({"username": "Steve"}))
        self.assertEqual(code, 401)

    def test_kick_valid_queues(self):
        code, resp = _call_app(
            "/api/admin/players/kick", "POST",
            json.dumps({"username": "Steve", "reason": "afk"}),
            cookies=self.cookies)
        self.assertEqual(code, 200)
        self.assertEqual(resp["status"], "queued")
        self.assertEqual(resp["state"], "pending")
        self.assertIn("action_id", resp)

    def test_kick_invalid_username_400(self):
        code, resp = _call_app(
            "/api/admin/players/kick", "POST",
            json.dumps({"username": "bad;name"}),
            cookies=self.cookies)
        self.assertEqual(code, 400)

    def test_kick_injection_username_400(self):
        code, resp = _call_app(
            "/api/admin/players/kick", "POST",
            json.dumps({"username": "Steve\nop Notch"}),
            cookies=self.cookies)
        self.assertEqual(code, 400)

    def test_ban_queues_with_sanitized_reason(self):
        code, resp = _call_app(
            "/api/admin/players/ban", "POST",
            json.dumps({"username": "Griefer", "reason": "bad\nreason"}),
            cookies=self.cookies)
        self.assertEqual(code, 200)
        params = command_handler.get_params(resp["action_id"])
        self.assertNotIn("\n", params.get("reason", ""))

    def test_unban_queues(self):
        code, resp = _call_app(
            "/api/admin/players/unban", "POST",
            json.dumps({"username": "Griefer"}),
            cookies=self.cookies)
        self.assertEqual(code, 200)

    def test_duplicate_mutation_busy(self):
        code, r1 = _call_app(
            "/api/admin/players/kick", "POST",
            json.dumps({"username": "Steve"}), cookies=self.cookies)
        code2, r2 = _call_app(
            "/api/admin/players/kick", "POST",
            json.dumps({"username": "Steve"}), cookies=self.cookies)
        self.assertEqual(r2.get("status"), "busy")
        self.assertEqual(r1["action_id"], r2["action_id"])

    def test_mutation_503_when_agent_offline(self):
        agent_handler.agents[self.agent_id]["last_heartbeat"] = \
            time.time() - 600
        try:
            code, resp = _call_app(
                "/api/admin/players/ban", "POST",
                json.dumps({"username": "Steve"}), cookies=self.cookies)
            self.assertEqual(code, 503)
        finally:
            agent_handler.heartbeat(self.agent_id, self.agent_secret, {})

    def test_queue_rejects_unknown_stage3_type(self):
        with self.assertRaises(ValueError):
            command_handler.queue_for_agent("rcon_exec", {"cmd": "x"})


class TestOperatorAuth(Stage3EndpointBase):
    """Step-up auth for privilege changes."""

    def test_op_grant_requires_password(self):
        code, resp = _call_app(
            "/api/admin/operators", "POST",
            json.dumps({"username": "Steve"}),
            cookies=self.cookies)
        self.assertEqual(code, 403)
        self.assertIn("password", resp["error"].lower())

    def test_op_grant_wrong_password(self):
        code, resp = _call_app(
            "/api/admin/operators", "POST",
            json.dumps({"username": "Steve", "confirm_password": "wrong"}),
            cookies=self.cookies)
        self.assertEqual(code, 403)

    def test_op_grant_correct_password_queues(self):
        code, resp = _call_app(
            "/api/admin/operators", "POST",
            json.dumps({"username": "Steve",
                        "confirm_password": "test-password-123"}),
            cookies=self.cookies)
        self.assertEqual(code, 200)
        self.assertEqual(resp["status"], "queued")

    def test_op_revoke_requires_password(self):
        code, resp = _call_app(
            "/api/admin/operators/Steve", "DELETE",
            json.dumps({}), cookies=self.cookies)
        self.assertEqual(code, 403)

    def test_op_revoke_with_password_queues(self):
        code, resp = _call_app(
            "/api/admin/operators/Steve", "DELETE",
            json.dumps({"confirm_password": "test-password-123"}),
            cookies=self.cookies)
        self.assertEqual(code, 200)

    def test_op_revoke_invalid_username(self):
        code, _ = _call_app(
            "/api/admin/operators/bad;name", "DELETE",
            json.dumps({"confirm_password": "test-password-123"}),
            cookies=self.cookies)
        self.assertEqual(code, 400)


class TestAuditAndHistory(Stage3EndpointBase):

    def test_mutation_writes_audit_entry(self):
        from backend.config import config
        before = len(config.AUDIT_LOG)
        _call_app("/api/admin/players/kick", "POST",
                  json.dumps({"username": "Steve", "reason": "afk"}),
                  cookies=self.cookies)
        entry = config.AUDIT_LOG[-1]
        self.assertTrue(entry["action"].startswith("player_"))
        self.assertEqual(entry["details"]["target"], "Steve")
        self.assertEqual(entry["user_id"], "admin")
        self.assertIn("action_id", entry["details"])

    def test_audit_does_not_record_passwords(self):
        from backend.config import config
        _call_app("/api/admin/operators", "POST",
                  json.dumps({"username": "Steve",
                              "confirm_password": "hunter2-secret"}),
                  cookies=self.cookies)
        # The failed step-up attempt must not persist the password
        for entry in config.AUDIT_LOG[-5:]:
            self.assertNotIn("hunter2-secret", json.dumps(entry))

    def test_history_endpoint_filters_by_target(self):
        from backend.config import config
        _call_app("/api/admin/players/kick", "POST",
                  json.dumps({"username": "Target1"}),
                  cookies=self.cookies)
        _call_app("/api/admin/players/kick", "POST",
                  json.dumps({"username": "Target2"}),
                  cookies=self.cookies)
        code, resp = _call_app(
            "/api/admin/players/history?username=Target1",
            cookies=self.cookies)
        self.assertEqual(code, 200)
        targets = {e["details"]["target"] for e in resp["entries"]
                   if e["details"].get("target")}
        self.assertEqual(targets, {"Target1"})

    def test_history_rejects_invalid_username(self):
        code, _ = _call_app(
            "/api/admin/players/history?username=bad;name",
            cookies=self.cookies)
        self.assertEqual(code, 400)

    def test_history_requires_auth(self):
        code, _ = _call_app("/api/admin/players/history")
        self.assertEqual(code, 401)

    def test_outcome_audit_written(self):
        """Agent result for a player action writes an outcome entry."""
        from backend.config import config
        code, resp = _call_app("/api/admin/players/kick", "POST",
                               json.dumps({"username": "Steve"}),
                               cookies=self.cookies)
        action_id = resp["action_id"]
        agent_handler.agents[self.agent_id]["last_heartbeat"] = \
            time.time()  # keep online
        _call_app(
            f"/api/agent/actions/{action_id}", "POST",
            json.dumps({"state": "failed", "error": "not online"}),
            headers={"X-Agent-ID": self.agent_id,
                     "X-Agent-Token": self.agent_secret})
        outcomes = [e for e in config.AUDIT_LOG
                    if e["action"] == "outcome_kick_player"]
        self.assertTrue(outcomes)
        self.assertEqual(outcomes[-1]["details"]["state"], "failed")
        self.assertEqual(outcomes[-1]["details"]["target"], "Steve")


class TestLogFileEndpoints(unittest.TestCase):
    """The log-history endpoints read a real file.

    Both `/api/logs` and `/api/admin/console/logs` call
    `server_status.get_log_path()`. That method did not exist, so both
    endpoints answered 500 on every request; nothing covered them, so the
    dashboard's log history was silently broken. These tests point the
    backend at a temporary server directory and go through the HTTP layer, so
    a missing/renamed method (or an unreadable encoding) fails here.
    """

    @classmethod
    def setUpClass(cls):
        cls.cookies = login_cookies()

    def setUp(self):
        from backend.server_status import server_status
        self.server_status = server_status
        self._real_dir = server_status.server_dir
        self.dir = tempfile.mkdtemp(prefix="rahee-logs-")
        os.makedirs(os.path.join(self.dir, "logs"), exist_ok=True)
        server_status.server_dir = Path(self.dir)

    def tearDown(self):
        self.server_status.server_dir = self._real_dir
        shutil.rmtree(self.dir, ignore_errors=True)

    def write_log(self, lines):
        path = os.path.join(self.dir, "logs", "latest.log")
        with open(path, "wb") as fh:
            fh.write(b"\n".join(l.encode("utf-8") for l in lines) + b"\n")
        return path

    def test_log_path_default_and_override(self):
        """Canonical path is logs/latest.log; LOG_FILE overrides it."""
        path = self.server_status.get_log_path()
        self.assertEqual(str(path),
                         os.path.join(self.dir, "logs", "latest.log"))
        with patch.dict(os.environ, {"LOG_FILE": "/tmp/custom-server.log"}):
            self.assertEqual(str(self.server_status.get_log_path()),
                             "/tmp/custom-server.log")

    def test_console_logs_returns_lines(self):
        self.write_log(["[INFO] Starting server", "[INFO] Done (1.0s)!",
                        "[INFO] Steve joined the game"])
        code, resp = _call_app("/api/admin/console/logs",
                               cookies=self.cookies)
        self.assertEqual(code, 200, resp)
        self.assertEqual(resp["total"], 3)
        self.assertIn("Steve joined the game", resp["lines"][-1])

    def test_console_logs_filter_search_limit(self):
        self.write_log([f"[INFO] line {i}" for i in range(50)] +
                       ["[WARN] something odd"])
        code, resp = _call_app("/api/admin/console/logs", query="search=odd",
                               cookies=self.cookies)
        self.assertEqual(code, 200)
        self.assertEqual(resp["filtered"], 1)
        self.assertEqual(resp["lines"], ["[WARN] something odd"])

        code, resp = _call_app("/api/admin/console/logs", query="limit=5",
                               cookies=self.cookies)
        self.assertEqual(code, 200)
        self.assertEqual(len(resp["lines"]), 5)

    def test_console_logs_missing_file_is_empty_not_500(self):
        """No server on this host yet is a normal state, not an error."""
        code, resp = _call_app("/api/admin/console/logs",
                               cookies=self.cookies)
        self.assertEqual(code, 200)
        self.assertEqual(resp["lines"], [])
        self.assertEqual(resp["total"], 0)

    def test_non_utf8_locale_content_still_served(self):
        """Paper logs are UTF-8; a locale-less host must not 500 on them."""
        self.write_log(["[INFO] Player \u00c9mile \u2014 joined the game"])
        code, resp = _call_app("/api/admin/console/logs",
                               cookies=self.cookies)
        self.assertEqual(code, 200, resp)
        self.assertIn("joined the game", resp["lines"][0])

    def test_logs_endpoint_returns_lines(self):
        self.write_log(["[INFO] hello", "[INFO] world"])
        code, resp = _call_app("/api/logs", cookies=self.cookies)
        self.assertEqual(code, 200, resp)
        self.assertEqual(resp["lines"], ["[INFO] hello", "[INFO] world"])
        self.assertEqual(resp["total_lines"], 2)

    def test_log_endpoints_require_auth(self):
        self.assertEqual(_call_app("/api/admin/console/logs")[0], 401)
        self.assertEqual(_call_app("/api/logs")[0], 401)


class TestHeartbeatViewContract(unittest.TestCase):
    """What the agent POSTs must actually reach the panel.

    The heartbeat *view* used to rebuild the payload from a hand-written subset
    of keys, so `player_snapshot` and the crash/restart fields never arrived:
    the agent showed as online while every player panel stayed on "agent has
    not reported player data yet". These tests go through the HTTP endpoint,
    not `agent_handler` directly — the older snapshot tests injected straight
    into the handler, which is exactly why the dropped fields went unnoticed.
    """

    AGENT_ID = "agent-hb-view-01"
    SECRET = "hb-view-secret-0123456789abcdef"

    def setUp(self):
        command_handler.actions.clear()
        agent_handler.agents.pop(self.AGENT_ID, None)
        agent_handler.register_agent(self.AGENT_ID, self.SECRET)
        # Leave the shared registry as it was found: other tests assert what a
        # request does when no agent is online, and a heartbeating agent left
        # behind here made those see this one instead.
        self.addCleanup(agent_handler.agents.pop, self.AGENT_ID, None)

    def _heartbeat(self, payload):
        return _call_app("/api/agent/heartbeat", "POST", json.dumps(payload),
                         headers={"X-Agent-ID": self.AGENT_ID,
                                  "X-Agent-Token": self.SECRET})

    def _only_my_agent_online(self):
        """The player endpoints report the first ONLINE agent, so quiet the
        others for the duration of a test instead of depending on test order."""
        saved = {}
        for agent_id, info in agent_handler.agents.items():
            if agent_id != self.AGENT_ID:
                saved[agent_id] = info.get("last_heartbeat")
                info["last_heartbeat"] = time.time() - 10_000
        self.addCleanup(self._restore_heartbeats, saved)

    @staticmethod
    def _restore_heartbeats(saved):
        for agent_id, value in saved.items():
            if agent_id in agent_handler.agents:
                agent_handler.agents[agent_id]["last_heartbeat"] = value

    def test_heartbeat_requires_credentials(self):
        code, _ = _call_app("/api/agent/heartbeat", "POST", "{}")
        self.assertEqual(code, 401)

    def test_player_snapshot_reaches_the_panel(self):
        snapshot = {
            "ts": time.time(),
            "online": [{"username": "Steve", "uuid": "u1",
                        "session_seconds": 12}],
            "whitelist": {"enabled": True,
                          "entries": [{"username": "Steve"}]},
            "bans": [],
            "ops": [{"username": "Steve", "level": 4}],
            "luckperms": {"installed": True, "operational": True},
        }
        code, resp = self._heartbeat({"paper_running": True, "paper_pid": 4242,
                                      "player_snapshot": snapshot})
        self.assertEqual(code, 200, resp)
        self._only_my_agent_online()

        code, resp = _call_app("/api/admin/players", cookies=login_cookies())
        self.assertEqual(code, 200)
        self.assertTrue(resp["available"], resp)
        self.assertEqual([p["username"] for p in resp["online"]], ["Steve"])

        code, resp = _call_app("/api/admin/whitelist", cookies=login_cookies())
        self.assertEqual(code, 200)
        self.assertTrue(resp["available"])
        self.assertTrue(resp["enabled"])
        self.assertEqual(len(resp["entries"]), 1)

        code, resp = _call_app("/api/admin/operators", cookies=login_cookies())
        self.assertEqual(code, 200)
        self.assertTrue(resp["available"])
        self.assertEqual(len(resp["operators"]), 1)

    def test_lifecycle_and_crash_fields_reach_the_panel(self):
        code, resp = self._heartbeat({
            "server_state": "CRASHED",
            "desired_state": "RUNNING",
            "paper_running": False,
            "exit_code": 137,
            "restart_attempts": 2,
            "paper_startup_state": "failed",
            "last_error": "The server has stopped without saving",
            "log_tail": "line one\nline two",
        })
        self.assertEqual(code, 200, resp)

        status = agent_handler.get_agent_status(self.AGENT_ID)
        self.assertEqual(status["server_state"], "CRASHED")
        self.assertEqual(status["desired_state"], "RUNNING")
        self.assertEqual(status["exit_code"], 137)
        self.assertEqual(status["restart_attempts"], 2)
        self.assertEqual(status["paper_startup_state"], "failed")
        self.assertIn("stopped without saving", status["last_error"])
        self.assertIn("line two", status["log_tail"])

    def test_caps_and_allowlists_still_apply_through_the_view(self):
        from backend.agent_handler import MAX_ERROR_CHARS, MAX_LOG_TAIL_CHARS

        code, _ = self._heartbeat({
            "server_state": "BANANA",           # not a lifecycle state
            "restart_attempts": -5,              # negative is clamped to 0
            "log_tail": "x" * (MAX_LOG_TAIL_CHARS + 500),
            "last_error": "e" * (MAX_ERROR_CHARS + 500),
        })
        self.assertEqual(code, 200)

        status = agent_handler.get_agent_status(self.AGENT_ID)
        self.assertNotEqual(status["server_state"], "BANANA")
        self.assertEqual(status["restart_attempts"], 0)
        self.assertEqual(len(status["log_tail"]), MAX_LOG_TAIL_CHARS)
        self.assertEqual(len(status["last_error"]), MAX_ERROR_CHARS)

    def test_a_snapshot_over_the_caps_is_stored_capped(self):
        """A server with more bans than the cap must not blow up the panel."""
        code, _ = self._heartbeat({
            "paper_running": True,
            "player_snapshot": {"ts": time.time(),
                                "bans": [{"username": f"p{i}"} for i in range(600)]},
        })
        self.assertEqual(code, 200)
        snap = agent_handler.get_agent_status(self.AGENT_ID)["player_snapshot"]
        self.assertEqual(len(snap["bans"]), 500)

    def test_a_payload_beyond_bottles_request_limit_is_refused(self):
        """Bottle caps a request body at MEMFILE_MAX (100 KiB); a heartbeat
        larger than that is refused outright rather than half-parsed."""
        huge = {"paper_running": True,
                "player_snapshot": {"ts": time.time(),
                                    "bans": [{"username": "p" * 60} for _ in range(4000)]}}
        code, _ = self._heartbeat(huge)
        self.assertEqual(code, 413)
        # and the stored snapshot is untouched by the refused request
        status = agent_handler.get_agent_status(self.AGENT_ID)
        self.assertIsNone(status["player_snapshot"])


class TestRedaction(unittest.TestCase):
    """Secrets never leak into logs, snapshots, or errors."""

    def test_snapshot_sanitizer_drops_unknown_keys(self):
        snap = {"online": [], "evil_key": "x" * 10000,
                "ts": 123.0, "credentials": {"password": "secret"}}
        clean = _sanitize_snapshot(snap)
        self.assertNotIn("evil_key", clean)
        self.assertNotIn("credentials", clean)

    def test_snapshot_caps_bans(self):
        snap = {"bans": [{"username": f"B{i}"} for i in range(1000)],
                "ts": 1.0}
        clean = _sanitize_snapshot(snap)
        self.assertLessEqual(len(clean["bans"]), 500)

    def test_ban_reason_redacted_in_log_buffer(self):
        """A reason containing a token pattern is redacted at the buffer."""
        from backend.log_buffer import LogBuffer
        buf = LogBuffer()
        buf.append("kicked: token=supersecrettoken123456 rec")
        stored = buf.tail(since=0)["lines"][0]["text"]
        self.assertNotIn("supersecrettoken123456", stored)

    def test_error_messages_do_not_contain_reason(self):
        """Step-up failure error is generic (no echo of submitted values)."""
        cookies = login_cookies()
        code, resp = _call_app(
            "/api/admin/operators", "POST",
            json.dumps({"username": "Steve", "confirm_password": "leaky"}),
            cookies=cookies)
        self.assertEqual(code, 403)
        self.assertNotIn("leaky", json.dumps(resp))


if __name__ == "__main__":
    unittest.main(verbosity=2)
