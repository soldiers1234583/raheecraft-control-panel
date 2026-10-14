#!/usr/bin/env python3
"""
RaheeCraft Agent Action Executor
================================
Executes power and console actions received from the backend.

Runs on the Kaggle side where Paper lives. Security rules:
  - Strict action-type allowlist (start/stop/restart/console_command only).
  - Strict Minecraft console command allowlist; never a shell.
  - Commands are written to the Paper process stdin channel only —
    never passed to eval(), os.system(), or string-built subprocess.
  - Stop uses Minecraft's own `stop` command so the world saves;
    SIGTERM only as a bounded fallback, SIGKILL only as last resort.
  - Never deletes or overwrites world data.
"""

import os
import sys
import time
import signal
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.config import config

# --- Allowlists -------------------------------------------------------------

ALLOWED_ACTION_TYPES = {
    "start_server", "stop_server", "restart_server", "console_command",
}

# Minecraft console commands permitted from the dashboard.
ALLOWED_CONSOLE_COMMANDS = {
    "list", "say", "time", "weather", "difficulty", "gamemode",
    "whitelist", "save-all", "save-off", "save-on", "stop",
    "op", "deop", "ban", "pardon", "kick", "me", "tell",
    "scoreboard", "title", "summon", "give", "tp", "effect",
    "xp", "clear", "enchant", "unban", "setworldspawn",
    "setidletimeout", "spreadplayers", "worldborder", "recipe",
    "trigger", "gamerule", "defaultgamemode", "difficulty",
}

# Characters that never appear in normal MC commands but are common in
# shell/Python injection attempts — rejected outright.
FORBIDDEN_CHARS = set("\n\r;|&`$><\\{}")

STOP_TIMEOUT_SECONDS = int(os.environ.get("STOP_TIMEOUT_SECONDS", "60"))
START_TIMEOUT_SECONDS = int(os.environ.get("START_TIMEOUT_SECONDS", "30"))


def validate_console_command(command: str) -> Tuple[bool, str]:
    """Validate a Minecraft console command. Returns (ok, error)."""
    if not command or not command.strip():
        return False, "Empty command"
    if len(command) > 500:
        return False, "Command too long (max 500 chars)"
    if any(ch in FORBIDDEN_CHARS for ch in command):
        return False, "Command contains forbidden characters"
    base = command.strip().split()[0].lower()
    if base not in ALLOWED_CONSOLE_COMMANDS:
        return False, f"Command '{base}' not allowed"
    return True, ""


class ActionExecutor:
    """Executes allowlisted actions against the local Paper server."""

    def __init__(self, server_dir: str = None):
        self.server_dir = Path(server_dir or config.SERVER_DIR)
        self.server_jar = self.server_dir / "paper.jar"
        self._console_proc: Optional[subprocess.Popen] = None
        self._java_xms = os.environ.get("JAVA_XMS", "1024")
        self._java_xmx = os.environ.get("JAVA_XMX", "2048")

    # --- process helpers ----------------------------------------------------

    def find_paper_pid(self) -> Optional[int]:
        """Find the Paper server PID (paper.jar), or None."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "paper.jar"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.strip().split("\n")[0])
        except Exception:
            pass
        return None

    def _pid_alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except (ProcessLookupError, PermissionError):
            return False
        except Exception:
            return False

    # --- console channel ----------------------------------------------------

    def _write_console(self, line: str) -> bool:
        """Write a line to Paper's stdin if we own the process."""
        proc = self._console_proc
        if proc is None or proc.poll() is not None:
            return False
        try:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
            return True
        except Exception:
            return False

    # --- actions ------------------------------------------------------------

    def execute(self, action: dict) -> Dict[str, Any]:
        """Execute one action dict from the backend.

        Returns {"state": "success"|"failed", "error": str|None, "result": dict}.
        """
        action_type = action.get("action_type")
        action_id = action.get("action_id", "?")
        params = action.get("params") or {}

        if action_type not in ALLOWED_ACTION_TYPES:
            return {"state": "failed",
                    "error": f"Unknown action type: {action_type}",
                    "result": {}}

        try:
            if action_type == "start_server":
                return self._start()
            if action_type == "stop_server":
                return self._stop()
            if action_type == "restart_server":
                return self._restart()
            if action_type == "console_command":
                return self._console(params.get("command", ""))
        except Exception as e:
            return {"state": "failed", "error": str(e), "result": {}}

        return {"state": "failed", "error": "Unhandled action", "result": {}}

    def _start(self) -> Dict[str, Any]:
        pid = self.find_paper_pid()
        if pid:
            # Idempotent: already running is not an error
            return {"state": "success", "error": None,
                    "result": {"already_running": True, "pid": pid}}

        if not self.server_jar.exists():
            return {"state": "failed",
                    "error": f"paper.jar not found at {self.server_jar.name}",
                    "result": {}}

        eula_path = self.server_dir / "eula.txt"
        if eula_path.exists():
            try:
                if "eula=true" not in eula_path.read_text().lower():
                    return {"state": "failed", "error": "EULA not accepted",
                            "result": {}}
            except Exception:
                pass

        cmd = [
            "java",
            f"-Xms{self._java_xms}m",
            f"-Xmx{self._java_xmx}m",
            "-jar", str(self.server_jar),
            "nogui",
        ]
        try:
            # Open the log file for child stdout so Paper's own log file
            # (logs/latest.log) is still produced normally.
            log_dir = self.server_dir / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            logf = open(log_dir / "latest.log", "a")

            proc = subprocess.Popen(
                cmd,
                cwd=str(self.server_dir),
                stdin=subprocess.PIPE,
                stdout=logf,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self._console_proc = proc
        except Exception as e:
            return {"state": "failed", "error": f"Start failed: {e}",
                    "result": {}}

        # Verify the process survived the first seconds (failed start detection)
        deadline = time.time() + min(START_TIMEOUT_SECONDS, 10)
        while time.time() < deadline:
            if proc.poll() is not None:
                return {"state": "failed",
                        "error": f"Java exited during startup (code {proc.returncode})",
                        "result": {"exit_code": proc.returncode}}
            if self.find_paper_pid():
                break
            time.sleep(0.5)

        new_pid = self.find_paper_pid()
        if new_pid is None:
            # Process alive but pgrep hasn't matched yet — accept if still up
            if proc.poll() is not None:
                return {"state": "failed",
                        "error": "Server process not found after start",
                        "result": {}}
            new_pid = proc.pid

        return {"state": "success", "error": None,
                "result": {"pid": new_pid, "started": True}}

    def _stop(self) -> Dict[str, Any]:
        pid = self.find_paper_pid()
        if pid is None:
            return {"state": "success", "error": None,
                    "result": {"already_stopped": True}}

        # 1. Preferred: Minecraft's own `stop` → saves world gracefully.
        sent = self._write_console("stop")
        if sent:
            if self._wait_termination(pid, STOP_TIMEOUT_SECONDS):
                return {"state": "success", "error": None,
                        "result": {"method": "console stop", "pid": pid,
                                   "saved": True}}
            # fell through to signal path

        # 2. SIGTERM — Paper hooks this and shuts down cleanly (saves world).
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return {"state": "success", "error": None,
                    "result": {"note": "process already exited"}}
        except Exception as e:
            return {"state": "failed", "error": f"SIGTERM failed: {e}",
                    "result": {}}

        if self._wait_termination(pid, STOP_TIMEOUT_SECONDS):
            return {"state": "success", "error": None,
                    "result": {"method": "SIGTERM", "pid": pid, "saved": True}}

        # 3. Last resort after timeout: SIGKILL. World may lose last seconds;
        # report honestly rather than claiming a graceful stop.
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return {"state": "success", "error": None,
                    "result": {"note": "exited just after timeout"}}
        if self._wait_termination(pid, 10):
            return {"state": "success", "error": None,
                    "result": {"method": "SIGKILL", "pid": pid,
                               "saved": False,
                               "warning": "Forced kill — last moments may be unsaved"}}
        return {"state": "timeout", "error": "Process did not terminate",
                "result": {"pid": pid}}

    def _restart(self) -> Dict[str, Any]:
        # Controlled stop → verify termination → start.
        stop = self._stop()
        if stop["state"] != "success":
            return {"state": "failed",
                    "error": f"Stop phase failed: {stop.get('error')}",
                    "result": {"stop": stop}}

        pid = self.find_paper_pid()
        if pid is not None:
            if not self._wait_termination(pid, 15):
                return {"state": "failed",
                        "error": "Process still alive after stop phase",
                        "result": {"pid": pid}}

        # Small settle delay so ports free up before restart.
        time.sleep(2)

        start = self._start()
        start["result"] = dict(start.get("result") or {}, stop=stop.get("result"))
        return start

    def _console(self, command: str) -> Dict[str, Any]:
        ok, err = validate_console_command(command)
        if not ok:
            return {"state": "failed", "error": err, "result": {}}

        # `stop` through the console is a power action — route it properly.
        if command.strip().lower() == "stop":
            return self._stop()

        if self.find_paper_pid() is None:
            return {"state": "failed", "error": "Server is not running",
                    "result": {}}

        if not self._write_console(command):
            return {"state": "failed",
                    "error": "Console channel unavailable — agent did not start "
                             "this server process (notebook cell owns it). "
                             "Restart the server from the dashboard to attach "
                             "the console.",
                    "result": {}}
        return {"state": "success", "error": None,
                "result": {"command": command, "delivered": True}}

    def _wait_termination(self, pid: int, timeout: int) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self._pid_alive(pid) and self.find_paper_pid() is None:
                return True
            time.sleep(1)
        return False


# Singleton
action_executor = ActionExecutor()
