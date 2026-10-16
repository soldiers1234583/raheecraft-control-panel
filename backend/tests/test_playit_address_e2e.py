#!/usr/bin/env python3
"""
End-to-end test of the Playit join address, from connector to panel.

The panel's "Java Address" was blank because nothing carried the address from
the process that runs the connector to the agent that answers the panel's
heartbeats. These tests run the real pieces in one process — a fake connector
binary, `PlayitTunnel`, the agent's `check_playit_tunnel` and the backend's
`_playit_view` — and assert the address the dashboard would render.

They also pin the two honest outcomes: when the connector prints no address the
panel must say "unknown" rather than invent one, and the explicit
`PLAYIT_PUBLIC_ADDRESS` override must reach the panel when parsing cannot.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

os.environ.setdefault("SERVER_DIR", "/kaggle/working/minecraft_server")

import playit_setup  # noqa: E402
from playit_setup import PlayitTunnel  # noqa: E402
from agent import process_info  # noqa: E402
from agent import status as agent_status  # noqa: E402
from backend.app import _playit_view  # noqa: E402


def _write_connector(path: Path, lines) -> str:
    """A tiny executable that prints `lines` and exits."""
    binary = path / "playit"
    body = "#!/bin/sh\n" + "".join(f"echo {line!r}\n" for line in lines)
    binary.write_text(body)
    binary.chmod(0o755)
    return str(binary)


class _PlayitEnv(unittest.TestCase):
    """Point the module's files at a throwaway directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        patch("playit_setup.PLAYIT_DIR", str(self.dir)).start()
        patch("playit_setup.ADDRESS_FILE",
              str(self.dir / "public_address.json")).start()
        patch("playit_setup.PLAYIT_LOG", str(self.dir / "playit.log")).start()
        os.environ.pop("PLAYIT_PUBLIC_ADDRESS", None)
        self.addCleanup(patch.stopall)

    def _run_connector(self, lines):
        tunnel = PlayitTunnel(dry_run=False)
        tunnel.setup_directory()
        self.assertTrue(tunnel.start_agent(_write_connector(self.dir, lines)))
        # let the drain thread flush the scripted output
        deadline = time.time() + 5
        while time.time() < deadline and not playit_setup.read_public_address():
            time.sleep(0.05)
        return tunnel

    def _as_the_panel_sees_it(self):
        """What the agent reports, then what the dashboard renders from it."""
        with patch.object(process_info, "find_playit_pid", lambda: 4321):
            running, address = agent_status.check_playit_tunnel()
        view = _playit_view({"playit_status": "running" if running else "stopped",
                             "playit_address": address}, {})
        return running, address, view


class TestAddressReachesThePanel(_PlayitEnv):

    def test_a_real_assignment_line_reaches_the_panel(self):
        self._run_connector([
            "[INFO] playit-agent starting",
            "[INFO] connecting to tunnel server",
            "[INFO] tunnel 'rahee' address: rahee.gl.at.ply.gg:25565",
            "[INFO] ready",
        ])
        running, address, view = self._as_the_panel_sees_it()
        self.assertEqual((running, address), (True, "rahee.gl.at.ply.gg:25565"))
        self.assertEqual(view["hostname"], "rahee.gl.at.ply.gg")
        self.assertEqual(view["port"], "25565")

    def test_output_without_an_address_reports_unknown_not_a_guess(self):
        self._run_connector(["[INFO] playit-agent starting",
                             "[INFO] waiting for a tunnel to be claimed"])
        running, address, view = self._as_the_panel_sees_it()
        self.assertEqual((running, address), (True, None))
        self.assertIsNone(view["address"])

    def test_the_override_reaches_the_panel_when_output_cannot_be_parsed(self):
        """The escape hatch: set PLAYIT_PUBLIC_ADDRESS from the Playit dashboard."""
        self._run_connector(["[INFO] playit-agent starting", "[INFO] ready"])
        with patch.dict(os.environ,
                        {"PLAYIT_PUBLIC_ADDRESS": "rahee.gl.at.ply.gg:25565"}):
            running, address, view = self._as_the_panel_sees_it()
        self.assertEqual((running, address), (True, "rahee.gl.at.ply.gg:25565"))
        self.assertEqual(view["hostname"], "rahee.gl.at.ply.gg")
        self.assertEqual(view["port"], "25565")

    def test_a_stopped_connector_never_reports_an_address(self):
        self._run_connector(["[INFO] tunnel address: rahee.playit.gg:25565"])
        with patch.object(process_info, "find_playit_pid", lambda: None):
            running, address = agent_status.check_playit_tunnel()
        self.assertEqual((running, address), (False, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
