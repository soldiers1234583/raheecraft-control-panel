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

Lifecycle ownership
-------------------
Power actions are NOT implemented here. They are delegated to
`agent.lifecycle.ServerLifecycle`, the single component that owns the Paper
process (state machine, desired_state, readiness, crash recovery). This
executor keeps the security surface — allowlists, argument validation,
confirmation against Paper's own state files — and nothing else, so two parts
of the system can never each start their own JVM.
"""

import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.config import config

# --- Allowlists -------------------------------------------------------------

ALLOWED_ACTION_TYPES = {
    "start_server", "stop_server", "restart_server", "console_command",
    # Stage 3 — player management (all executed via the Paper console
    # channel with validated arguments; results confirmed against
    # Paper's own JSON state files, never assumed).
    "kick_player", "ban_player", "unban_player",
    "whitelist_add", "whitelist_remove", "whitelist_enable", "whitelist_disable",
    "op_grant", "op_revoke",
}

# Commands Stage 3 may construct dynamically. Arguments are validated
# separately (username regex, sanitized reasons) before joining — never
# shell-interpolated, never eval'd.
PLAYER_ACTION_COMMANDS = {
    "kick_player": "kick",
    "ban_player": "ban",
    "unban_player": "pardon",
    "whitelist_add": "whitelist add",
    "whitelist_remove": "whitelist remove",
    "whitelist_enable": "whitelist on",
    "whitelist_disable": "whitelist off",
    "op_grant": "op",
    "op_revoke": "deop",
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

# Timing for start/stop lives in `agent.lifecycle` and is read from the
# environment at call time, so a notebook can raise the limits after import:
#   START_TIMEOUT_SECONDS   readiness window (default 300; first-run world
#                           generation is slow)
#   STOP_TIMEOUT_SECONDS    graceful-shutdown window (default 180; Paper 1.21.x
#                           saves every dimension and unwinds its worker pools
#                           after the console `stop` is accepted)


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
        self._server_dir_override = Path(server_dir) if server_dir else None

    # --- lifecycle delegation ----------------------------------------------

    @property
    def server_dir(self) -> Path:
        """Server directory, re-read from config unless explicitly overridden.

        Previously this was cached at import, so a late change to
        `config.SERVER_DIR` was silently ignored and the executor operated on a
        stale path.
        """
        return self._server_dir_override or Path(config.SERVER_DIR)

    @server_dir.setter
    def server_dir(self, value) -> None:
        self._server_dir_override = Path(value) if value else None

    @property
    def server_jar(self) -> Path:
        return self.server_dir / "paper.jar"

    @property
    def lifecycle(self):
        """The one lifecycle manager for this server directory."""
        from agent.lifecycle import get_lifecycle
        return get_lifecycle(self._server_dir_override)

    # --- process helpers ----------------------------------------------------

    def find_paper_pid(self) -> Optional[int]:
        """Find the Paper server PID (paper.jar), or None.

        Delegates to the canonical detector, which requires a real `java`
        process with this server's `paper.jar` on its argv. A bare substring
        match (the old `pgrep -f paper.jar`) wrongly matched editors, greps,
        and the shell that launched the check, and made a stopped server look
        running.
        """
        from agent.process_info import find_paper_pid
        return find_paper_pid(self.server_dir)

    # --- console channel ----------------------------------------------------

    def _write_console(self, line: str) -> bool:
        """Write a line to Paper's console stdin (owned by the lifecycle)."""
        return self.lifecycle.send_console(line)

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
            # Stage 3 player management
            if action_type in PLAYER_ACTION_COMMANDS:
                return self._player_action(action_type, params)
        except Exception as e:
            return {"state": "failed", "error": str(e), "result": {}}

        return {"state": "failed", "error": "Unhandled action", "result": {}}

    def _start(self) -> Dict[str, Any]:
        """Start the server through the lifecycle manager (idempotent)."""
        return self.lifecycle.start(reason="dashboard")

    def _stop(self) -> Dict[str, Any]:
        """Graceful stop through the lifecycle manager."""
        return self.lifecycle.stop(reason="dashboard")

    def _restart(self) -> Dict[str, Any]:
        """Controlled restart through the lifecycle manager."""
        return self.lifecycle.restart_once()

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
                    "error": "Console channel unavailable — the server is not "
                             "running, or this process did not start it. Start "
                             "the server from the dashboard (or run the "
                             "server cell) so the console is attached.",
                    "result": {}}
        return {"state": "success", "error": None,
                "result": {"command": command, "delivered": True}}

    # --- Stage 3: player management ----------------------------------------

    def _player_action(self, action_type: str, params: dict) -> Dict[str, Any]:
        """Execute a validated player-management action.

        The command is assembled from a fixed template plus strictly
        validated arguments (username regex / sanitized reason) — no
        user string is ever passed to a shell or eval. After each
        mutation the resulting state is confirmed by re-reading Paper's
        own JSON files instead of assuming success.
        """
        from agent.player_tracker import (
            player_tracker, valid_username, sanitize_reason,
        )

        needs_username = action_type not in (
            "whitelist_enable", "whitelist_disable")
        username = params.get("username")
        if needs_username and not valid_username(username):
            return {"state": "failed",
                    "error": "Invalid username (3-16 chars: letters, digits, _)",
                    "result": {}}

        reason = sanitize_reason(params.get("reason"))

        if self.find_paper_pid() is None:
            return {"state": "failed", "error": "Server is not running",
                    "result": {}}

        # Build command from template + validated tokens
        base = PLAYER_ACTION_COMMANDS[action_type]
        if needs_username:
            parts = [base, username]
            if action_type in ("kick_player", "ban_player") and reason:
                parts.append(reason)
            command = " ".join(parts)
        else:
            command = base

        # Precondition checks against real state (idempotency + safety)
        pre = self._player_precondition(action_type, username, player_tracker)
        if pre is not None:
            return pre

        if not self._write_console(command):
            return {"state": "failed",
                    "error": "Console channel unavailable — restart the "
                             "server from the dashboard to attach the console.",
                    "result": {}}

        # Allow the server to apply the change before confirming
        import time as _time
        _time.sleep(2)

        return self._confirm_player_action(action_type, username, player_tracker)

    def _player_precondition(self, action_type: str, username: str,
                             tracker) -> Optional[Dict[str, Any]]:
        """Idempotent checks before sending the command."""
        if action_type == "kick_player":
            online_names = [p["username"] for p in tracker.get_online()]
            if username not in online_names:
                return {"state": "failed",
                        "error": f"{username} is not online",
                        "result": {}}
        elif action_type == "unban_player":
            banned = tracker.get_bans()
            names = {b["username"] for b in banned if b["username"]}
            uuids = {b["uuid"] for b in banned if b["uuid"]}
            u = tracker.lookup_uuid(username) if username else None
            if username not in names and u not in uuids:
                return {"state": "success", "error": None,
                        "result": {"already": True,
                                   "note": "player was not banned"}}
        elif action_type == "ban_player":
            banned = tracker.get_bans()
            names = {b["username"] for b in banned if b["username"]}
            if username in names:
                return {"state": "success", "error": None,
                        "result": {"already": True,
                                   "note": "player already banned"}}
        elif action_type == "whitelist_add":
            entries = {e["username"] for e in
                       tracker.get_whitelist()["entries"]}
            if username in entries:
                return {"state": "success", "error": None,
                        "result": {"already": True,
                                   "note": "already whitelisted"}}
        elif action_type == "whitelist_remove":
            entries = {e["username"] for e in
                       tracker.get_whitelist()["entries"]}
            if username not in entries:
                return {"state": "success", "error": None,
                        "result": {"already": True,
                                   "note": "player was not whitelisted"}}
        elif action_type == "op_revoke":
            ops = tracker.get_ops()
            op_names = {o["username"] for o in ops if o["username"]}
            # Safeguard: never remove the last operator
            if username in op_names and len(op_names) <= 1:
                return {"state": "failed",
                        "error": "Refusing to revoke the last operator "
                                 "(would lock out administrative access)",
                        "result": {}}
        elif action_type == "op_grant":
            ops = tracker.get_ops()
            op_names = {o["username"] for o in ops if o["username"]}
            if username in op_names:
                return {"state": "success", "error": None,
                        "result": {"already": True,
                                   "note": "player is already an operator"}}
        return None

    def _confirm_player_action(self, action_type: str, username: str,
                               tracker) -> Dict[str, Any]:
        """Confirm the mutation by re-reading Paper's state files."""
        if action_type == "whitelist_enable" or action_type == "whitelist_disable":
            want = action_type == "whitelist_enable"
            got = tracker.get_whitelist()["enabled"]
            if got is None:
                return {"state": "success", "error": None,
                        "result": {"sent": True,
                                   "confirmed": False,
                                   "note": "could not read server.properties"}}
            if got == want:
                return {"state": "success", "error": None,
                        "result": {"confirmed": True, "enabled": got}}
            return {"state": "failed",
                    "error": "Whitelist toggle did not take effect",
                    "result": {"enabled": got}}

        if action_type == "whitelist_add":
            entries = {e["username"] for e in
                       tracker.get_whitelist()["entries"]}
            if username in entries:
                return {"state": "success", "error": None,
                        "result": {"confirmed": True}}
            return {"state": "failed",
                    "error": "Player did not appear in whitelist.json "
                             "(name may need to have joined the server once)",
                    "result": {}}

        if action_type == "whitelist_remove":
            entries = {e["username"] for e in
                       tracker.get_whitelist()["entries"]}
            if username not in entries:
                return {"state": "success", "error": None,
                        "result": {"confirmed": True}}
            return {"state": "failed",
                    "error": "Player still present in whitelist.json",
                    "result": {}}

        if action_type == "ban_player":
            names = {b["username"] for b in tracker.get_bans()
                     if b["username"]}
            if username in names:
                return {"state": "success", "error": None,
                        "result": {"confirmed": True}}
            return {"state": "failed",
                    "error": "Ban not found in banned-players.json "
                             "(player may be unknown to the server)",
                    "result": {}}

        if action_type == "unban_player":
            names = {b["username"] for b in tracker.get_bans()
                     if b["username"]}
            if username not in names:
                return {"state": "success", "error": None,
                        "result": {"confirmed": True}}
            return {"state": "failed",
                    "error": "Player still present in banned-players.json",
                    "result": {}}

        if action_type == "op_grant":
            names = {o["username"] for o in tracker.get_ops()
                     if o["username"]}
            if username in names:
                return {"state": "success", "error": None,
                        "result": {"confirmed": True}}
            return {"state": "failed",
                    "error": "Player did not appear in ops.json",
                    "result": {}}

        if action_type == "op_revoke":
            names = {o["username"] for o in tracker.get_ops()
                     if o["username"]}
            if username not in names:
                return {"state": "success", "error": None,
                        "result": {"confirmed": True}}
            return {"state": "failed",
                    "error": "Player still present in ops.json",
                    "result": {}}

        if action_type == "kick_player":
            online = [p["username"] for p in tracker.get_online()]
            if username not in online:
                return {"state": "success", "error": None,
                        "result": {"confirmed": True}}
            return {"state": "failed",
                    "error": f"{username} still appears online",
                    "result": {}}

        return {"state": "failed", "error": "Unhandled player action",
                "result": {}}


# Singleton
action_executor = ActionExecutor()
