#!/usr/bin/env python3
"""
Tests for the authoritative server lifecycle manager (agent/lifecycle.py).

These tests never touch the real world, the real server directory, or a real
JVM: every process is a fake Popen and every directory is a temporary one. What
they do check is the behaviour the dashboard and the notebook depend on:

  * a start is idempotent and never spawns a second JVM;
  * "running" means Paper itself reported ready, not that a subprocess exists;
  * a stop goes console `stop` → SIGTERM → SIGKILL in that order, and says
    honestly when the shutdown was not graceful;
  * `desired_state` gates crash recovery: nothing restarts after an intentional
    Stop, and recovery is bounded with backoff;
  * an unrecoverable startup failure is not retried at all;
  * concurrent lifecycle operations are refused instead of racing;
  * the backend reports the agent's lifecycle state, not a backend-host guess.
"""

import json
import os
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.lifecycle import (  # noqa: E402
    ServerLifecycle, ServerState, DesiredState, ConsoleLogWriter,
    get_lifecycle,
)

BOOT_LINE = "[12:00:00 INFO]: Starting minecraft server version 1.21.4"
READY_LINE = '[12:00:47 INFO]: Done (47.069s)! For help, type "help"'


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeOutput:
    """Iterable with readline(), matching the drained stdout pipe."""

    def __init__(self, lines=None):
        self._lines = list(lines or [])
        self.closed = False

    def readline(self):
        return self._lines.pop(0) if self._lines else ""

    def close(self):
        self.closed = True


class FakeStdin:
    def __init__(self, on_write=None):
        self.lines = []
        self.closed = False
        self._on_write = on_write

    def write(self, text):
        line = text.rstrip("\n")
        self.lines.append(line)
        if self._on_write:
            self._on_write(line)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakeProc:
    """Minimal stand-in for subprocess.Popen with the surface we use."""

    _next_pid = 4000

    def __init__(self, stdout_lines=None, exit_code=None, pid=None,
                 on_console=None):
        FakeProc._next_pid += 1
        self.pid = pid or FakeProc._next_pid
        self.stdout = FakeOutput(stdout_lines)
        self._exit_code = exit_code
        self.returncode = None
        self.stdin = FakeStdin(on_write=on_console)

    def poll(self):
        if self._exit_code is not None:
            self.returncode = self._exit_code
            return self._exit_code
        return None

    def kill_externally(self, code):
        """Simulate the JVM dying on its own (crash)."""
        self._exit_code = code
        self.returncode = code


def write_log(server_dir: Path, lines, append=False):
    logs = Path(server_dir) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    mode = "a" if append else "w"
    with open(logs / "latest.log", mode, encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def make_server_dir(ready=False, jar=True, eula=True, world=True, **extra):
    """A temporary server directory that passes preflight.

    A real server directory has a world, so the fixture has one by default:
    preflight refuses to start when a world is expected but `level.dat` is
    missing, because Paper would generate an empty world and the Drive backup
    would then overwrite the real one.
    """
    d = Path(tempfile.mkdtemp(prefix="rahee-lifecycle-"))
    if jar:
        # A real (tiny) ZIP so the `PK` magic check passes.
        (d / "paper.jar").write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    if eula:
        (d / "eula.txt").write_text("eula=true\n")
    (d / "logs").mkdir(exist_ok=True)
    if ready:
        write_log(d, [BOOT_LINE, READY_LINE])
    else:
        write_log(d, [BOOT_LINE])
    if world:
        (d / "world").mkdir(exist_ok=True)
        (d / "world" / "level.dat").write_bytes(b"world-data")
    for name in extra:
        (d / name).write_bytes(b"")
    return d


class LifecycleTestBase(unittest.TestCase):
    def setUp(self):
        self.dirs = []

    def tearDown(self):
        import shutil
        for d in self.dirs:
            shutil.rmtree(d, ignore_errors=True)

    def make_dir(self, **kwargs):
        d = make_server_dir(**kwargs)
        self.dirs.append(d)
        return d

    def manager(self, server_dir, **env):
        """A lifecycle manager bound to a temp dir, with knobs set via env."""
        env.setdefault("START_TIMEOUT_SECONDS", "5")
        env.setdefault("START_SETTLE_SECONDS", "1")
        env.setdefault("STOP_TIMEOUT_SECONDS", "5")
        patcher = patch.dict(os.environ, {k: str(v) for k, v in env.items()})
        patcher.start()
        self.addCleanup(patcher.stop)
        # Preflight's `java -version` probe is a real JVM start: slow and
        # environment-dependent, and orthogonal to lifecycle behaviour. It is
        # pinned to a deterministic answer here so these tests stay fast and
        # hermetic; the probe itself is covered by its own test, and verified
        # for real by tools/real_verification.
        probe = patch.object(ServerLifecycle, "_java_version",
                             staticmethod(lambda java, cancel=None, timeout=20:
                                          'openjdk version "21.0.12" 2026-07-21'))
        probe.start()
        self.addCleanup(probe.stop)
        return ServerLifecycle(server_dir=str(server_dir))

    def started(self, on_console=None, **env):
        """A manager brought to RUNNING against a fake JVM.

        Returns (manager, fake_process, start_result). The fake dies when the
        console receives whatever `on_console` decides, which lets the stop
        tests exercise the real graceful path without a real Minecraft server.
        """
        d = self.make_dir(ready=True)
        lm = self.manager(d, **env)
        proc = FakeProc(stdout_lines=[BOOT_LINE + "\n", READY_LINE + "\n"],
                        on_console=on_console)
        with patch("agent.lifecycle.subprocess.Popen", return_value=proc), \
                patch("agent.lifecycle.find_paper_pid", return_value=None):
            result = lm.start()
        return lm, proc, result

    def detector(self, proc):
        """A find_paper_pid stand-in that tracks the fake process's life."""
        return lambda *_a, **_k: (proc.pid if proc.poll() is None else None)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

class TestPreflight(LifecycleTestBase):

    def test_missing_jar_and_eula_are_named(self):
        d = self.make_dir(jar=False, eula=False)
        lm = self.manager(d)
        report = lm.preflight()
        self.assertFalse(report["ok"])
        joined = " ".join(report["problems"])
        self.assertIn("paper.jar", joined)
        self.assertIn("EULA", joined)
        # Cell number is part of the instruction — never the wrong one.
        self.assertIn("Cell 6", joined)
        self.assertIn("Cell 7", joined)

    def test_java_version_is_actually_checked(self):
        """A JVM too old for 1.21.x must be reported, not discovered at runtime."""
        d = self.make_dir(ready=True)
        lm = self.manager(d)
        with patch.object(ServerLifecycle, "_java_version",
                          staticmethod(lambda java, cancel=None, timeout=20:
                                       'openjdk version "17.0.9" 2023-10-17')):
            pre = lm.preflight()
        self.assertFalse(pre["ok"])
        self.assertTrue(any("too old" in p for p in pre["problems"]),
                        str(pre["problems"]))
        self.assertIn("17", str(pre["info"].get("java_version")))

    def test_unreadable_java_probe_is_inconclusive_not_fatal(self):
        """A probe that fails for its own reasons must not fail the preflight."""
        d = self.make_dir(ready=True)
        lm = self.manager(d)
        with patch.object(ServerLifecycle, "_java_version",
                          staticmethod(lambda java, cancel=None, timeout=20: None)):
            pre = lm.preflight()
        self.assertTrue(pre["ok"], str(pre["problems"]))
        self.assertIsNone(pre["info"]["java_version"])

    def test_missing_java_is_reported_before_spawning(self):
        d = self.make_dir()
        lm = self.manager(d)
        real_exists = Path.exists

        def fake_exists(self):
            return False if str(self).endswith("bin/java") else real_exists(self)

        with patch("shutil.which", return_value=None), \
                patch.object(Path, "exists", fake_exists):
            report = lm.preflight()
        self.assertFalse(report["ok"])
        self.assertIn("Java", " ".join(report["problems"]))

    def test_unwritable_directory_is_reported(self):
        d = self.make_dir()
        lm = self.manager(d)
        with patch("pathlib.Path.write_text", side_effect=OSError(30, "Read-only file system")):
            report = lm.preflight()
        self.assertFalse(report["ok"])
        self.assertIn("not writable", " ".join(report["problems"]))

    def test_restore_in_progress_blocks_start(self):
        d = self.make_dir()
        (d / "world").mkdir(exist_ok=True)
        (d / "world" / ".rahee-restore-in-progress").write_text("restore")
        lm = self.manager(d)
        report = lm.preflight()
        self.assertFalse(report["ok"])
        self.assertIn("restore", " ".join(report["problems"]).lower())

    def test_missing_world_is_refused_when_the_server_ran_before(self):
        """A lost world must not be silently regenerated over the real backup."""
        d = self.make_dir(ready=True, world=False)  # logs + BOOT_LINE present
        lm = self.manager(d)
        pre = lm.preflight()
        self.assertFalse(pre["ok"])
        self.assertTrue(any("Refusing to start" in p for p in pre["problems"]),
                        str(pre["problems"]))
        self.assertFalse(pre["info"]["world"]["level_dat"])
        with patch("agent.lifecycle.subprocess.Popen") as popen:
            result = lm.start()
        popen.assert_not_called()
        self.assertEqual(result["state"], "failed")

    def test_empty_world_directory_is_refused(self):
        """A world directory with no level.dat is a partial/failed restore."""
        d = self.make_dir(ready=True)
        (d / "world" / "level.dat").unlink()
        pre = self.manager(d).preflight()
        self.assertFalse(pre["ok"])
        self.assertTrue(any("no level.dat" in p for p in pre["problems"]),
                        str(pre["problems"]))

    def test_a_truly_fresh_directory_may_generate_a_world(self):
        """First run on an empty session is legitimate, not a lost world."""
        d = Path(tempfile.mkdtemp(prefix="rahee-lifecycle-fresh-"))
        self.dirs.append(d)
        (d / "paper.jar").write_bytes(b"PK\x03\x04" + b"\x00" * 64)
        (d / "eula.txt").write_text("eula=true\n")
        lm = self.manager(d)
        pre = lm.preflight()
        self.assertTrue(pre["ok"], str(pre["problems"]))
        self.assertFalse(pre["info"]["world"]["level_dat"])

    def test_allow_new_world_opts_in_explicitly(self):
        """Deliberately starting fresh is possible, but on purpose."""
        d = self.make_dir(ready=True, world=False)
        lm = self.manager(d, ALLOW_NEW_WORLD="1")
        self.assertTrue(lm.preflight()["ok"])

    def test_xms_larger_than_xmx_is_reported(self):
        d = self.make_dir()
        lm = self.manager(d, JAVA_XMS="4096", JAVA_XMX="2048")
        report = lm.preflight()
        self.assertFalse(report["ok"])
        self.assertIn("JAVA_XMS", " ".join(report["problems"]))


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------

class TestStart(LifecycleTestBase):

    def test_missing_jar_fails_without_spawning_and_marks_crashed(self):
        d = self.make_dir(jar=False)
        lm = self.manager(d)
        with patch("agent.lifecycle.subprocess.Popen") as popen:
            result = lm.start()
        # No JVM may be spawned: every call is a `java -version` probe, never
        # the server itself.
        for call in popen.call_args_list:
            self.assertNotIn("paper.jar", " ".join(map(str, call.args[0])))
        self.assertEqual(result["state"], "failed")
        self.assertIn("paper.jar", result["error"])
        self.assertEqual(lm.state, ServerState.CRASHED)
        self.assertEqual(lm.desired_state, DesiredState.RUNNING)

    def test_ready_start_reports_real_readiness(self):
        lm, proc, result = self.started()
        self.assertEqual(result["state"], "success")
        self.assertTrue(result["result"]["ready"])
        self.assertEqual(result["result"]["pid"], proc.pid)
        self.assertIn("Done (", result["result"]["ready_marker"])
        self.assertEqual(lm.state, ServerState.RUNNING)
        self.assertTrue(lm.is_ready())

    def test_java_running_but_not_ready_is_not_success(self):
        d = self.make_dir(ready=False)
        lm = self.manager(d, START_TIMEOUT_SECONDS="3", START_SETTLE_SECONDS="1")
        proc = FakeProc(stdout_lines=[BOOT_LINE + "\n"])
        with patch("agent.lifecycle.subprocess.Popen", return_value=proc), \
                patch("agent.lifecycle.find_paper_pid", return_value=None):
            result = lm.start()
        self.assertEqual(result["state"], "timeout")
        self.assertFalse(result["result"]["ready"])
        # Still loading is not RUNNING and not CRASHED.
        self.assertEqual(lm.state, ServerState.STARTING)

    def test_late_readiness_promotes_to_running(self):
        d = self.make_dir(ready=False)
        lm = self.manager(d, START_TIMEOUT_SECONDS="2", START_SETTLE_SECONDS="1")
        proc = FakeProc(stdout_lines=[BOOT_LINE + "\n"])
        with patch("agent.lifecycle.subprocess.Popen", return_value=proc), \
                patch("agent.lifecycle.find_paper_pid", return_value=None):
            lm.start()
        self.assertEqual(lm.state, ServerState.STARTING)
        write_log(d, [BOOT_LINE, READY_LINE], append=True)
        with patch("agent.lifecycle.find_paper_pid", return_value=proc.pid):
            self.assertEqual(lm.supervise(), "promoted-to-running")
        self.assertEqual(lm.state, ServerState.RUNNING)

    def test_immediate_exit_during_startup_is_a_failed_start(self):
        d = self.make_dir()
        lm = self.manager(d, START_TIMEOUT_SECONDS="5", START_SETTLE_SECONDS="1")
        proc = FakeProc(stdout_lines=[], exit_code=1)
        with patch("agent.lifecycle.subprocess.Popen", return_value=proc), \
                patch("agent.lifecycle.find_paper_pid", return_value=None):
            result = lm.start()
        self.assertEqual(result["state"], "failed")
        self.assertIn("exited during startup", result["error"])

    def test_duplicate_start_never_spawns_a_second_process(self):
        lm, proc, _ = self.started()
        with patch("agent.lifecycle.subprocess.Popen") as popen, \
                patch("agent.lifecycle.find_paper_pid", return_value=proc.pid):
            again = lm.start()
        popen.assert_not_called()
        self.assertEqual(again["state"], "success")
        self.assertTrue(again["result"]["already_running"])
        self.assertEqual(again["result"]["pid"], proc.pid)

    def test_preflight_returns_early_when_a_stop_already_arrived(self):
        """A stop that landed before preflight must not cost a JVM probe."""
        d = self.make_dir(ready=True)
        lm = self.manager(d)
        cancel = threading.Event()
        cancel.set()
        with patch.object(ServerLifecycle, "_java_version") as probe:
            pre = lm.preflight(cancel=cancel)
        probe.assert_not_called()
        self.assertFalse(pre["ok"])
        self.assertTrue(pre["cancelled"])
        self.assertIn("cancel", pre["problems"][0])

    def test_stop_during_preflight_cancels_before_spawning(self):
        """A stop arriving in preflight must abort the start, not spawn anyway."""
        d = self.make_dir(ready=True)
        lm = self.manager(d)

        def stop_mid_probe(java, cancel=None, timeout=20):
            # Exactly what a Stop click does while preflight is running: the
            # intent is recorded first, then the in-flight start is cancelled.
            with lm._lock:
                lm._desired = DesiredState.STOPPED
            lm._cancel_start.set()
            return None

        with patch.object(ServerLifecycle, "_java_version",
                          side_effect=stop_mid_probe), \
                patch("agent.lifecycle.subprocess.Popen") as popen:
            result = lm.start()

        popen.assert_not_called()
        self.assertEqual(result["state"], "cancelled")
        self.assertTrue(result["result"]["cancelled"])
        self.assertEqual(lm.state, ServerState.STOPPED)
        self.assertEqual(lm.desired_state, DesiredState.STOPPED)
        self.assertFalse(lm._starting)
        self.assertIsNone(lm.status()["last_error"])

    def test_start_is_refused_while_another_operation_holds_the_gate(self):
        d = self.make_dir(ready=True)
        lm = self.manager(d)
        with patch("agent.lifecycle.subprocess.Popen") as popen:
            lm._op_lock.acquire()
            try:
                result = lm.start()
            finally:
                lm._op_lock.release()
        popen.assert_not_called()
        self.assertEqual(result["state"], "failed")
        self.assertIn("lifecycle operation", result["error"])


# ---------------------------------------------------------------------------
# Stop
# ---------------------------------------------------------------------------

class TestStop(LifecycleTestBase):

    def test_stop_when_idle_is_success_and_sets_intent_stopped(self):
        d = self.make_dir()
        lm = self.manager(d)
        with patch("agent.lifecycle.find_paper_pid", return_value=None):
            result = lm.stop()
        self.assertEqual(result["state"], "success")
        self.assertTrue(result["result"]["already_stopped"])
        self.assertEqual(lm.state, ServerState.STOPPED)
        self.assertEqual(lm.desired_state, DesiredState.STOPPED)

    def test_graceful_stop_sends_console_stop_and_saves(self):
        # The fake JVM exits when Minecraft's own `stop` reaches its console,
        # which is exactly the behaviour the graceful path depends on.
        lm, proc, _ = self.started(
            on_console=lambda line: proc.kill_externally(0) if line == "stop" else None)
        with patch("agent.lifecycle.find_paper_pid", side_effect=self.detector(proc)), \
                patch.object(ServerLifecycle, "_pid_alive",
                             side_effect=lambda pid: proc.poll() is None):
            result = lm.stop()
        self.assertIn("stop", proc.stdin.lines)
        self.assertEqual(result["state"], "success")
        self.assertEqual(result["result"]["method"], "console stop")
        self.assertTrue(result["result"]["saved"])
        self.assertEqual(lm.state, ServerState.STOPPED)

    def test_stop_escalates_sigterm_then_sigkill_and_says_not_saved(self):
        lm, proc, _ = self.started()
        kills = []
        with patch.object(ServerLifecycle, "_wait_termination",
                          side_effect=[False, False, True]), \
                patch("agent.lifecycle.find_paper_pid", return_value=proc.pid), \
                patch("agent.lifecycle.os.kill",
                      side_effect=lambda pid, sig: kills.append(sig)):
            result = lm.stop()
        self.assertEqual(kills, [signal.SIGTERM, signal.SIGKILL])
        self.assertEqual(result["result"]["method"], "SIGKILL")
        self.assertFalse(result["result"]["saved"])
        self.assertIn("warning", result["result"])
        self.assertEqual(lm.state, ServerState.STOPPED)

    def test_stop_that_never_terminates_is_reported_as_timeout(self):
        lm, proc, _ = self.started()
        with patch.object(ServerLifecycle, "_wait_termination", return_value=False), \
                patch("agent.lifecycle.find_paper_pid", return_value=proc.pid), \
                patch("agent.lifecycle.os.kill"):
            result = lm.stop()
        self.assertEqual(result["state"], "timeout")
        self.assertIn("did not terminate", result["error"])
        # A server that is still alive must never be displayed as STOPPED.
        self.assertEqual(lm.state, ServerState.CRASHED)

    def test_stop_during_startup_cancels_the_start(self):
        """A Stop click during a long world-load must actually stop the server.

        The start holds the operation gate for up to START_TIMEOUT_SECONDS.
        Merely refusing the stop would leave a server the user just stopped
        coming up minutes later, so the stop aborts the in-flight start.
        """
        d = self.make_dir(ready=False)
        lm = self.manager(d, START_TIMEOUT_SECONDS="60", START_SETTLE_SECONDS="1",
                          STOP_TIMEOUT_SECONDS="10")
        proc = FakeProc(stdout_lines=[BOOT_LINE + "\n"])  # never becomes ready
        starts = []

        def slow_start():
            with patch("agent.lifecycle.subprocess.Popen", return_value=proc), \
                    patch("agent.lifecycle.find_paper_pid", return_value=None), \
                    patch.object(ServerLifecycle, "_pid_alive", return_value=False):
                starts.append(lm.start())

        t = threading.Thread(target=slow_start, daemon=True)
        t.start()
        # Wait until the JVM exists *and* the start is waiting on readiness:
        # this is the case where a stop must reach Minecraft's console.
        deadline = time.time() + 5
        while time.time() < deadline and not (lm._proc is not None
                                              and lm._op_lock.locked()):
            time.sleep(0.05)
        self.assertIsNotNone(lm._proc, "the start never spawned a JVM")
        self.assertTrue(lm._op_lock.locked(), "the start never claimed the gate")

        began = time.time()
        with patch("agent.lifecycle.find_paper_pid", return_value=None), \
                patch.object(ServerLifecycle, "_pid_alive", return_value=False):
            result = lm.stop()
        waited = time.time() - began
        t.join(10)

        self.assertFalse(t.is_alive(), "the start never returned")
        self.assertEqual(starts[0]["state"], "cancelled")
        self.assertTrue(starts[0]["result"]["cancelled"])
        # Console `stop` reached Minecraft first, so the world still saves.
        self.assertIn("stop", proc.stdin.lines)
        # The stop is a real success, not a refusal, and no process is left.
        self.assertEqual(result["state"], "success")
        self.assertEqual(lm.state, ServerState.STOPPED)
        self.assertEqual(lm.desired_state, DesiredState.STOPPED)
        with patch("agent.lifecycle.find_paper_pid", return_value=None):
            self.assertFalse(lm.is_running())
        # It aborted on the stop request, not by exhausting the start timeout.
        self.assertLess(waited, 10, "stop waited out the start timeout instead")

    def test_stop_with_a_genuinely_stuck_operation_fails_bounded(self):
        """If the gate is held by something that never finishes, say so.

        Stop must not wait forever, and it must not report success for a server
        it never actually reached.
        """
        d = self.make_dir()
        lm = self.manager(d, STOP_TIMEOUT_SECONDS="2")
        lm._op_lock.acquire()
        try:
            began = time.time()
            with patch("agent.lifecycle.find_paper_pid", return_value=None):
                result = lm.stop()
            waited = time.time() - began
        finally:
            lm._op_lock.release()

        self.assertEqual(result["state"], "failed")
        self.assertIn("could not be applied", result["error"])
        self.assertGreaterEqual(waited, 1.5, "stop did not wait for the holder")
        self.assertLess(waited, 15, "stop waited unbounded")
        # The intent is still recorded, so nothing can restart it.
        self.assertEqual(lm.desired_state, DesiredState.STOPPED)

    def test_stop_stops_registered_helpers(self):
        lm, proc, _ = self.started(
            on_console=lambda line: proc.kill_externally(0) if line == "stop" else None)
        stopped = []
        lm.register_helper("playit", lambda: stopped.append("playit"),
                           is_running=lambda: True)
        with patch("agent.lifecycle.find_paper_pid", side_effect=self.detector(proc)), \
                patch.object(ServerLifecycle, "_pid_alive",
                             side_effect=lambda pid: proc.poll() is None):
            result = lm.stop()
        self.assertEqual(stopped, ["playit"])
        self.assertEqual(result["result"]["helpers"], {"playit": "stopped"})
        self.assertEqual(lm.helper_status()["playit"]["running"], True)

    def test_failing_helper_is_reported_not_swallowed(self):
        lm, proc, _ = self.started(
            on_console=lambda line: proc.kill_externally(0) if line == "stop" else None)

        def boom():
            raise RuntimeError("tunnel would not die")

        lm.register_helper("playit", boom)
        with patch("agent.lifecycle.find_paper_pid", side_effect=self.detector(proc)), \
                patch.object(ServerLifecycle, "_pid_alive",
                             side_effect=lambda pid: proc.poll() is None):
            result = lm.stop()
        self.assertIn("stop failed", result["result"]["helpers"]["playit"])


# ---------------------------------------------------------------------------
# Crash detection and bounded recovery
# ---------------------------------------------------------------------------

class TestCrashRecovery(LifecycleTestBase):

    def test_unexpected_exit_is_recorded_and_schedules_one_restart(self):
        lm, proc, _ = self.started()
        proc.kill_externally(1)
        outcome = lm.supervise()
        self.assertEqual(outcome, "restart-scheduled")
        self.assertEqual(lm.state, ServerState.RESTARTING)
        status = lm.status()
        self.assertEqual(status["exit_code"], 1)
        self.assertEqual(status["restart_attempts"], 1)
        self.assertIn("exited unexpectedly", status["last_error"])
        self.assertIsInstance(status["log_tail"], str)

    def test_restart_is_bounded_then_crashed_with_a_reason(self):
        lm, proc, _ = self.started()
        with patch.dict(os.environ, {"MAX_RESTART_ATTEMPTS": "2",
                                     "RESTART_BACKOFF_SECONDS": "0"}):
            outcomes = []
            for _ in range(3):
                # Each restart attempt spawns a JVM that dies again.
                attempt_proc = FakeProc(exit_code=1)
                lm._proc = attempt_proc
                outcomes.append(lm.supervise())
        self.assertEqual(outcomes[:2], ["restart-scheduled"] * 2)
        self.assertEqual(outcomes[2], "restart-exhausted")
        self.assertEqual(lm.state, ServerState.CRASHED)
        self.assertIn("Giving up", lm.status()["last_error"])

    def test_perform_restart_actually_spawns_a_new_process(self):
        """Recovery must start a JVM, not answer "already running".

        `perform_restart()` runs while the state is RESTARTING, so an
        idempotency check based on the state alone would swallow it and the
        server would never come back. Only a start that is genuinely in flight
        may be answered with `already_running`.
        """
        lm, proc, _ = self.started()
        proc.kill_externally(1)
        with patch.dict(os.environ, {"RESTART_BACKOFF_SECONDS": "0",
                                     "MAX_RESTART_ATTEMPTS": "3"}):
            self.assertEqual(lm.supervise(), "restart-scheduled")
            self.assertTrue(lm.restart_due())
            revived = FakeProc(
                stdout_lines=[BOOT_LINE + "\n", READY_LINE + "\n"], pid=777001)
            with patch("agent.lifecycle.subprocess.Popen",
                       return_value=revived) as popen, \
                    patch("agent.lifecycle.find_paper_pid", return_value=None):
                result = lm.perform_restart()

        popen.assert_called_once()
        self.assertEqual(result["state"], "success")
        self.assertEqual(result["result"]["pid"], revived.pid)
        self.assertNotEqual(result["result"]["pid"], proc.pid)
        self.assertFalse(result["result"].get("already_running", False))
        self.assertEqual(lm.state, ServerState.RUNNING)
        self.assertEqual(lm.status()["server_state"], "RUNNING")

    def test_backoff_grows_between_attempts(self):
        lm, proc, _ = self.started()
        with patch.dict(os.environ, {"RESTART_BACKOFF_SECONDS": "10",
                                     "MAX_RESTART_ATTEMPTS": "5"}):
            lm._proc = FakeProc(exit_code=1)
            lm.supervise()
            first = lm._next_restart_at - time.time()
            lm._proc = FakeProc(exit_code=1)
            lm.supervise()
            second = lm._next_restart_at - time.time()
        self.assertGreater(second, first)
        self.assertAlmostEqual(first, 10, delta=1)
        self.assertAlmostEqual(second, 20, delta=1)

    def test_unrecoverable_startup_failure_is_not_retried(self):
        d = self.make_dir(ready=False)
        lm = self.manager(d, MAX_RESTART_ATTEMPTS="3")
        write_log(d, [BOOT_LINE,
                      "Error: UnsupportedClassVersionError: class file version 65.0"],
                 append=True)
        proc = FakeProc(stdout_lines=[], exit_code=1)
        with patch("agent.lifecycle.subprocess.Popen", return_value=proc), \
                patch("agent.lifecycle.find_paper_pid", return_value=None):
            lm.start()
        self.assertEqual(lm.state, ServerState.CRASHED)
        self.assertEqual(lm.status()["restart_attempts"], 0)

    def test_no_restart_after_intentional_stop(self):
        lm, proc, _ = self.started(
            on_console=lambda line: proc.kill_externally(0) if line == "stop" else None)
        with patch("agent.lifecycle.find_paper_pid", side_effect=self.detector(proc)), \
                patch.object(ServerLifecycle, "_pid_alive",
                             side_effect=lambda pid: proc.poll() is None), \
                patch("agent.lifecycle.subprocess.Popen") as popen:
            stop = lm.stop()
            # A second health tick after the stop must never restart it.
            outcome = lm.supervise()
        self.assertEqual(stop["state"], "success")
        popen.assert_not_called()
        self.assertEqual(lm.desired_state, DesiredState.STOPPED)
        self.assertIsNone(outcome)
        self.assertEqual(lm.state, ServerState.STOPPED)
        self.assertEqual(lm.status()["restart_attempts"], 0)

    def test_exit_after_stop_request_is_not_a_crash(self):
        lm, proc, _ = self.started()
        with lm._lock:
            lm._desired = DesiredState.STOPPED
        proc.kill_externally(0)
        outcome = lm.supervise()
        self.assertEqual(outcome, "stopped-by-intent")
        self.assertEqual(lm.state, ServerState.STOPPED)

    def test_perform_restart_refuses_when_intent_is_stopped(self):
        lm, proc, _ = self.started()
        with lm._lock:
            lm._desired = DesiredState.STOPPED
            lm._set_state(ServerState.RESTARTING, "test")
        with patch("agent.lifecycle.subprocess.Popen") as popen:
            result = lm.perform_restart()
        popen.assert_not_called()
        self.assertEqual(result["state"], "failed")
        self.assertIn("cancelled", result["error"])

    def test_perform_restart_refuses_while_previous_process_is_alive(self):
        d = self.make_dir(ready=True)
        lm = self.manager(d, MAX_RESTART_ATTEMPTS="3")
        with lm._lock:
            lm._desired = DesiredState.RUNNING
            lm._set_state(ServerState.RESTARTING, "test")
        with patch("agent.lifecycle.find_paper_pid", return_value=1234), \
                patch("agent.lifecycle.subprocess.Popen") as popen:
            result = lm.perform_restart()
        popen.assert_not_called()
        self.assertEqual(result["state"], "failed")
        self.assertIn("still alive", result["error"])

    def test_crash_restart_keeps_helpers_running(self):
        """A crash recovery must not restart the tunnel (same port)."""
        lm, proc, _ = self.started()
        stopped = []
        lm.register_helper("playit", lambda: stopped.append("playit"))
        proc.kill_externally(1)
        lm.supervise()
        self.assertEqual(stopped, [])

    def test_status_never_says_running_without_a_process(self):
        lm, proc, _ = self.started()
        lm._proc = None
        with patch("agent.lifecycle.find_paper_pid", return_value=None):
            status = lm.status()
        self.assertEqual(status["server_state"], "CRASHED")
        self.assertFalse(status["paper_running"])
        self.assertFalse(status["ready"])

    def test_status_reports_loading_state_from_paper_markers(self):
        d = self.make_dir(ready=False)
        lm = self.manager(d)
        proc = FakeProc(stdout_lines=[BOOT_LINE + "\n"])
        with patch("agent.lifecycle.subprocess.Popen", return_value=proc), \
                patch("agent.lifecycle.find_paper_pid", return_value=proc.pid):
            lm._set_state(ServerState.STARTING, "test")
            status = lm.status()
        self.assertEqual(status["server_state"], "STARTING")
        self.assertEqual(status["paper_startup_state"], "loading")
        self.assertEqual(status["uptime_seconds"], 0)


# ---------------------------------------------------------------------------
# Supervisor plumbing
# ---------------------------------------------------------------------------

class TestSupervisor(LifecycleTestBase):

    def test_supervisor_thread_starts_and_stops_cleanly(self):
        lm = self.manager(self.make_dir(), HEALTH_INTERVAL_SECONDS="0.05")
        with patch("agent.lifecycle.find_paper_pid", return_value=None):
            lm.start_supervisor()
            lm.start_supervisor()  # idempotent: still exactly one thread
            threads = [t for t in threading.enumerate()
                       if t.name == "rahee-lifecycle-supervisor"]
            self.assertEqual(len(threads), 1)
            lm.stop_supervisor()
        self.assertFalse(any(t.is_alive() for t in threads))

    def test_wait_while_running_returns_when_stop_is_requested(self):
        lm = self.manager(self.make_dir(), HEALTH_INTERVAL_SECONDS="0.05")
        with lm._lock:
            lm._desired = DesiredState.RUNNING
        ticks = []

        def stopper():
            time.sleep(0.2)
            with lm._lock:
                lm._desired = DesiredState.STOPPED

        threading.Thread(target=stopper, daemon=True).start()
        result = lm.wait_while_running(poll_interval=0.05,
                                       on_tick=lambda s: ticks.append(s))
        self.assertEqual(result["reason"], "desired_state=STOPPED")
        self.assertTrue(ticks)

    def test_wait_while_running_respects_time_budget(self):
        lm = self.manager(self.make_dir())
        with lm._lock:
            lm._desired = DesiredState.RUNNING
        started = time.time()
        result = lm.wait_while_running(poll_interval=0.05, max_seconds=0.3)
        self.assertLess(time.time() - started, 5)
        self.assertEqual(result["reason"], "time budget reached")

    def test_shutdown_saves_and_stops(self):
        lm, proc, _ = self.started(
            on_console=lambda line: proc.kill_externally(0) if line == "stop" else None)
        with patch("agent.lifecycle.find_paper_pid", side_effect=self.detector(proc)), \
                patch.object(ServerLifecycle, "_pid_alive",
                             side_effect=lambda pid: proc.poll() is None):
            result = lm.shutdown()
        self.assertEqual(result["state"], "success")
        self.assertIn("save-all", proc.stdin.lines)
        self.assertIn("stop", proc.stdin.lines)
        self.assertEqual(lm.state, ServerState.STOPPED)

    def test_concurrent_supervise_calls_schedule_one_restart(self):
        lm, proc, _ = self.started()
        proc.kill_externally(1)
        outcomes = []
        with patch.dict(os.environ, {"RESTART_BACKOFF_SECONDS": "5"}):
            threads = [threading.Thread(target=lambda: outcomes.append(lm.supervise()))
                       for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
        scheduled = [o for o in outcomes if o == "restart-scheduled"]
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(lm.status()["restart_attempts"], 1)


# ---------------------------------------------------------------------------
# Console log bounding
# ---------------------------------------------------------------------------

class TestConsoleLog(LifecycleTestBase):

    def test_console_log_rotates_and_stays_bounded(self):
        d = self.make_dir()
        writer = ConsoleLogWriter(d / "logs" / "agent-console.log", 64 * 1024)
        writer.open()
        try:
            line = "x" * 900
            for _ in range(200):  # ~176 KiB of output
                writer.write(line)
        finally:
            writer.close()
        main = d / "logs" / "agent-console.log"
        rotated = d / "logs" / "agent-console.log.1"
        self.assertTrue(main.exists())
        self.assertTrue(rotated.exists())
        self.assertLess(main.stat().st_size, 64 * 1024 + 4096)
        self.assertTrue(writer.tail_text())

    def test_writer_survives_a_vanished_file(self):
        d = self.make_dir()
        path = d / "logs" / "agent-console.log"
        writer = ConsoleLogWriter(path, 64 * 1024)
        writer.open()
        path.unlink()
        try:
            # Enough output to force a rotation while the file is missing.
            for _ in range(100):
                writer.write("y" * 900)
        finally:
            writer.close()
        self.assertTrue(path.exists())


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistry(LifecycleTestBase):

    def test_get_lifecycle_is_one_manager_for_the_configured_dir(self):
        from agent.config import config
        a = get_lifecycle()
        b = get_lifecycle(None)
        self.assertIs(a, b)
        # A different directory gets its own manager, and asking twice returns
        # the same one (no duplicate supervisors per server directory).
        other = self.make_dir()
        m1 = get_lifecycle(other)
        m2 = get_lifecycle(other)
        self.assertIs(m1, m2)
        self.assertNotEqual(str(config.SERVER_DIR), str(other))


# ---------------------------------------------------------------------------
# Backend: the panel must show the agent's state, not a backend-host guess
# ---------------------------------------------------------------------------

class TestStatusReportsAgentLifecycle(unittest.TestCase):
    """`/api/status` must prefer the agent heartbeat over local process scans."""

    @classmethod
    def setUpClass(cls):
        from backend.tests.test_stage2 import login_cookies
        cls.cookies = login_cookies()

    def _status(self):
        from backend.tests.test_stage2 import _call_app
        code, body = _call_app("/api/status", cookies=self.cookies)
        self.assertEqual(code, 200)
        return json.loads(body) if isinstance(body, (str, bytes)) else body

    def test_status_reflects_reported_running_state(self):
        from backend.app import agent_handler
        agent_handler.register_agent("agent-lifecycle-test", "secret-lifecycle")
        agent_handler.heartbeat("agent-lifecycle-test", "secret-lifecycle", {
            "paper_running": True,
            "paper_pid": 4321,
            "server_state": "RUNNING",
            "desired_state": "RUNNING",
            "paper_startup_state": "ready",
        })
        self.addCleanup(agent_handler.agents.pop, "agent-lifecycle-test", None)

        data = self._status()
        self.assertEqual(data["server"]["server_state"], "RUNNING")
        self.assertTrue(data["server"]["paper_running"])
        self.assertEqual(data["server"]["paper_pid"], 4321)
        self.assertEqual(data["server"]["source"], "agent")

    def test_status_reflects_crashed_state_and_reason(self):
        from backend.app import agent_handler
        agent_handler.register_agent("agent-crash-test", "secret-crash")
        agent_handler.heartbeat("agent-crash-test", "secret-crash", {
            "paper_running": False,
            "server_state": "CRASHED",
            "desired_state": "RUNNING",
            "restart_attempts": 3,
            "exit_code": 1,
            "last_error": "Giving up after 3 restart attempts",
        })
        self.addCleanup(agent_handler.agents.pop, "agent-crash-test", None)

        data = self._status()
        self.assertEqual(data["server"]["server_state"], "CRASHED")
        self.assertFalse(data["server"]["paper_running"])
        self.assertIn("Giving up", data["server"]["last_error"])
        self.assertEqual(data["server"]["restart_attempts"], 3)
        self.assertEqual(data["server"]["exit_code"], 1)

    def test_unknown_state_from_an_agent_is_not_rendered_raw(self):
        from backend.app import agent_handler
        agent_handler.register_agent("agent-junk-test", "secret-junk")
        agent_handler.heartbeat("agent-junk-test", "secret-junk", {
            "paper_running": True,
            "server_state": "<script>alert(1)</script>",
        })
        self.addCleanup(agent_handler.agents.pop, "agent-junk-test", None)
        data = self._status()
        self.assertIn(data["server"]["server_state"],
                      ("RUNNING", "STOPPED", "STARTING", "STOPPING",
                       "CRASHED", "RESTARTING"))

    def test_status_requires_authentication(self):
        from backend.tests.test_stage2 import _call_app
        code, _ = _call_app("/api/status")
        self.assertEqual(code, 401)


class TestLifecycleEndpointAuth(unittest.TestCase):
    """Power endpoints must stay behind admin auth (no anonymous shutdown)."""

    @classmethod
    def setUpClass(cls):
        from backend.tests.test_stage2 import login_cookies
        cls.cookies = login_cookies()

    def test_power_endpoints_require_auth(self):
        from backend.tests.test_stage2 import _call_app
        for path in ("/api/admin/server/start", "/api/admin/server/stop",
                     "/api/admin/server/restart"):
            code, _ = _call_app(path, "POST", "{}")
            self.assertEqual(code, 401, path)

    def test_stop_endpoint_queues_for_the_online_agent(self):
        from backend.tests.test_stage2 import _call_app
        from backend.app import agent_handler, command_handler
        agent_handler.register_agent("agent-stop-test", "secret-stop")
        agent_handler.heartbeat("agent-stop-test", "secret-stop",
                                {"paper_running": True, "server_state": "RUNNING",
                                 "desired_state": "RUNNING"})
        self.addCleanup(agent_handler.agents.pop, "agent-stop-test", None)

        code, body = _call_app("/api/admin/server/stop", "POST", "{}",
                               cookies=self.cookies)
        self.assertEqual(code, 200)
        data = json.loads(body) if isinstance(body, (str, bytes)) else body
        self.assertEqual(data["status"], "queued")
        action = command_handler.get_action(data["action_id"])
        self.assertEqual(action["action_type"], "stop_server")
        # The action is queued, NOT reported as a completed stop.
        self.assertIn(action["state"], ("pending", "running"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
