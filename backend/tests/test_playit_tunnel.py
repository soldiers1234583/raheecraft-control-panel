#!/usr/bin/env python3
"""
Playit.gg tunnel tests.

The tunnel is the only thing that makes the server reachable from outside, and
it handles an account token. These tests pin the two properties that matter:

  * the token is never exposed — not on the connector's command line (where
    `ps` would show it to any local user) and not in the output that is printed
    and appended to playit.log;
  * a public address is only reported when the connector actually produced one.
    A missing port must never be filled in with a guess, because that sends
    players to an address nobody is listening on.
"""

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import playit_setup  # noqa: E402
from playit_setup import (  # noqa: E402
    PlayitTunnel, PLAYIT_TOKEN_VAR, read_public_address)

TOKEN = "PLAYIT-ACCOUNT-TOKEN-abcdef0123456789"


class TestTokenIsNeverExposed(unittest.TestCase):

    def setUp(self):
        self.tunnel = PlayitTunnel(dry_run=True)
        self.tunnel.token = TOKEN

    def test_redact_removes_the_token_from_output(self):
        line = f"playit: authenticating with {TOKEN} against playit.gg"
        scrubbed = self.tunnel._redact(line)
        self.assertNotIn(TOKEN, scrubbed)
        self.assertIn("***", scrubbed)

    def test_redact_leaves_ordinary_output_alone(self):
        for line in ("listening on 0.0.0.0:25565",
                     "no address assigned yet",
                     "ready"):
            self.assertEqual(self.tunnel._redact(line), line)

    def test_redact_ignores_values_too_short_to_be_a_token(self):
        # A short value would scrub unrelated text; it cannot be a real token.
        self.tunnel.token = "abc"
        self.assertEqual(self.tunnel._redact("value abc"), "value abc")

    def test_redact_also_covers_the_environment_token(self):
        self.tunnel.token = None
        with patch.dict(os.environ, {PLAYIT_TOKEN_VAR: TOKEN}):
            self.assertNotIn(TOKEN, self.tunnel._redact(f"token {TOKEN}"))

    def test_token_is_passed_by_environment_not_on_the_command_line(self):
        """A token in argv is visible to every local user via `ps`."""
        # Real launching path with Popen mocked: dry-run mode never builds the
        # command, so it would not exercise the argv/env split at all.
        self.tunnel = PlayitTunnel(dry_run=False)
        self.tunnel.token = TOKEN
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "playit"
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)

            proc = MagicMock()
            proc.pid = 4321
            proc.stdout = None          # no drain thread, no blocking read
            proc.poll.return_value = 0  # already finished: do not wait

            with patch.dict(os.environ, {PLAYIT_TOKEN_VAR: TOKEN}), \
                    patch("playit_setup.subprocess.Popen",
                          return_value=proc) as popen:
                started = self.tunnel.start_agent(str(binary))

        self.assertTrue(started)
        argv = popen.call_args[0][0]
        self.assertNotIn(TOKEN, " ".join(argv))
        self.assertFalse(any(TOKEN in str(a) for a in argv))
        # ...but the connector still receives it, through its environment.
        self.assertEqual(popen.call_args[1]["env"].get(PLAYIT_TOKEN_VAR), TOKEN)


class _SharedAddressFile(unittest.TestCase):
    """Redirect the address file and log to a throwaway dir for one test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patch("playit_setup.PLAYIT_DIR", self._tmp.name).start()
        patch("playit_setup.ADDRESS_FILE",
              os.path.join(self._tmp.name, "public_address.json")).start()
        patch("playit_setup.PLAYIT_LOG",
              os.path.join(self._tmp.name, "playit.log")).start()
        self.addCleanup(patch.stopall)


class TestPublicAddressIsNeverInvented(_SharedAddressFile):

    def setUp(self):
        super().setUp()
        self.tunnel = PlayitTunnel(dry_run=True)

    def test_real_address_is_reported(self):
        self.tunnel._parse_public_address(
            ["connecting to tunnel", "address: rahee.playit.gg:12345 assigned"])
        self.assertEqual(self.tunnel.public_address, ("rahee.playit.gg", "12345"))

    def test_no_address_means_no_address(self):
        self.tunnel._parse_public_address(
            ["claim code: ABCD", "waiting for a tunnel to be claimed"])
        self.assertIsNone(self.tunnel.public_address)

    def test_hostname_without_a_port_is_not_an_address(self):
        """The old code defaulted the port to 25565 and reported that."""
        self.tunnel._parse_public_address(["assigned rahee.playit.gg"])
        self.assertIsNone(self.tunnel.public_address)

    def test_a_claimed_address_requires_the_playit_domain(self):
        self.tunnel._parse_public_address(["local server on 127.0.0.1:25565"])
        self.assertIsNone(self.tunnel.public_address)

    def test_get_public_address_returns_none_before_assignment(self):
        self.assertIsNone(self.tunnel.get_public_address())

    def test_ply_gg_addresses_are_recognized(self):
        """Playit's common short domain must not read as 'unknown'.

        Real addresses are usually `name.<region>.at.ply.gg:port`, which the
        old `*.playit.gg`-only match silently dropped.
        """
        self.tunnel._parse_public_address(
            ["tunnel address: rahee.gl.at.ply.gg:25565"])
        self.assertEqual(self.tunnel.public_address,
                         ("rahee.gl.at.ply.gg", "25565"))


class TestPublicAddressReachesTheAgent(_SharedAddressFile):
    """The address the connector finds must be readable by the agent.

    The notebook starts the connector, but the agent answers the panel's
    heartbeats from a different thread and its own PlayitTunnel instance. Only
    a shared file crosses that boundary; without it the panel reported the
    tunnel as running with no address ("address unknown") forever.
    """

    def test_parsed_address_is_persisted_and_readable(self):
        tunnel = PlayitTunnel(dry_run=False)
        tunnel._parse_public_address(["assigned rahee.playit.gg:12345"])
        self.assertEqual(read_public_address(), "rahee.playit.gg:12345")

    def test_one_instance_reads_another_instances_address(self):
        writer = PlayitTunnel(dry_run=False)
        writer._parse_public_address(["assigned rahee.playit.gg:12345"])
        # A fresh instance (the agent's) has never run a connector...
        reader = PlayitTunnel(dry_run=True)
        self.assertIsNone(reader.public_address)
        # ...but still learns the real address from the shared file.
        self.assertEqual(reader.get_public_address(),
                         ("rahee.playit.gg", "12345"))

    def test_no_address_is_persisted_when_none_was_reported(self):
        tunnel = PlayitTunnel(dry_run=False)
        tunnel._parse_public_address(["claim code: ABCD"])
        self.assertIsNone(read_public_address())

    def test_clear_removes_a_previous_sessions_address(self):
        playit_setup._write_public_address("old.playit.gg", "11111")
        self.assertEqual(read_public_address(), "old.playit.gg:11111")
        playit_setup.clear_public_address()
        self.assertIsNone(read_public_address())

    def test_clearing_also_drops_a_stale_log_address(self):
        """A new connector must not inherit the previous one's address."""
        with open(playit_setup.PLAYIT_LOG, "w", encoding="utf-8") as fh:
            fh.write("tunnel address: old.gl.at.ply.gg:11111\n")
        self.assertEqual(read_public_address(), "old.gl.at.ply.gg:11111")
        playit_setup.clear_public_address()
        self.assertIsNone(read_public_address())
        self.assertTrue(os.path.exists(playit_setup.PLAYIT_LOG + ".prev"))

    def test_the_live_output_drain_persists_the_address(self):
        """The address must be captured as output arrives, not only on demand."""
        tunnel = PlayitTunnel(dry_run=False)
        stream = MagicMock()
        stream.readline.side_effect = [
            b"connecting to tunnel server\n",
            b"tunnel address: rahee.gl.at.ply.gg:25565\n",
            b"",
        ]
        tunnel.process = MagicMock()
        tunnel.process.stdout = stream
        tunnel._start_output_drain()
        deadline = time.time() + 3
        while time.time() < deadline:
            if read_public_address() == "rahee.gl.at.ply.gg:25565":
                break
            time.sleep(0.02)
        self.assertEqual(read_public_address(), "rahee.gl.at.ply.gg:25565")

    def test_address_is_recovered_from_the_connector_log(self):
        """A re-run of the cell (no new connector) still reports the address."""
        with open(playit_setup.PLAYIT_LOG, "w", encoding="utf-8") as fh:
            fh.write("boot\n")
            fh.write("tunnel address: rahee.gl.at.ply.gg:25565\n")
        self.assertIsNone(playit_setup._read_recorded_address())
        self.assertEqual(read_public_address(), "rahee.gl.at.ply.gg:25565")

    def test_a_log_without_a_playit_address_yields_nothing(self):
        with open(playit_setup.PLAYIT_LOG, "w", encoding="utf-8") as fh:
            fh.write("local server on 127.0.0.1:25565\n")
        self.assertIsNone(read_public_address())


class TestAgentReportsTheTunnelAddress(_SharedAddressFile):
    """`check_playit_tunnel` must surface the shared address to the panel."""

    def _check(self, pid):
        from agent import process_info
        from agent import status as agent_status
        with patch.object(process_info, "find_playit_pid", lambda: pid):
            return agent_status.check_playit_tunnel()

    def test_running_connector_reports_the_shared_address(self):
        playit_setup._write_public_address("rahee.playit.gg", "12345")
        self.assertEqual(self._check(4321), (True, "rahee.playit.gg:12345"))

    def test_running_connector_without_an_address_stays_honest(self):
        self.assertEqual(self._check(4321), (True, None))

    def test_stopped_connector_never_reports_an_address(self):
        playit_setup._write_public_address("rahee.playit.gg", "12345")
        self.assertEqual(self._check(None), (False, None))


if __name__ == "__main__":
    unittest.main()
