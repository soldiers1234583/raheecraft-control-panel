#!/usr/bin/env python3
"""
RaheeCraft Agent Reconnect Tests
================================
The agent must survive the panel being restarted.

The panel keeps its agent registry in memory, so a restart (or redeploy)
forgets every agent id while the agent keeps the credentials it registered
with. Before this was covered, the agent's inner loop retried the heartbeat
forever against a panel that answered 401, so it stayed permanently invisible
until it was restarted by hand — the dashboard showed every section empty.

These tests pin the contract:
  - 401/403 on a heartbeat is reported as AUTH_REJECTED
  - AUTH_REJECTED leaves the heartbeat loop and re-registers
  - Re-registration re-sends recent console lines (a fresh panel's console
    would otherwise stay blank until the server happened to log something)
  - A transient failure (500, unreachable) keeps retrying in place
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

# Config reads the environment at import time.
os.environ.setdefault("AGENT_ID", "agent-test-reconnect")
os.environ.setdefault("AGENT_SECRET", "test-secret-0123456789abcdef")
os.environ.setdefault("BACKEND_URL", "http://backend.invalid")

from agent import main as agent_main  # noqa: E402
from agent.main import ControlAgent, AUTH_REJECTED  # noqa: E402
from agent.logstream import log_streamer  # noqa: E402
from agent.config import config  # noqa: E402


class FakeResponse:
    """Minimal stand-in for an httpx response."""

    def __init__(self, status_code):
        self.status_code = status_code
        self.text = ""


def make_agent():
    """An agent whose network, lifecycle and streaming are all inert."""
    agent = ControlAgent()
    agent.poll_and_execute_actions = lambda: 0
    agent.push_logs = lambda: True
    return agent


class TestHeartbeatStatus(unittest.TestCase):
    """`send_heartbeat` must distinguish "unknown agent" from "try later"."""

    def setUp(self):
        self.agent = make_agent()

    def heartbeat_with(self, status_code):
        with patch.object(agent_main, "get_agent_status", lambda: {}), \
                patch.object(agent_main.player_tracker, "snapshot", lambda: {}), \
                patch("httpx.post", return_value=FakeResponse(status_code)):
            return self.agent.send_heartbeat()

    def test_success(self):
        self.assertIs(self.heartbeat_with(200), True)

    def test_401_means_re_register(self):
        self.assertEqual(self.heartbeat_with(401), AUTH_REJECTED)

    def test_403_means_re_register(self):
        self.assertEqual(self.heartbeat_with(403), AUTH_REJECTED)

    def test_500_is_transient(self):
        """A backend error is not an identity problem: just retry."""
        self.assertIs(self.heartbeat_with(500), False)

    def test_unreachable_backend_is_transient(self):
        import httpx
        with patch.object(agent_main, "get_agent_status", lambda: {}), \
                patch.object(agent_main.player_tracker, "snapshot", lambda: {}), \
                patch("httpx.post", side_effect=httpx.ConnectError("down")):
            self.assertIs(self.agent.send_heartbeat(), False)


class TestRunLoopReRegisters(unittest.TestCase):
    """The loop must act on AUTH_REJECTED instead of looping forever."""

    def run_loop(self, heartbeats):
        """Run the real `run()` against scripted heartbeat answers.

        Returns (register_calls, resets, heartbeat_calls).
        """
        agent = make_agent()
        calls = {"register": 0, "heartbeat": 0, "reset": 0}
        script = list(heartbeats)

        def fake_register():
            calls["register"] += 1
            return True

        def fake_heartbeat():
            calls["heartbeat"] += 1
            answer = script.pop(0) if script else True
            if answer is True:
                # Let the loop finish once it has done its work.
                agent.running = False
            return answer

        def fake_reset():
            calls["reset"] += 1

        agent.register = fake_register
        agent.send_heartbeat = fake_heartbeat
        with patch.object(config, "POLL_INTERVAL_SECONDS", 0), \
                patch.object(agent_main.lifecycle, "on_event", lambda cb: None), \
                patch.object(agent_main.lifecycle, "start_supervisor",
                             lambda: None), \
                patch.object(agent_main.lifecycle, "stop_supervisor",
                             lambda: None), \
                patch.object(log_streamer, "reset", fake_reset):
            agent.run()
        return calls

    def test_rejected_heartbeat_triggers_re_registration(self):
        calls = self.run_loop([AUTH_REJECTED, True])
        self.assertEqual(calls["register"], 2,
                         "the agent must register again after a 401")
        self.assertEqual(calls["heartbeat"], 2)

    def test_re_registration_resends_recent_console_lines(self):
        calls = self.run_loop([AUTH_REJECTED, True])
        self.assertEqual(calls["reset"], 1,
                         "a fresh panel needs recent log lines re-sent")

    def test_transient_failure_does_not_re_register(self):
        """Only an identity rejection re-registers; a 500 just retries."""
        calls = self.run_loop([False, False, True])
        self.assertEqual(calls["register"], 1)
        self.assertEqual(calls["heartbeat"], 3)
        self.assertEqual(calls["reset"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
