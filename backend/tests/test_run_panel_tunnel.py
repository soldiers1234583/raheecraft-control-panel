"""
Tests for the local tunnel runner (run_panel_tunnel.py).

These cover the two pieces that decide whether a locally-run connector works
and stays safe: finding the token (environment before file, never echoed) and
deciding whether this host needs the `proot` wrapper that gives a Go binary a
`resolv.conf` (Android/Termux does; a normal Linux host does not).

The host checks are patched rather than performed, so the tests never wrap
anything in proot and never touch the real `/etc`.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import run_panel_tunnel as runner
from cloudflare_tunnel import CloudflareTunnel, TOKEN_VAR


class TokenResolutionTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)
        self._old_token = os.environ.pop(TOKEN_VAR, None)
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        os.environ.pop(TOKEN_VAR, None)
        if self._old_token is not None:
            os.environ[TOKEN_VAR] = self._old_token

    def test_token_file_is_read_and_trimmed(self):
        path = self.tmpdir / "token"
        path.write_text("  some-token-value \n")
        token, source = runner.resolve_token(str(path))
        self.assertEqual(token, "some-token-value")
        self.assertEqual(source, str(path))

    def test_environment_wins_over_the_file(self):
        path = self.tmpdir / "token"
        path.write_text("from-file\n")
        os.environ[TOKEN_VAR] = "from-environment"
        token, source = runner.resolve_token(str(path))
        self.assertEqual(token, "from-environment")
        self.assertIn("environment", source)

    def test_no_token_anywhere_is_reported_as_empty(self):
        with mock.patch.object(runner, "DEFAULT_TOKEN_FILE",
                               self.tmpdir / "absent"):
            token, source = runner.resolve_token()
        self.assertEqual((token, source), ("", ""))

    def test_source_label_never_repeats_the_secret(self):
        os.environ[TOKEN_VAR] = "do-not-print-me"
        token, source = runner.resolve_token()
        self.assertEqual(token, "do-not-print-me")
        self.assertNotIn("do-not-print-me", source)

    def test_check_mode_without_a_token_exits_2(self):
        # --check still probes the local panel and the public hostname; both are
        # patched so the test cannot depend on the network or on a running
        # panel, and it asserts the exit code a caller can branch on.
        unreachable = {"ok": False, "url": "https://example.invalid/health",
                       "detail": "unreachable"}
        with mock.patch.object(runner, "load_env_file", return_value=0), \
             mock.patch.object(runner, "resolve_token", return_value=("", "")), \
             mock.patch.object(CloudflareTunnel, "verify_local_service",
                               return_value=False), \
             mock.patch.object(CloudflareTunnel, "verify_public_service",
                               return_value=unreachable):
            self.assertEqual(runner.main(["--check"]), 2)


class LauncherDetectionTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmpdir = Path(self.tmp.name)

    def test_normal_host_gets_no_wrapper(self):
        with mock.patch.object(runner, "resolv_conf_is_usable", return_value=True):
            launcher, reason = runner.detect_launcher()
        self.assertEqual(launcher, "")
        self.assertIn("readable", reason)

    def test_no_resolv_conf_and_no_proot_is_reported_not_faked(self):
        with mock.patch.object(runner, "resolv_conf_is_usable", return_value=False), \
             mock.patch("shutil.which", return_value=None):
            launcher, reason = runner.detect_launcher()
        self.assertEqual(launcher, "")
        self.assertIn("proot", reason)

    def test_proot_launcher_mounts_the_prefix_resolv_conf(self):
        prefix = self.tmpdir / "usr"
        (prefix / "etc").mkdir(parents=True)
        (prefix / "etc" / "resolv.conf").write_text("nameserver 8.8.8.8\n")
        with mock.patch.object(runner, "resolv_conf_is_usable", return_value=False), \
             mock.patch("shutil.which", return_value="/usr/bin/proot"), \
             mock.patch.dict(os.environ, {"PREFIX": str(prefix)}):
            launcher, reason = runner.detect_launcher()
        self.assertIn("/usr/bin/proot -b", launcher)
        self.assertIn(f"{prefix}/etc/resolv.conf:/system/etc/resolv.conf", launcher)
        self.assertTrue(launcher.endswith("-r /"))
        self.assertIn("mounting", reason)

    def test_fallback_resolv_conf_is_written_when_the_host_has_none(self):
        empty_prefix = self.tmpdir / "no-resolv-conf-here"
        empty_prefix.mkdir()
        with mock.patch.object(runner, "resolv_conf_is_usable", return_value=False), \
             mock.patch("shutil.which", return_value="/usr/bin/proot"), \
             mock.patch.dict(os.environ, {"PREFIX": str(empty_prefix)}), \
             mock.patch("pathlib.Path.home", return_value=self.tmpdir):
            launcher, reason = runner.detect_launcher()
        fallback = self.tmpdir / ".rahee-bin" / "resolv.conf"
        self.assertTrue(fallback.is_file())
        self.assertIn("nameserver", fallback.read_text())
        self.assertIn(str(fallback), launcher)
        self.assertIn("wrote", reason)


if __name__ == "__main__":
    unittest.main()
