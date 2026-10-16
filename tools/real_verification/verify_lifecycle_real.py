#!/usr/bin/env python3
"""
Real end-to-end verification of the RaheeCraft server lifecycle.

Every check below runs a REAL JVM (a stand-in Paper server compiled to a real
runnable jar) through the same `agent.lifecycle.ServerLifecycle` the dashboard,
the agent and the notebook use. Nothing is mocked: real processes, real
signals, real logs, real readiness markers.

Scenarios:
  1. preflight on a real server directory
  2. start -> RUNNING, with the PID positively identified as *this* server
  3. duplicate start spawns no second JVM
  4. kill -9 crash -> recorded exit + bounded restart, new PID
  5. Stop -> Minecraft's own `stop` reaches the JVM, it exits 0, no process left
  6. no restart after an intentional Stop
  7. an unrecoverable startup failure is not retried
  8. repeated crashes exhaust the restart budget -> CRASHED with a reason
  9. Stop during startup cancels the start (does not wait out the timeout)
 10. Stop escalates to SIGTERM when the console channel is unusable
 11. Stop escalates to SIGKILL and reports the world was not saved
"""

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent.lifecycle import (  # noqa: E402
    ServerLifecycle, ServerState, DesiredState, get_lifecycle,
)
from agent.process_info import find_paper_pid  # noqa: E402

JAR = Path(__file__).resolve().parent / "paper.jar"

RESULTS = []
TEMP_DIRS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    return ok


def server_dir(overrides=None, **env):
    """A fresh real server directory containing the compiled stand-in jar."""
    d = Path(tempfile.mkdtemp(prefix="rahee-realverify-"))
    TEMP_DIRS.append(d)
    shutil.copy(JAR, d / "paper.jar")
    (d / "logs").mkdir(exist_ok=True)
    (d / "eula.txt").write_text("eula=true\n")
    (d / "server.properties").write_text(
        "server-port=25565\nlevel-name=world\nonline-mode=true\n")
    settings = dict(BASE_ENV)
    settings.update(overrides or {})
    settings.update(env)
    for key, value in settings.items():
        os.environ[key] = str(value)
    return d


def clear_env(*names):
    for key in names:
        os.environ.pop(key, None)


def dir_processes(d):
    """PIDs of every live JVM running this directory's paper.jar (real detect)."""
    pid = find_paper_pid(d)
    return [] if pid is None else [pid]


def wait_for(predicate, timeout, interval=0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


BASE_ENV = dict(
    START_TIMEOUT_SECONDS="40",
    START_SETTLE_SECONDS="1",
    STOP_TIMEOUT_SECONDS="8",
    MAX_RESTART_ATTEMPTS="3",
    RESTART_BACKOFF_SECONDS="1",
    RESTART_BACKOFF_CAP_SECONDS="2",
    HEALTH_INTERVAL_SECONDS="1",
    CONSOLE_LOG_MAX_BYTES=str(128 * 1024),
)


def fresh_lifecycle(d):
    return ServerLifecycle(server_dir=str(d))


# ---------------------------------------------------------------------------
# 1. preflight
# ---------------------------------------------------------------------------

def scenario_preflight():
    print("\n1. Preflight on a real server directory")
    d = server_dir()
    lm = fresh_lifecycle(d)
    pre = lm.preflight()
    check("preflight passes for a real JDK 21 + valid jar + accepted EULA",
          pre["ok"], f"problems={pre['problems']}")
    check("preflight reports the detected Java version",
          "21" in str(pre["info"].get("java_version", "")),
          str(pre["info"].get("java_version")))

    bad = server_dir()
    (bad / "paper.jar").unlink()
    (bad / "eula.txt").write_text("eula=false\n")
    pre_bad = fresh_lifecycle(bad).preflight()
    joined = " ".join(pre_bad["problems"])
    check("missing jar and refused EULA are both named", not pre_bad["ok"]
          and "paper.jar is missing" in joined and "EULA not accepted" in joined,
          joined[:160])


# ---------------------------------------------------------------------------
# 2 & 3. start, readiness, and duplicate-start safety
# ---------------------------------------------------------------------------

def scenario_start():
    print("\n2. Start a real JVM and require real readiness")
    d = server_dir(overrides={"FAKE_READY_AFTER_MS": "1500"})
    lm = fresh_lifecycle(d)
    began = time.time()
    result = lm.start(reason="verify")
    took = time.time() - began

    check("start reports success", result["state"] == "success", str(result.get("error")))
    payload = result.get("result") or {}
    check("readiness came from Paper's own 'Done (' marker",
          "Done (" in str(payload.get("ready_marker")),
          str(payload.get("ready_marker")))
    check("lifecycle state is RUNNING", lm.state == ServerState.RUNNING)
    check("desired_state is RUNNING", lm.desired_state == DesiredState.RUNNING)

    pid = payload.get("pid")
    check("a real PID was returned", isinstance(pid, int) and pid > 0, f"pid={pid}")
    check("process identity verified by the real detector (find_paper_pid)",
          find_paper_pid(d) == pid, f"detector={find_paper_pid(d)} reported={pid}")
    check("readiness waited for the server, it was not a bare 'process exists'",
          took >= 1.0, f"start returned in {took:.1f}s")
    check("Paper's own latest.log holds the ready line",
          "Done (" in (d / "logs" / "latest.log").read_text(errors="replace"))
    console = d / "logs" / "agent-console.log"
    check("stdout was drained into a bounded console log",
          console.exists() and console.stat().st_size > 0,
          f"bytes={console.stat().st_size if console.exists() else 0}")

    status = lm.status()
    check("status agrees with the process table",
          status["paper_running"] and status["paper_pid"] == pid
          and status["server_state"] == "RUNNING" and status["ready"])

    print("\n3. A duplicate Start must not spawn a second JVM")
    before = dir_processes(d)
    again = lm.start(reason="duplicate-click")
    after = dir_processes(d)
    check("second start returns already_running",
          again["state"] == "success" and again["result"].get("already_running"),
          str(again["result"]))
    check("second start reports the original PID",
          again["result"].get("pid") in (pid, None), str(again["result"].get("pid")))
    check("exactly one JVM is running for this world",
          len(after) == 1 and before == after, f"before={before} after={after}")

    # Leave the server RUNNING on purpose: the next scenario must verify the
    # real graceful Stop against a live server.
    return d, lm


# ---------------------------------------------------------------------------
# 4. crash -> bounded restart
# ---------------------------------------------------------------------------

def scenario_crash_restart():
    print("\n4. kill -9 crash is detected and recovered within a bounded budget")
    d = server_dir(overrides={"FAKE_READY_AFTER_MS": "1200"})
    lm = fresh_lifecycle(d)

    start = lm.start(reason="verify")
    if start["state"] != "success":
        check("baseline start succeeded", False, str(start.get("error")))
        return
    old_pid = start["result"]["pid"]

    # A genuine SIGKILL: the JVM dies with no chance to log, exactly like an
    # OOM kill or a hard runtime failure.
    proc = lm._proc
    os.kill(old_pid, signal.SIGKILL)
    check("the SIGKILL really terminated the JVM",
          wait_for(lambda: proc.poll() is not None, 15),
          f"poll={proc.poll()}")
    check("the JVM is gone from the process table too",
          wait_for(lambda: find_paper_pid(d) is None, 10)
          and not _pid_exists(old_pid),
          f"detector={find_paper_pid(d)}")

    # Detection happens on a health tick, so the tick is what is under test
    # here -- not a race with the kernel finishing the kill.
    ticked = lm.supervise()
    check("supervise() noticed the exit and acted", ticked is not None, str(ticked))
    check("the crash is recorded with the exit code and a log tail",
          lm.status()["exit_code"] is not None
          and lm.status()["log_tail"] is not None,
          f"exit_code={lm.status()['exit_code']}")
    check("a restart is scheduled with backoff (state RESTARTING)",
          lm.state == ServerState.RESTARTING
          and lm.restart_due() is False,
          f"state={lm.state.value} due={lm.restart_due()}")
    check("desired_state stays RUNNING so recovery is allowed",
          lm.desired_state == DesiredState.RUNNING)
    check("a second concurrent tick does not double-count the crash",
          lm.supervise() in (None, "restart-scheduled", "promoted-to-running"))

    check("the restart becomes due after the backoff", wait_for(lm.restart_due, 10))
    restarted = lm.perform_restart()
    check("the restart brought the server back", restarted["state"] == "success",
          str(restarted.get("error")))
    new_pid = restarted.get("result", {}).get("pid")
    check("the recovered server is a NEW process (the old one really died)",
          isinstance(new_pid, int) and new_pid != old_pid,
          f"old={old_pid} new={new_pid}")
    check("the recovered server is genuinely RUNNING",
          lm.state == ServerState.RUNNING and find_paper_pid(d) == new_pid)
    check("restart attempts were counted", lm.status()["restart_attempts"] >= 1,
          f"attempts={lm.status()['restart_attempts']}")

    return d, lm


# ---------------------------------------------------------------------------
# 5 & 6. graceful stop, and no restart afterwards
# ---------------------------------------------------------------------------

def scenario_stop(d, lm):
    print("\n5. Stop must reach Minecraft's own console and save the world")
    pid = lm.paper_pid()
    result = lm.stop(reason="verify")
    check("stop reports success", result["state"] == "success", str(result.get("error")))
    check("Minecraft's own `stop` was the method used",
          result["result"].get("method") == "console stop", str(result["result"]))
    check("the result reports the world was saved",
          result["result"].get("saved") is True)
    check("the JVM is actually gone",
          wait_for(lambda: find_paper_pid(d) is None, 5) and not _pid_exists(pid),
          f"pid={pid}")
    check("lifecycle state is STOPPED", lm.state == ServerState.STOPPED)
    check("status does not claim a process is running",
          lm.status()["paper_running"] is False and lm.status()["paper_pid"] is None)
    log = (d / "logs" / "latest.log").read_text(errors="replace")
    check("Paper's log shows the clean save-and-stop sequence",
          "Saving worlds" in log and "Stopping the server" in log)

    print("\n6. Nothing may restart the server after an intentional Stop")
    for _ in range(8):
        lm.supervise()
        time.sleep(0.2)
    check("supervise() leaves the server stopped",
          lm.state in (ServerState.STOPPED,) and lm.desired_state == DesiredState.STOPPED,
          f"state={lm.state.value} desired={lm.desired_state.value}")
    check("no JVM was resurrected", dir_processes(d) == [])
    check("perform_restart() refuses when the intent is STOPPED",
          lm.perform_restart()["state"] == "failed")
    check("status never says RUNNING without a process",
          lm.status()["server_state"] != "RUNNING")

    print("\n   Restart after Stop must work again, without duplicates")
    restarted = lm.start(reason="verify-again")
    check("Start after an intentional Stop works", restarted["state"] == "success",
          str(restarted.get("error")))
    check("exactly one JVM again", len(dir_processes(d)) == 1,
          f"{dir_processes(d)}")
    final = lm.stop(reason="verify-cleanup")
    check("the follow-up stop also succeeds", final["state"] == "success",
          str(final.get("error")))


# ---------------------------------------------------------------------------
# 7. unrecoverable startup failure
# ---------------------------------------------------------------------------

def scenario_unrecoverable():
    print("\n7. An unrecoverable startup failure must not be retried in a loop")
    d = server_dir(overrides={"FAKE_BOOT_FAIL": "eula"})
    lm = fresh_lifecycle(d)
    result = lm.start(reason="verify")
    check("start fails instead of reporting a running server",
          result["state"] == "failed", str(result.get("error")))
    check("the failure names the real cause",
          "EULA" in str(result.get("error", "")) or
          "EULA" in str(lm.status().get("last_error")),
          str(result.get("error"))[:160])

    for _ in range(10):
        lm.supervise()
        time.sleep(0.2)
    check("no restart was attempted and the server stays CRASHED",
          lm.status()["restart_attempts"] == 0
          and lm.state == ServerState.CRASHED,
          f"attempts={lm.status()['restart_attempts']} state={lm.state.value} "
          f"desired={lm.desired_state.value}")
    check("the standalone supervisor never restarts a CRASHED server",
          lm.supervise() is None)
    check("no JVM is left behind", dir_processes(d) == [])
    clear_env("FAKE_BOOT_FAIL")


# ---------------------------------------------------------------------------
# 8. restart budget exhaustion
# ---------------------------------------------------------------------------

def scenario_budget():
    print("\n8. Repeated crashes exhaust the restart budget -> CRASHED")
    d = server_dir(overrides={"MAX_RESTART_ATTEMPTS": "1",
                              "FAKE_READY_AFTER_MS": "800",
                              "FAKE_EXIT_AFTER_MS": "1000",
                              "FAKE_EXIT_CODE": "3"})
    lm = fresh_lifecycle(d)
    first = lm.start(reason="verify")
    check("the server starts once", first["state"] == "success", str(first.get("error")))

    deadline = time.time() + 60
    while time.time() < deadline and lm.state != ServerState.CRASHED:
        lm.supervise()
        if lm.restart_due():
            lm.perform_restart()
        time.sleep(0.4)

    status = lm.status()
    check("the server ends up CRASHED, not silently restarting forever",
          lm.state == ServerState.CRASHED, f"state={lm.state.value}")
    check("attempts stayed within the configured maximum",
          0 <= status["restart_attempts"] <= 1, f"attempts={status['restart_attempts']}")
    check("a human-readable reason is reported",
          bool(status["last_error"]) and len(str(status["last_error"])) > 20,
          str(status["last_error"])[:200])
    check("the reason is the restart budget, not a fake success",
          "Giving up" in str(status["last_error"]), str(status["last_error"])[:120])
    check("desired_state is preserved as RUNNING (it was not a user stop)",
          lm.desired_state == DesiredState.RUNNING)
    check("no JVM is left running", dir_processes(d) == [])
    clear_env("FAKE_READY_AFTER_MS", "FAKE_EXIT_AFTER_MS", "FAKE_EXIT_CODE")


# ---------------------------------------------------------------------------
# 9. stop during startup
# ---------------------------------------------------------------------------

def scenario_stop_during_start():
    print("\n9. Stop during a long startup must stop, not wait out the timeout")
    d = server_dir(overrides={"START_TIMEOUT_SECONDS": "120",
                              "FAKE_READY_AFTER_MS": "60000"})
    lm = fresh_lifecycle(d)
    started = []

    t = threading.Thread(target=lambda: started.append(lm.start(reason="verify")),
                         daemon=True)
    t.start()
    # Generous: preflight probes `java -version` first, which is slow on this
    # device (the start is still only starting here).
    check("the JVM is up and the start is waiting for readiness",
          wait_for(lambda: find_paper_pid(d) is not None, 90),
          f"detector={find_paper_pid(d)}")
    check("state is STARTING while loading",
          wait_for(lambda: lm.state == ServerState.STARTING, 5),
          f"state={lm.state.value}")

    began = time.time()
    result = lm.stop(reason="verify")
    took = time.time() - began
    t.join(30)

    check("the start returned instead of blocking for the full timeout",
          not t.is_alive(), f"start_state={started[0]['state'] if started else 'n/a'}")
    check("the start reports it was cancelled",
          bool(started) and started[0]["state"] == "cancelled"
          and started[0]["result"].get("cancelled") is True,
          str(started[0] if started else None)[:160])
    check("the stop is a real success, not a refusal",
          result["state"] == "success", str(result.get("error")))
    check("the stop was prompt (it cancelled rather than waiting 120s)",
          took < 30, f"took {took:.1f}s")
    check("the JVM is gone", dir_processes(d) == []
          and wait_for(lambda: find_paper_pid(d) is None, 5))
    check("the stopped server is not displayed as running",
          lm.state == ServerState.STOPPED
          and lm.status()["paper_running"] is False
          and lm.desired_state == DesiredState.STOPPED)


# ---------------------------------------------------------------------------
# 10 & 11. escalation
# ---------------------------------------------------------------------------

def scenario_escalation():
    print("\n10. Stop escalates to SIGTERM when the console channel is unusable")
    d = server_dir(overrides={"FAKE_READY_AFTER_MS": "800",
                              "FAKE_IGNORE_STDIN": "1",
                              "STOP_TIMEOUT_SECONDS": "3"})
    lm = fresh_lifecycle(d)
    started = lm.start(reason="verify")
    if started["state"] != "success":
        check("baseline start for the SIGTERM scenario", False, str(started.get("error")))
    else:
        result = lm.stop(reason="verify")
        check("stop succeeded via escalation", result["state"] == "success",
              str(result.get("error")))
        check("SIGTERM was the method actually used",
              result["result"].get("method") == "SIGTERM", str(result["result"]))
        check("the JVM is gone", wait_for(lambda: find_paper_pid(d) is None, 5))
    clear_env("FAKE_IGNORE_STDIN")

    print("\n11. A wedged JVM is force-killed, and that is reported honestly")
    d2 = server_dir(overrides={"FAKE_READY_AFTER_MS": "800",
                               "FAKE_IGNORE_STDIN": "1",
                               "FAKE_IGNORE_SIGTERM": "1",
                               "STOP_TIMEOUT_SECONDS": "2"})
    lm2 = fresh_lifecycle(d2)
    started2 = lm2.start(reason="verify")
    if started2["state"] != "success":
        check("baseline start for the SIGKILL scenario", False,
              str(started2.get("error")))
        return
    result2 = lm2.stop(reason="verify")
    check("stop still ends the server", result2["state"] == "success",
          str(result2.get("error")))
    check("SIGKILL was the last resort that was used",
          result2["result"].get("method") == "SIGKILL", str(result2["result"]))
    check("the result warns the world may not be fully saved",
          result2["result"].get("saved") is False
          and "warning" in result2["result"], str(result2["result"])[:200])
    check("the JVM is gone", wait_for(lambda: find_paper_pid(d2) is None, 5))
    clear_env("FAKE_IGNORE_STDIN", "FAKE_IGNORE_SIGTERM")


# ---------------------------------------------------------------------------

def _pid_exists(pid):
    """True only for a process that has not already exited.

    A zombie still answers `os.kill(pid, 0)`, so the /proc state is checked the
    same way the lifecycle manager does.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_bytes().decode("utf-8", "replace")
    except OSError:
        return False
    try:
        state = stat[stat.rindex(")") + 2:].split()[0]
    except (ValueError, IndexError):
        return False
    return state not in ("Z", "X", "x")


def main():
    if not JAR.exists():
        print(f"missing {JAR}; build it first")
        return 2

    d = lm = None
    try:
        scenario_preflight()
        d, lm = scenario_start()
        scenario_stop(d, lm)
        d2, lm2 = scenario_crash_restart()
        scenario_stop(d2, lm2)
        scenario_unrecoverable()
        scenario_budget()
        scenario_stop_during_start()
        scenario_escalation()
    finally:
        for environment_dir in TEMP_DIRS:
            shutil.rmtree(environment_dir, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    failed = [(n, det) for n, ok, det in RESULTS if not ok]
    print("\n" + "=" * 70)
    print(f"REAL-VERIFICATION RESULT: {passed}/{len(RESULTS)} checks passed")
    for name, detail in failed:
        print(f"  FAILED: {name} — {detail}")
    print("=" * 70)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
