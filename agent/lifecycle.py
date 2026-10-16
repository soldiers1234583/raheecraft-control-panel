#!/usr/bin/env python3
"""
RaheeCraft — Authoritative server lifecycle manager
===================================================
ONE component owns the Minecraft process. The notebook, the control agent and
the dashboard all observe *this* object; none of them starts a competing JVM.

Why this module exists
----------------------
Previously the notebook started Paper with its own `subprocess.Popen`, the agent
started another one through `ActionExecutor`, and "is it running?" was answered
by `pgrep`. That produced three classes of failure:

  * two Paper processes could open the same world directory;
  * a start that died at any point left no record, so nothing could recover;
  * nothing distinguished "java is alive" from "Minecraft is ready", and
    nothing at all distinguished "a request finished" from "the server
    crashed".

The state machine here is explicit and observable:

      STOPPED ──start──▶ STARTING ──ready──▶ RUNNING ──stop──▶ STOPPING ──▶ STOPPED
          ▲                  │                  │
          │             (fatal / timeout)   (unexpected exit)
          │                  ▼                  ▼
          └──────────────── CRASHED ◀── RESTARTING ──(bounded backoff)──┐
                                                                        │
                                   desired_state == RUNNING ─────────────┘

Guarantees
----------
  * `desired_state` is recorded BEFORE any shutdown begins, so the supervisor
    can never resurrect a server the user deliberately stopped.
  * Every transition happens under one lock; a second concurrent start/stop is
    refused (and reported as such) instead of spawning a duplicate JVM.
  * Readiness comes from Paper's own `Done (…)` line, never from "the process
    exists".
  * Crash recovery is bounded: recorded exit code, a log tail, exponential
    backoff, a maximum attempt count, then `CRASHED` with a reason a human can
    act on. Unrecoverable startup failures (bad jar, wrong Java, EULA) are not
    retried at all.
  * Server stdout is drained by a reader thread into a size-bounded, rotating
    file. (An undrained `stdout=PIPE` fills the 64 KiB pipe buffer and freezes
    the JVM — that was one of the original startup deadlocks.)
  * Intentional Stop also stops the helpers registered against it (the Playit
    tunnel), while a crash-restart leaves them alone, since Paper comes back on
    the same port.

Platform honesty
----------------
This cannot outlive the Kaggle runtime. If the session is terminated, the JVM
dies with the kernel and nothing here can prevent it; `atexit` makes a
best-effort save-and-stop so the world is not lost, and that is all that is
claimed.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.config import config
from agent.process_info import (
    FATAL_MARKERS,
    READY_MARKERS,
    find_paper_pid,
    paper_startup_state,
)

__all__ = [
    "ServerState",
    "DesiredState",
    "ServerLifecycle",
    "lifecycle",
    "LIFECYCLE_STATES",
]


class ServerState(str, Enum):
    """Observed state of the Minecraft process."""

    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    CRASHED = "CRASHED"
    RESTARTING = "RESTARTING"


class DesiredState(str, Enum):
    """What the human asked for. Independent of what is happening right now."""

    RUNNING = "RUNNING"
    STOPPED = "STOPPED"


LIFECYCLE_STATES = tuple(s.value for s in ServerState)

# Keep the tail of the console in memory so a crash report can quote it without
# re-reading (and without guessing at) the log file.
_LOG_TAIL_LINES = 40
_MAX_EVENT_HISTORY = 60


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    """Read an int from the environment; junk falls back, small values clamp.

    Clamping (rather than falling back to the default) matters: a caller that
    asks for a 3-second readiness window must get 3 seconds, not the 300-second
    default, or a startup check silently blocks a test or a notebook for
    minutes.
    """
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return max(minimum, default)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return max(minimum, default)
    return max(minimum, value)


def _env_flag(name: str, default: bool = False) -> bool:
    """Read a boolean from the environment."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_float(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return max(minimum, default)
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return max(minimum, default)
    return max(minimum, value)


class ConsoleLogWriter:
    """Size-bounded, rotating writer for the drained server stdout.

    The file is rotated (`.1` generation kept) instead of growing without
    limit for the whole session, and every write keeps a small in-memory tail
    for crash reporting.
    """

    def __init__(self, path: Path, max_bytes: int):
        self.path = Path(path)
        self.max_bytes = max(64 * 1024, int(max_bytes))
        self._handle = None
        self._size = 0
        self.tail: deque = deque(maxlen=_LOG_TAIL_LINES)

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_if_needed(force=False)
        self._size = self.path.stat().st_size if self.path.exists() else 0
        # Line-buffered so a crash still leaves the last lines on disk.
        self._handle = open(self.path, "a", encoding="utf-8", errors="replace",
                            buffering=1)

    def _rotate_if_needed(self, force: bool) -> bool:
        """Roll the file over once when it exceeds the cap."""
        try:
            if not self.path.exists():
                return False
            if not force and self.path.stat().st_size < self.max_bytes:
                return False
            rotated = self.path.with_suffix(self.path.suffix + ".1")
            try:
                if rotated.exists():
                    rotated.unlink()
                self.path.rename(rotated)
            except OSError:
                # Rotation failure must never take the server down; truncate
                # instead so the file still cannot grow without bound.
                with open(self.path, "w"):
                    pass
            return True
        except OSError:
            return False

    def write(self, line: str) -> None:
        text = line if line.endswith("\n") else line + "\n"
        self.tail.append(line.rstrip("\n"))
        if self._handle is None:
            return
        try:
            if self._size >= self.max_bytes:
                self._handle.close()
                self._rotate_if_needed(force=True)
                self._handle = open(self.path, "a", encoding="utf-8",
                                    errors="replace", buffering=1)
                self._size = self.path.stat().st_size if self.path.exists() else 0
            self._handle.write(text)
            self._size += len(text.encode("utf-8", "replace"))
            self._handle.flush()
        except (OSError, ValueError):
            # A vanished/replaced file must not kill the reader thread.
            self._handle = None

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            except Exception:
                pass
            self._handle = None

    def tail_text(self) -> str:
        return "\n".join(self.tail)


class ServerLifecycle:
    """Owns the Paper process, its state machine, and its recovery policy."""

    def __init__(self, server_dir: Optional[Any] = None) -> None:
        self._server_dir_override = Path(server_dir) if server_dir else None
        # Short-lived state guard. Never held across a sleep or a blocking wait.
        self._lock = threading.RLock()
        # Operation gate: held for the duration of a start/stop/restart so two
        # lifecycle operations can never interleave.
        self._op_lock = threading.Lock()
        # Health-tick gate: several callers (agent loop, notebook supervisor)
        # may tick concurrently; only one may be handling an exit at a time or
        # the same crash would be counted twice.
        self._tick_lock = threading.Lock()

        self._state = ServerState.STOPPED
        self._desired = DesiredState.STOPPED
        # True only while a start is genuinely in flight (spawning/waiting for
        # readiness). This is what makes Start idempotent. It is deliberately
        # NOT derived from `server_state == STARTING`: a crash-recovery restart
        # legitimately starts from the RESTARTING state and must not be answered
        # with "already running" instead of spawning a JVM.
        self._starting = False
        self._proc: Optional[subprocess.Popen] = None
        self._owns_process = False
        self._log: Optional[ConsoleLogWriter] = None
        self._reader: Optional[threading.Thread] = None

        self._started_at: Optional[float] = None
        self._ready_at: Optional[float] = None
        self._stopped_at: Optional[float] = None
        self._ready_marker: Optional[str] = None
        self._startup_state = "unknown"

        self._exit_code: Optional[int] = None
        self._last_error: Optional[str] = None
        self._last_log_tail: Optional[str] = None
        self._last_exit_at: Optional[float] = None
        self._restart_attempts = 0
        self._next_restart_at: Optional[float] = None
        self._crashed_reason: Optional[str] = None

        self._events: deque = deque(maxlen=_MAX_EVENT_HISTORY)
        self._helpers: Dict[str, Dict[str, Any]] = {}
        self._supervisor_thread: Optional[threading.Thread] = None
        self._supervisor_stop = threading.Event()
        self._supervisor_on_event: Optional[Callable[[str, Dict[str, Any]], None]] = None
        # Set by a stop request so an in-flight *start* aborts instead of
        # holding the operation gate (and refusing the stop) until its readiness
        # timeout expires. World generation can take minutes on Kaggle, and a
        # Stop click during it must still stop the server.
        self._cancel_start = threading.Event()

    # ------------------------------------------------------------------ paths

    @property
    def server_dir(self) -> Path:
        """Always re-read the configured directory (never cached at import)."""
        return self._server_dir_override or Path(config.SERVER_DIR)

    @property
    def server_jar(self) -> Path:
        return self.server_dir / "paper.jar"

    @property
    def log_path(self) -> Path:
        """Paper's own log (written by Paper itself, used for readiness)."""
        if self._server_dir_override:
            return self.server_dir / "logs" / "latest.log"
        return Path(config.get_log_path())

    @property
    def console_path(self) -> Path:
        """Our drained copy of the server's stdout."""
        return self.server_dir / "logs" / "agent-console.log"

    # ------------------------------------------------------------- tuning knobs

    def start_timeout(self) -> int:
        return _env_int("START_TIMEOUT_SECONDS", 300, minimum=5)

    def settle_timeout(self) -> int:
        """How long the JVM must merely survive before readiness is waited on.

        A broken jar, wrong Java version or refused EULA kills the process
        within seconds; this is the window that turns that into a clean failed
        start instead of a misleading "still loading".
        """
        return _env_int("START_SETTLE_SECONDS",
                        min(self.start_timeout(), 10), minimum=1)

    def stop_timeout(self) -> int:
        return _env_int("STOP_TIMEOUT_SECONDS", 180, minimum=5)

    def max_restart_attempts(self) -> int:
        return _env_int("MAX_RESTART_ATTEMPTS", 3, minimum=0)

    def restart_backoff(self) -> float:
        """Delay before attempt N+1: base * 2**N, capped."""
        base = _env_float("RESTART_BACKOFF_SECONDS", 15.0, minimum=0.0)
        cap = _env_float("RESTART_BACKOFF_CAP_SECONDS", 120.0, minimum=0.0)
        attempt = max(0, self._restart_attempts - 1)
        return min(cap, base * (2 ** attempt))

    def health_interval(self) -> float:
        return _env_float("HEALTH_INTERVAL_SECONDS", 10.0, minimum=0.5)

    def console_log_max_bytes(self) -> int:
        return _env_int("CONSOLE_LOG_MAX_BYTES", 5 * 1024 * 1024, minimum=64 * 1024)

    # --------------------------------------------------------------- accessors

    @property
    def state(self) -> ServerState:
        with self._lock:
            return self._state

    @property
    def desired_state(self) -> DesiredState:
        with self._lock:
            return self._desired

    def _set_state(self, state: ServerState, note: str = "") -> None:
        with self._lock:
            if self._state == state and not note:
                return
            previous = self._state
            self._state = state
            self._events.append({
                "ts": time.time(),
                "from": previous.value,
                "to": state.value,
                "note": note,
            })
            self._startup_state = (
                "ready" if state == ServerState.RUNNING
                else "loading" if state == ServerState.STARTING
                else "unknown"
            )

    def _record_event(self, note: str, **fields: Any) -> None:
        with self._lock:
            self._events.append({"ts": time.time(), "note": note, **fields})

    def paper_pid(self) -> Optional[int]:
        """PID of the server, from our own child if we own it, else detection."""
        with self._lock:
            proc = self._proc
        if proc is not None:
            try:
                if proc.poll() is None:
                    return proc.pid
            except Exception:
                pass
        return find_paper_pid(self.server_dir)

    def is_running(self) -> bool:
        return self.paper_pid() is not None

    def is_ready(self) -> bool:
        with self._lock:
            return self._state == ServerState.RUNNING

    # ------------------------------------------------------------- preflight

    def preflight(self, cancel: Optional[threading.Event] = None) -> Dict[str, Any]:
        """Check everything Paper needs *before* trying to spawn it.

        Returns {"ok": bool, "problems": [str], "info": {...}}. A missing JVM,
        jar, EULA, or unwritable directory is reported here with a specific
        message instead of surfacing later as an opaque `Errno 2`.

        `cancel` (default: the lifecycle's own stop-request flag) makes the one
        slow step — probing the JVM version — abort promptly, so a Stop that
        arrives during preflight does not have to wait it out.
        """
        cancel = cancel if cancel is not None else self._cancel_start
        problems: List[str] = []
        info: Dict[str, Any] = {}

        server_dir = self.server_dir
        jar = self.server_jar

        # Java on PATH, and whether its major version can run modern Paper.
        java = None
        for candidate in ("java", "/usr/bin/java", "/usr/lib/jvm/default-java/bin/java"):
            if candidate.startswith("/"):
                if Path(candidate).exists():
                    java = candidate
                    break
            else:
                from shutil import which
                found = which(candidate)
                if found:
                    java = found
                    break
        if not java:
            problems.append(
                "Java was not found on PATH. Install a JRE/JDK (Java 21 for "
                "Minecraft 1.21.x) and re-run the environment cell.")
        else:
            info["java"] = java
            if cancel.is_set():
                return {"ok": False, "cancelled": True, "info": info,
                        "problems": ["start cancelled by a stop request"]}
            version = self._java_version(java, cancel)
            info["java_version"] = version
            if version:
                major = self._java_major(version)
                if major is not None and major < 21:
                    problems.append(
                        f"Java {major} is too old for Minecraft 1.21.x "
                        f"(need 21+). Detected: {version}")

        # The jar must exist and actually be a jar.
        if not jar.exists():
            problems.append(
                "paper.jar is missing. Run the Paper download cell (Cell 6) "
                "before starting the server.")
        else:
            try:
                with open(jar, "rb") as fh:
                    head = fh.read(2)
                if head != b"PK":
                    problems.append(
                        "paper.jar is not a valid ZIP/JAR archive; re-run the "
                        "Paper download cell (Cell 6).")
            except OSError as exc:
                problems.append(f"paper.jar cannot be read: {exc.strerror or exc}")

        # EULA must be accepted; Paper refuses to start otherwise.
        eula = server_dir / "eula.txt"
        if eula.exists():
            try:
                if "eula=true" not in eula.read_text(errors="replace").lower():
                    problems.append(
                        "EULA not accepted. Set eula=true in "
                        f"{server_dir.name}/eula.txt (Cell 7 does this).")
            except OSError:
                problems.append("eula.txt exists but cannot be read.")
        else:
            problems.append(
                "eula.txt is missing. Run the server configuration cell "
                "(Cell 7) to accept the Minecraft EULA.")

        # Storage must be writable — a read-only /kaggle/working mount is fatal.
        try:
            logs = server_dir / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            probe = logs / ".rahee-write-probe"
            probe.write_text("ok")
            probe.unlink()
        except OSError as exc:
            problems.append(
                f"Server directory is not writable ({exc.strerror or exc}). "
                "Kaggle's /kaggle/working must be writable to run a server.")

        # The world must not be mid-restore: opening a half-extracted world
        # would corrupt it. `drive_backup.restore_backup()` writes this marker
        # for the duration of its extraction, so a start is refused while a
        # restore is in flight rather than racing it.
        world_dir = (self.server_dir / "world"
                     if self._server_dir_override else Path(config.get_world_path()))
        if (world_dir / ".rahee-restore-in-progress").exists():
            problems.append(
                f"A world restore into {world_dir} is still in progress; "
                "refusing to start the server and risk the world.")

        # The world must never be *silently* regenerated. Paper creates a fresh
        # world when `level.dat` is missing, and the periodic Drive backup then
        # uploads that empty world over the real one — the one way this setup
        # can lose a world. A missing world is only fatal when something shows a
        # world was here before (otherwise this is a legitimate first run).
        level_dat = world_dir / "level.dat"
        world_present = level_dat.exists()
        info["world"] = {"path": str(world_dir), "level_dat": world_present}
        if not world_present:
            damaged = world_dir.exists()
            leftovers = [name for name in (
                "usercache.json", "logs/latest.log", "world_nether",
                "world_the_end",
            ) if (server_dir / name).exists()]
            if (damaged or leftovers) and not _env_flag("ALLOW_NEW_WORLD"):
                evidence = (
                    f"{world_dir} exists but has no level.dat (a partial or "
                    "failed restore)" if damaged
                    else "this server has run before ("
                         f"{', '.join(leftovers)}), but world/level.dat is gone")
                problems.append(
                    f"Refusing to start: {evidence}. Paper would generate a new "
                    "world and the periodic Drive backup would then overwrite "
                    "the real one. Restore the world from Drive (Cell 5), or "
                    "set ALLOW_NEW_WORLD=1 to create a new world deliberately.")

        # Memory: Paper needs real heap, and Xms must not exceed Xmx.
        xms = _env_int("JAVA_XMS", 1024, minimum=0)
        xmx = _env_int("JAVA_XMX", 2048, minimum=0)
        info["java_xms_mb"] = xms
        info["java_xmx_mb"] = xmx
        if xmx and xms and xms > xmx:
            problems.append(
                f"JAVA_XMS ({xms}m) is larger than JAVA_XMX ({xmx}m).")

        info["server_dir"] = str(server_dir)
        return {"ok": not problems, "problems": problems, "info": info}

    @staticmethod
    def _java_version(java: str,
                      cancel: Optional[threading.Event] = None,
                      timeout: int = 20) -> Optional[str]:
        """Probe `java -version`, interruptibly.

        Starting a JVM is preflight's only slow step (seconds on a small
        device), so it is polled instead of blocking: when a stop request
        arrives the probe is killed and the start aborts promptly rather than
        holding the operation gate until the probe finishes.
        """
        try:
            proc = subprocess.Popen([java, "-version"],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT,
                                    text=True)
        except Exception:
            return None

        deadline = time.time() + timeout
        try:
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                if cancel is not None and cancel.is_set():
                    proc.kill()
                    proc.wait(timeout=5)
                    return None
                time.sleep(0.1)
            else:
                proc.kill()
            output = proc.stdout.read() or ""
        except Exception:
            # A probe that cannot be read is simply inconclusive: report it as
            # unknown rather than failing the whole preflight.
            return None
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass

        for line in output.splitlines():
            line = line.strip()
            if line.lower().startswith("openjdk version") or \
                    line.lower().startswith("java version"):
                return line
        return output.splitlines()[0].strip() if output.strip() else None

    @staticmethod
    def _java_major(version_line: str) -> Optional[int]:
        import re
        match = re.search(r'"(\d+)(?:\.(\d+))?', version_line)
        if not match:
            return None
        major = int(match.group(1))
        if major == 1 and match.group(2):  # legacy 1.8 style
            major = int(match.group(2))
        return major

    # ---------------------------------------------------------------- helpers

    def register_helper(self, name: str, stop: Callable[[], Any],
                        is_running: Optional[Callable[[], bool]] = None) -> None:
        """Register a server-scoped helper (e.g. the Playit tunnel).

        Helpers are stopped on intentional Stop and left alone across a crash
        restart, because Paper returns on the same port.
        """
        with self._lock:
            self._helpers[name] = {"stop": stop, "is_running": is_running}

    def unregister_helper(self, name: str) -> None:
        with self._lock:
            self._helpers.pop(name, None)

    def helper_status(self) -> Dict[str, Any]:
        with self._lock:
            helpers = list(self._helpers.items())
        out: Dict[str, Any] = {}
        for name, spec in helpers:
            running: Optional[bool] = None
            if spec.get("is_running"):
                try:
                    running = bool(spec["is_running"]())
                except Exception:
                    running = None
            out[name] = {"registered": True, "running": running}
        return out

    def stop_helpers(self) -> Dict[str, str]:
        """Stop every registered helper; report each outcome honestly."""
        with self._lock:
            helpers = list(self._helpers.items())
        results: Dict[str, str] = {}
        for name, spec in helpers:
            try:
                spec["stop"]()
                results[name] = "stopped"
            except Exception as exc:
                results[name] = f"stop failed: {exc}"
        return results

    # ------------------------------------------------------------ log plumbing

    def _spawn_log_writer(self) -> ConsoleLogWriter:
        writer = ConsoleLogWriter(self.console_path, self.console_log_max_bytes())
        writer.open()
        return writer

    def _start_reader(self, proc: subprocess.Popen,
                      writer: ConsoleLogWriter) -> threading.Thread:
        """Drain the child's stdout continuously into the bounded log.

        A stalled/undrained pipe is what froze Paper at startup before, so this
        thread exists for the whole life of the process and never blocks the
        lifecycle lock.
        """
        def _pump() -> None:
            stream = proc.stdout
            if stream is None:
                return
            try:
                for raw in iter(stream.readline, ""):
                    if not raw:
                        break
                    writer.write(raw)
            except (ValueError, OSError):
                pass
            finally:
                try:
                    stream.close()
                except Exception:
                    pass

        thread = threading.Thread(target=_pump, name="rahee-console-drain",
                                  daemon=True)
        thread.start()
        return thread

    # ------------------------------------------------------------- start/stop

    def start(self, reason: str = "manual", force: bool = False) -> Dict[str, Any]:
        """Start Paper, waiting for real readiness.

        Idempotent: a start request while a server is already running or
        starting returns success with `already_running` and spawns nothing.
        """
        with self._lock:
            state = self._state
            starting = self._starting
        desired = self.desired_state

        if starting and desired == DesiredState.RUNNING:
            return {"state": "success", "error": None,
                    "result": {"already_running": True, "server_state": state.value,
                               "note": "a start is already in progress"}}

        existing = self.paper_pid()
        if existing is not None:
            with self._lock:
                self._desired = DesiredState.RUNNING
                if self._state != ServerState.RUNNING:
                    self._ready_at = self._ready_at or time.time()
                    self._set_state(ServerState.RUNNING, "existing process adopted")
            return {"state": "success", "error": None,
                    "result": {"already_running": True, "pid": existing,
                               "server_state": self.state.value}}

        if state == ServerState.STOPPING and not force:
            # Record nothing here: the stop is still the standing intent, and
            # flipping desired_state to RUNNING would re-arm recovery for a
            # server the user is shutting down.
            return {"state": "failed", "error":
                    "A stop is in progress; wait for it to finish before starting.",
                    "result": {"server_state": state.value}}

        with self._lock:
            self._desired = DesiredState.RUNNING
        # This start is now the standing intent, so any earlier stop request no
        # longer cancels it.
        self._cancel_start.clear()

        if not self._op_lock.acquire(blocking=False):
            return {"state": "failed",
                    "error": "another lifecycle operation is already running",
                    "result": {"server_state": self.state.value,
                               "desired_state": self.desired_state.value}}
        try:
            return self._start_locked(reason=reason)
        finally:
            self._op_lock.release()

    def _start_locked(self, reason: str) -> Dict[str, Any]:
        """Run one start under the operation gate, flagging it as in flight."""
        with self._lock:
            self._starting = True
        try:
            return self._start_locked_body(reason)
        finally:
            with self._lock:
                self._starting = False

    def _start_locked_body(self, reason: str) -> Dict[str, Any]:
        if self._cancel_start.is_set():
            return self._cancelled_before_spawn()
        pre = self.preflight()
        if pre.get("cancelled") or self._cancel_start.is_set():
            # A stop arrived while preflight was running: nothing was spawned,
            # so honour it here instead of starting a server nobody wants.
            return self._cancelled_before_spawn()
        if not pre["ok"]:
            message = "; ".join(pre["problems"])
            with self._lock:
                self._last_error = message
                self._set_state(ServerState.CRASHED, "preflight failed")
            self._record_event("preflight_failed", problems=pre["problems"])
            return {"state": "failed", "error": message,
                    "result": {"preflight": pre["info"], "problems": pre["problems"],
                               "server_state": ServerState.CRASHED.value}}

        with self._lock:
            self._exit_code = None
            self._last_error = None
            self._ready_marker = None
            self._started_at = time.time()
            self._ready_at = None
            self._set_state(ServerState.STARTING, reason)

        cmd = [
            "java",
            f"-Xms{_env_int('JAVA_XMS', 1024)}m",
            f"-Xmx{_env_int('JAVA_XMX', 2048)}m",
            "-jar", "paper.jar",
            "nogui",
        ]

        try:
            writer = self._spawn_log_writer()
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.server_dir),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                close_fds=True,
            )
        except FileNotFoundError:
            message = ("Java could not be executed (not found on PATH). "
                       "Install a JRE/JDK 21 and re-run the environment cell.")
            self._fail_start(message)
            return {"state": "failed", "error": message,
                    "result": {"server_state": ServerState.CRASHED.value}}
        except PermissionError as exc:
            message = f"Java is not executable: {exc}"
            self._fail_start(message)
            return {"state": "failed", "error": message,
                    "result": {"server_state": ServerState.CRASHED.value}}
        except OSError as exc:
            message = f"Could not launch the server process: {exc}"
            self._fail_start(message)
            return {"state": "failed", "error": message,
                    "result": {"server_state": ServerState.CRASHED.value}}

        with self._lock:
            self._proc = proc
            self._owns_process = True
            self._log = writer

        reader = self._start_reader(proc, writer)
        with self._lock:
            self._reader = reader

        self._record_event("spawned", pid=proc.pid, reason=reason)

        # Phase 1 — survive the first seconds. An immediate exit means a broken
        # jar, wrong Java, or rejected EULA: report that, do not claim a start.
        settle_deadline = time.time() + self.settle_timeout()
        while time.time() < settle_deadline:
            if self._cancel_start.is_set():
                return self._abort_start_for_stop(proc, writer)
            if proc.poll() is not None:
                message = (f"Java exited during startup (code {proc.returncode}). "
                           f"Last output: {writer.tail_text()[-400:] or 'none'}")
                self._handle_unexpected_exit(proc.returncode, phase="startup")
                return {"state": "failed", "error": message,
                        "result": {"exit_code": proc.returncode,
                                   "log_tail": writer.tail_text()}}
            # Short slices keep a Stop click responsive without busy-waiting.
            self._cancel_start.wait(0.25)

        # Phase 2 — wait for Paper's OWN readiness marker, bounded.
        ready, marker, failure, cancelled = self._wait_ready(
            proc, writer, self.start_timeout())

        if cancelled:
            return self._abort_start_for_stop(proc, writer)

        if failure:
            message = f"Paper reported a startup failure: {failure}"
            with self._lock:
                self._last_error = message
                self._set_state(ServerState.CRASHED, "startup failure")
            return {"state": "failed", "error": message,
                    "result": {"pid": proc.pid, "server_state": ServerState.CRASHED.value,
                               "log_tail": writer.tail_text()}}

        if not ready:
            # Java is alive but Minecraft is not ready yet. This is NOT success,
            # and it is NOT a crash: report a timeout and let the supervisor
            # keep observing.
            return {"state": "timeout", "error":
                    f"Paper did not report readiness within {self.start_timeout()}s",
                    "result": {"pid": proc.pid, "ready": False,
                               "server_state": self.state.value,
                               "warning": "Java is running but Paper has not "
                                          "finished loading; it may still be "
                                          "generating the world.",
                               "log_tail": writer.tail_text()[-800:]}}

        with self._lock:
            self._ready_at = time.time()
            self._ready_marker = marker
            self._crashed_reason = None
            self._set_state(ServerState.RUNNING, "ready")
        self._record_event("ready", marker=marker)
        return {"state": "success", "error": None,
                "result": {"pid": proc.pid, "started": True, "ready": True,
                           "ready_marker": marker,
                           "server_state": ServerState.RUNNING.value}}

    def _cancelled_before_spawn(self) -> Dict[str, Any]:
        """A stop request arrived before any JVM existed.

        Nothing to kill, and the stopped state is already correct; this exists
        so the caller gets an honest "cancelled" instead of a success or a
        misleading error.
        """
        self._mark_stopped("start cancelled before launch")
        with self._lock:
            self._last_error = None
            self._crashed_reason = None
        self._record_event("start_cancelled", phase="preflight")
        return {"state": "cancelled",
                "error": "start cancelled by a stop request",
                "result": {"cancelled": True,
                           "server_state": ServerState.STOPPED.value,
                           "desired_state": DesiredState.STOPPED.value}}

    def _abort_start_for_stop(self, proc: subprocess.Popen,
                             writer: ConsoleLogWriter) -> Dict[str, Any]:
        """A stop request arrived mid-start: actually stop, don't finish starting.

        The JVM is already spawned, so it is brought down here (console `stop`,
        then bounded escalation) and the start reports that it was cancelled.
        The waiting `stop()` then finds no process and reports a real success,
        so the dashboard never shows a "running" server the user just stopped.
        """
        self._record_event("start_cancelled", pid=proc.pid)
        if self._write_console("stop"):
            self._wait_termination(proc.pid, min(30, self.settle_timeout() * 3))
        if self._pid_alive(proc.pid):
            try:
                os.kill(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self._wait_termination(proc.pid, 20)
        if self._pid_alive(proc.pid):
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self._wait_termination(proc.pid, 10)
        tail = writer.tail_text()[-400:]
        self._mark_stopped("start cancelled by stop request")
        with self._lock:
            # Not an error state: the user asked for exactly this. The start's
            # own result carries the cancellation; `last_error` stays clean so
            # the dashboard does not show a fault on a deliberately stopped
            # server.
            self._last_error = None
            self._crashed_reason = None
        return {"state": "cancelled",
                "error": "start cancelled by a stop request",
                "result": {"cancelled": True, "pid": proc.pid,
                           "server_state": ServerState.STOPPED.value,
                           "desired_state": DesiredState.STOPPED.value,
                           "log_tail": tail}}

    def _fail_start(self, message: str) -> None:
        with self._lock:
            self._last_error = message
            self._set_state(ServerState.CRASHED, "start failed")
            self._proc = None
            self._owns_process = False
            if self._log is not None:
                self._log.close()
                self._log = None

    def _paper_state(self, writer: Optional[ConsoleLogWriter] = None) -> Dict[str, Any]:
        """Readiness from Paper's own log, falling back to the drained stdout.

        Paper writes `logs/latest.log` itself, which is the primary source. If
        that file is unreadable or unwritten in a given launch setup, the output
        we drained ourselves is consulted — but readiness is never *inferred*
        from the process being alive.
        """
        state = paper_startup_state(self.server_dir, self.log_path)
        if state.get("state") in ("ready", "failed"):
            return state
        if writer is not None and writer.tail:
            captured = writer.tail_text()
            marker = next((l for l in reversed(captured.splitlines())
                           if any(m in l for m in READY_MARKERS)), None)
            if marker:
                return {"state": "ready", "marker": marker}
            fatal = next((l for l in reversed(captured.splitlines())
                          if any(m in l for m in FATAL_MARKERS)), None)
            if fatal:
                return {"state": "failed", "marker": fatal}
        return state

    def _wait_ready(self, proc: subprocess.Popen, writer: ConsoleLogWriter,
                    timeout: int,
                    cancel: Optional[threading.Event] = None
                    ) -> Tuple[bool, Optional[str], Optional[str], bool]:
        """Wait for Paper's readiness marker.

        Returns (ready, marker, failure, cancelled). `cancelled` is True when a
        stop request asked this start to give up; the caller then stops the JVM
        rather than continuing to wait for a server nobody wants.
        """
        cancel = cancel if cancel is not None else self._cancel_start
        deadline = time.time() + max(5, timeout)
        next_probe = 0.0
        while time.time() < deadline:
            if cancel.is_set():
                return False, None, None, True
            if proc.poll() is not None:
                return (False, None,
                        f"Java exited during startup (code {proc.returncode})",
                        False)

            now = time.time()
            if now >= next_probe:
                next_probe = now + 1.0
                state = self._paper_state(writer)
                if state["state"] == "ready" and state.get("marker"):
                    return True, state["marker"], None, False
                if state["state"] == "failed":
                    return False, None, state.get("marker"), False
            # Wakes immediately when a stop request sets the flag.
            cancel.wait(0.25)
        return False, None, None, False

    def stop(self, reason: str = "manual", stop_helpers: bool = True) -> Dict[str, Any]:
        """Stop Paper gracefully: console `stop` first, then bounded escalation.

        `desired_state` is set to STOPPED *before* anything is signalled, so no
        supervisor can start it again behind the user's back. If a start is
        still in flight it is asked to abort (and waited for, bounded) so a Stop
        click during a long world-load actually stops the server.
        """
        with self._lock:
            self._desired = DesiredState.STOPPED
        self._cancel_start.set()
        self._record_event("stop_requested", reason=reason)

        acquired = self._op_lock.acquire(blocking=False)
        if not acquired:
            deadline = time.time() + self.stop_timeout()
            while time.time() < deadline and not acquired:
                acquired = self._op_lock.acquire(timeout=0.5)
        if not acquired:
            return {"state": "failed",
                    "error": "another lifecycle operation is still running; "
                             "the stop request could not be applied",
                    "result": {"server_state": self.state.value,
                               "desired_state": DesiredState.STOPPED.value}}
        try:
            result = self._stop_locked()
        finally:
            self._op_lock.release()

        helper_results: Dict[str, str] = {}
        if stop_helpers:
            helper_results = self.stop_helpers()
        if helper_results:
            result.setdefault("result", {})["helpers"] = helper_results
        return result

    def _stop_locked(self) -> Dict[str, Any]:
        with self._lock:
            self._set_state(ServerState.STOPPING, "stop requested")

        pid = self.paper_pid()
        if pid is None:
            self._mark_stopped("already stopped")
            return {"state": "success", "error": None,
                    "result": {"already_stopped": True,
                               "server_state": ServerState.STOPPED.value}}

        timeout = self.stop_timeout()
        proc = self._proc

        # 1. Preferred: Minecraft's own `stop`, which saves the world.
        if self._write_console("stop"):
            if self._wait_termination(pid, timeout):
                self._mark_stopped("stopped via console")
                return {"state": "success", "error": None,
                        "result": {"method": "console stop", "pid": pid,
                                   "saved": True,
                                   "server_state": ServerState.STOPPED.value}}

        # 2. SIGTERM — Paper installs a handler and shuts down cleanly.
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            self._mark_stopped("exited before signal")
            return {"state": "success", "error": None,
                    "result": {"note": "process already exited",
                               "server_state": ServerState.STOPPED.value}}
        except Exception as exc:
            return {"state": "failed", "error": f"SIGTERM failed: {exc}",
                    "result": {"pid": pid, "server_state": self.state.value}}

        if self._wait_termination(pid, timeout):
            self._mark_stopped("stopped via SIGTERM")
            return {"state": "success", "error": None,
                    "result": {"method": "SIGTERM", "pid": pid, "saved": True,
                               "server_state": ServerState.STOPPED.value}}

        # 3. Last resort. Report honestly that the world may be missing its last
        #    moments rather than pretending this was a graceful save.
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            self._mark_stopped("exited just after timeout")
            return {"state": "success", "error": None,
                    "result": {"note": "exited just after timeout",
                               "server_state": ServerState.STOPPED.value}}

        if self._wait_termination(pid, 15):
            self._mark_stopped("stopped via SIGKILL")
            return {"state": "success", "error": None,
                    "result": {"method": "SIGKILL", "pid": pid, "saved": False,
                               "warning": "Forced kill — the last moments before "
                                          "the kill may not be saved",
                               "server_state": ServerState.STOPPED.value}}

        with self._lock:
            self._last_error = f"Process {pid} did not terminate"
            self._set_state(ServerState.CRASHED, "stop failed")
        return {"state": "timeout", "error": "Process did not terminate",
                "result": {"pid": pid, "server_state": self.state.value}}

    def _mark_stopped(self, note: str) -> None:
        with self._lock:
            self._set_state(ServerState.STOPPED, note)
            self._stopped_at = time.time()
            self._ready_at = None
            self._ready_marker = None
            proc = self._proc
            log = self._log
            self._proc = None
            self._owns_process = False
            self._log = None
        if log is not None:
            log.close()
        if proc is not None:
            try:
                proc.poll()  # reap so it cannot linger as a zombie
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass

    def send_console(self, line: str) -> bool:
        """Public console channel: send one validated line to Paper's stdin.

        Only the component that owns the JVM has a stdin pipe; handing commands
        to this method keeps a single writer on the Minecraft console.
        """
        return self._write_console(line)

    def _write_console(self, line: str) -> bool:
        """Send a line to Paper's stdin (Minecraft console), if we own it."""
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None or proc.stdin is None:
            return False
        try:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
            return True
        except Exception:
            return False

    def _pid_alive(self, pid: int) -> bool:
        """True only while `pid` is a process that has not already exited.

        A zombie still answers `os.kill(pid, 0)`; treating that as "alive" made
        a cleanly saved stop look like a timeout that needed SIGKILL.
        """
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                stat_line = fh.read().decode("utf-8", "replace")
            state = stat_line[stat_line.rindex(")") + 2:].split()[0]
            if state in ("Z", "X", "x"):
                return False
        except (OSError, ValueError, IndexError):
            if os.path.isdir("/proc") and not os.path.exists(f"/proc/{pid}"):
                return False
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except Exception:
            return False

    def _wait_termination(self, pid: int, timeout: float) -> bool:
        deadline = time.time() + max(1, timeout)
        while time.time() < deadline:
            with self._lock:
                proc = self._proc
            if proc is not None and proc.pid == pid:
                try:
                    proc.poll()  # reap our own child
                except Exception:
                    pass
            if not self._pid_alive(pid) and find_paper_pid(self.server_dir) is None:
                return True
            time.sleep(1)
        return False

    # -------------------------------------------------------------- supervisor

    def supervise(self) -> Optional[str]:
        """One health tick. Returns a description of anything it did.

        Non-blocking and side-effect-light:
          * serialized — concurrent ticks cannot double-count one crash;
          * does nothing while a start/stop is in flight;
          * promotes a late-loading server from STARTING to RUNNING once Paper
            itself says it is ready;
          * notices an unexpected exit and runs the bounded recovery policy;
          * never starts anything unless `desired_state` is RUNNING.
        """
        if not self._tick_lock.acquire(blocking=False):
            return None
        try:
            return self._supervise_once()
        finally:
            self._tick_lock.release()

    def _supervise_once(self) -> Optional[str]:
        """Body of one health tick (see `supervise`)."""
        with self._lock:
            state = self._state
            desired = self._desired
            proc = self._proc

        # The user asked for this state; nothing to supervise.
        if state in (ServerState.STOPPED, ServerState.STOPPING):
            return None

        # A start/stop/restart is actively running: it owns the transitions and
        # will report its own outcome. Never race it.
        if self._op_lock.locked():
            return None

        # CRASHED is a terminal, reported state: retrying is the human's call
        # (a crash-restart schedules itself via RESTARTING instead).
        if state == ServerState.CRASHED:
            return None

        # Liveness: prefer our own child handle when we own the process.
        if proc is not None:
            alive = proc.poll() is None
            exit_code = None if alive else proc.returncode
        else:
            alive = find_paper_pid(self.server_dir) is not None
            exit_code = None

        if alive:
            if state == ServerState.STARTING:
                return self._promote_if_ready()
            if state == ServerState.RUNNING:
                self._maybe_reset_restart_budget()
            return None

        # Not alive. A RESTARTING server with no process is simply waiting out
        # its backoff, which is not another crash.
        if state == ServerState.RESTARTING and proc is None:
            return None

        if desired == DesiredState.STOPPED:
            # The human asked for this. Record it, do not restart.
            self._handle_unexpected_exit(exit_code, phase="after-stop",
                                         restart=False)
            return "stopped-by-intent"

        return self._handle_unexpected_exit(exit_code, phase="running")

    def _promote_if_ready(self) -> Optional[str]:
        """Move a still-loading server to RUNNING once Paper reports ready.

        A start that exceeded its readiness window is NOT success, but it is
        also not a failure: the world may still be generating. This is how that
        server is picked up when it finally finishes loading.
        """
        state = self._paper_state(self._log)
        if state["state"] != "ready" or not state.get("marker"):
            if state["state"] == "failed":
                return self._handle_unexpected_exit(None, phase="startup",
                                                    restart=False)
            return None
        with self._lock:
            self._ready_at = time.time()
            self._ready_marker = state["marker"]
            self._set_state(ServerState.RUNNING, "ready (late)")
        self._record_event("ready_late", marker=state["marker"])
        return "promoted-to-running"

    def _maybe_reset_restart_budget(self) -> None:
        """Clear the failure budget only after a sustained healthy uptime.

        Resetting it immediately on "ready" would let a server that boots and
        dies in a loop restart forever; requiring `RESTART_RESET_AFTER_SECONDS`
        of continuous readiness makes the bound real.
        """
        reset_after = _env_float("RESTART_RESET_AFTER_SECONDS", 300.0, minimum=0.0)
        with self._lock:
            ready_at = self._ready_at
            attempts = self._restart_attempts
        if not attempts or not ready_at:
            return
        if time.time() - ready_at >= reset_after:
            with self._lock:
                self._restart_attempts = 0
                self._crashed_reason = None
            self._record_event("restart_budget_reset",
                               after=int(time.time() - ready_at))

    def _handle_unexpected_exit(self, exit_code: Optional[int], phase: str,
                                restart: bool = True) -> Optional[str]:
        """Record an unexpected exit and decide whether recovery is allowed."""
        with self._lock:
            self._exit_code = exit_code
            self._last_exit_at = time.time()
            self._proc = None
            self._owns_process = False
            log = self._log
        tail = log.tail_text() if log is not None else ""
        startup = self._paper_state(log)
        if log is not None:
            log.close()
        with self._lock:
            self._last_log_tail = tail
        reason = f"Paper exited unexpectedly during {phase}"
        if exit_code is not None:
            reason += f" (exit code {exit_code})"
        if startup.get("marker"):
            reason += f": {startup['marker']}"

        with self._lock:
            self._last_error = reason
            self._set_state(ServerState.CRASHED, "unexpected exit")
            attempts = self._restart_attempts
        self._record_event("unexpected_exit", exit_code=exit_code, phase=phase,
                           log_tail=tail[-1000:])

        if not restart or self.desired_state == DesiredState.STOPPED:
            with self._lock:
                self._set_state(ServerState.STOPPED, "exited after stop request")
            return "exit-recorded"

        # An unrecoverable startup failure must not be retried in a loop: the
        # jar is broken, Java is incompatible, or the EULA is refused.
        if startup.get("state") == "failed":
            with self._lock:
                self._crashed_reason = (
                    f"Not restarting: Paper cannot start with this jar/Java "
                    f"configuration — {startup.get('marker')}")
                self._last_error = self._crashed_reason
                self._set_state(ServerState.CRASHED, "unrecoverable startup failure")
            self._notify("unrecoverable", {"reason": self._crashed_reason})
            return "unrecoverable"

        max_attempts = self.max_restart_attempts()
        if self._restart_attempts >= max_attempts:
            with self._lock:
                self._crashed_reason = (
                    f"Giving up after {self._restart_attempts} restart "
                    f"attempt(s). Last error: {reason}")
                self._last_error = self._crashed_reason
                self._set_state(ServerState.CRASHED, "restart budget exhausted")
            self._notify("restart_exhausted", {"reason": self._crashed_reason})
            return "restart-exhausted"

        with self._lock:
            self._restart_attempts += 1
            attempt = self._restart_attempts
            backoff = self.restart_backoff()
            self._next_restart_at = time.time() + backoff
            self._set_state(ServerState.RESTARTING,
                            f"attempt {attempt} in {backoff:.0f}s")
        self._record_event("restart_scheduled", attempt=attempt, backoff=backoff)
        self._notify("restart_scheduled", {"attempt": attempt, "backoff": backoff,
                                           "exit_code": exit_code})
        return "restart-scheduled"

    def restart_due(self) -> bool:
        """True when a scheduled restart is allowed to run now."""
        with self._lock:
            if self._state != ServerState.RESTARTING:
                return False
            if self._desired != DesiredState.RUNNING:
                return False
            due = self._next_restart_at
        return due is not None and time.time() >= due

    def perform_restart(self) -> Dict[str, Any]:
        """Execute a scheduled crash-restart (never bypasses `desired_state`)."""
        if self.desired_state != DesiredState.RUNNING:
            with self._lock:
                self._set_state(ServerState.STOPPED, "restart cancelled by intent")
            return {"state": "failed", "error": "restart cancelled: desired_state=STOPPED",
                    "result": {}}

        # Never start a second JVM while the previous one might still be alive.
        if find_paper_pid(self.server_dir) is not None:
            return {"state": "failed", "error": "previous process is still alive",
                    "result": {"server_state": self.state.value}}

        result = self.start(reason="crash-recovery")
        if result.get("state") != "success":
            return result
        self._record_event("restart_succeeded",
                           attempts=self._restart_attempts)
        self._notify("restart_succeeded", {"attempts": self._restart_attempts})
        return result

    def restart_once(self) -> Dict[str, Any]:
        """User-facing restart: graceful stop, verify termination, start."""
        with self._lock:
            self._set_state(ServerState.RESTARTING, "manual restart")

        stop = self.stop(reason="restart", stop_helpers=False)
        if stop["state"] not in ("success",):
            with self._lock:
                self._set_state(ServerState.CRASHED, "restart stop phase failed")
            return {"state": "failed",
                    "error": f"Stop phase failed: {stop.get('error')}",
                    "result": {"stop": stop}}

        pid = self.paper_pid()
        if pid is not None and not self._wait_termination(pid, 20):
            with self._lock:
                self._set_state(ServerState.CRASHED, "process survived stop")
            return {"state": "failed",
                    "error": "Process still alive after the stop phase",
                    "result": {"pid": pid}}

        time.sleep(2)  # let the port free up
        result = self.start(reason="restart")
        result.setdefault("result", {})["stop"] = stop.get("result")
        return result

    # ------------------------------------------------------------- supervisor

    def on_event(self, callback: Callable[[str, Dict[str, Any]], None]) -> None:
        """Register a notification callback for crash/restart milestones."""
        self._supervisor_on_event = callback

    def _notify(self, event: str, payload: Dict[str, Any]) -> None:
        callback = self._supervisor_on_event
        if callback is None:
            return
        try:
            callback(event, payload)
        except Exception:
            pass

    def run_supervisor(self, poll_interval: Optional[float] = None) -> None:
        """Background health loop: crash detection + bounded recovery.

        Bounded and interruptible: it sleeps in small slices (so CPU stays
        near-idle) and exits as soon as shutdown is requested.
        """
        interval = poll_interval or self.health_interval()
        while not self._supervisor_stop.is_set():
            try:
                self.supervise()
                if self.restart_due():
                    self.perform_restart()
            except Exception as exc:  # never let the supervisor die silently
                self._record_event("supervisor_error", error=str(exc))
            self._supervisor_stop.wait(interval)

    def start_supervisor(self) -> None:
        """Start the background health loop (idempotent)."""
        with self._lock:
            if self._supervisor_thread is not None and \
                    self._supervisor_thread.is_alive():
                return
            self._supervisor_stop.clear()
            thread = threading.Thread(target=self.run_supervisor,
                                      name="rahee-lifecycle-supervisor",
                                      daemon=True)
            self._supervisor_thread = thread
        thread.start()

    def stop_supervisor(self) -> None:
        self._supervisor_stop.set()
        with self._lock:
            thread = self._supervisor_thread
            self._supervisor_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=5)

    # ------------------------------------------------- blocking supervisor wait

    def wait_while_running(self, poll_interval: Optional[float] = None,
                           on_tick: Optional[Callable[[Dict[str, Any]], None]] = None,
                           max_seconds: Optional[float] = None) -> Dict[str, Any]:
        """Block the calling (notebook) execution while the server should run.

        This is the notebook's keep-alive: the cell stays in its execution loop,
        checking health at a low, non-busy interval, and returns as soon as
        `desired_state` becomes STOPPED (the Stop button) or the optional
        `max_seconds` budget is exhausted. It never restarts anything itself —
        the lifecycle manager owns that — so there is exactly one supervisor.
        """
        interval = poll_interval or self.health_interval()
        started = time.time()
        while True:
            if self.desired_state == DesiredState.STOPPED:
                return {"reason": "desired_state=STOPPED",
                        "seconds": round(time.time() - started, 1),
                        "state": self.state.value}
            if self._supervisor_stop.is_set():
                return {"reason": "shutdown requested",
                        "seconds": round(time.time() - started, 1),
                        "state": self.state.value}
            if max_seconds is not None and time.time() - started >= max_seconds:
                return {"reason": "time budget reached",
                        "seconds": round(time.time() - started, 1),
                        "state": self.state.value}
            if on_tick is not None:
                try:
                    on_tick(self.status())
                except Exception:
                    pass
            # Small slices keep Stop responsive even with a long health interval.
            deadline = time.time() + interval
            while time.time() < deadline and not self._supervisor_stop.is_set():
                if self.desired_state == DesiredState.STOPPED:
                    break
                time.sleep(min(1.0, max(0.05, deadline - time.time())))

    # ----------------------------------------------------------------- status

    def status(self) -> Dict[str, Any]:
        """Observed state for the heartbeat, the dashboard and the notebook."""
        with self._lock:
            state = self._state
            desired = self._desired
            started_at = self._started_at
            ready_at = self._ready_at
            exit_code = self._exit_code
            last_error = self._last_error
            attempts = self._restart_attempts
            crashed_reason = self._crashed_reason
            ready_marker = self._ready_marker
            events = list(self._events)[-10:]
            log = self._log

        pid = self.paper_pid()
        if state == ServerState.RUNNING and pid is None:
            # Never claim RUNNING for a process that is gone.
            state = ServerState.CRASHED
            last_error = last_error or "the server process is no longer present"

        startup = "unknown"
        if state in (ServerState.RUNNING, ServerState.STARTING):
            startup = "ready" if state == ServerState.RUNNING else "loading"
        uptime = int(time.time() - ready_at) if ready_at else 0

        return {
            "server_state": state.value,
            "desired_state": desired.value,
            "paper_running": pid is not None,
            "paper_pid": pid,
            "paper_startup_state": startup,
            "ready": state == ServerState.RUNNING and pid is not None,
            "ready_marker": ready_marker,
            "uptime_seconds": uptime,
            "started_at": started_at,
            "exit_code": exit_code,
            "restart_attempts": attempts,
            "restart_scheduled_in": (
                max(0, int((self._next_restart_at or 0) - time.time()))
                if state == ServerState.RESTARTING else None),
            "last_error": last_error or crashed_reason,
            "log_tail": (log.tail_text()[-1200:] if log is not None
                         else (self._last_log_tail or None)),
            "events": events,
            "helpers": self.helper_status(),
        }

    # ----------------------------------------------------------- clean shutdown

    def shutdown(self, save: bool = True, timeout: Optional[float] = None) -> Dict[str, Any]:
        """Best-effort clean shutdown, used on interpreter exit.

        Kaggle can end a session at any moment; this asks Minecraft to save and
        stop, bounded, so the world is not lost to a kill. It never raises.
        """
        try:
            self._supervisor_stop.set()
            if self.desired_state == DesiredState.STOPPED and not self.is_running():
                return {"state": "success", "result": {"already_stopped": True}}
            with self._lock:
                self._desired = DesiredState.STOPPED
            if save and self.is_running():
                self._write_console("save-all")
            result = self.stop(reason="shutdown", stop_helpers=True)
            if result.get("state") == "failed" and "already running" in \
                    (result.get("error") or ""):
                # A start/stop was mid-flight; wait briefly, then try once more
                # so an interactive Ctrl-C still saves the world.
                deadline = time.time() + (timeout or 30)
                while time.time() < deadline and self._op_lock.locked():
                    time.sleep(0.5)
                result = self.stop(reason="shutdown", stop_helpers=True)
            return result
        except Exception as exc:
            return {"state": "failed", "error": str(exc), "result": {}}


# Process-wide singleton: ONE lifecycle owner per kernel/backend process.
lifecycle = ServerLifecycle()

# Explicit per-directory managers, created only when a caller names a server
# directory other than the configured one (tests, tooling). Production always
# resolves to the single singleton above, so the notebook, the agent and the
# dashboard all drive the same object.
_bound: Dict[str, ServerLifecycle] = {"": lifecycle}
_bound_lock = threading.Lock()


def get_lifecycle(server_dir: Optional[Any] = None) -> ServerLifecycle:
    """Return the lifecycle manager that owns `server_dir` (default: config)."""
    if server_dir is None or str(server_dir) == str(_config_server_dir()):
        return lifecycle
    key = str(Path(server_dir))
    with _bound_lock:
        manager = _bound.get(key)
        if manager is None:
            manager = ServerLifecycle(server_dir=key)
            _bound[key] = manager
        return manager


def _config_server_dir() -> str:
    return str(Path(config.SERVER_DIR))
