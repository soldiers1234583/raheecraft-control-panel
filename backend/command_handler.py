#!/usr/bin/env python3
"""
RaheeCraft Command Handler
==========================
Handles server power actions (start, stop, restart) and console commands.

Security:
  - All actions require authenticated admin
  - Strict allowlist of supported operations
  - Action tracking with IDs for idempotency
  - No shell execution, no arbitrary commands
"""

import os
import time
import uuid
import subprocess
import re
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from enum import Enum

from backend.config import config

# Server directory (from environment or default, same as server_status.py)
SERVER_DIR = os.environ.get("SERVER_DIR", "/kaggle/working/minecraft_server")


class ActionState(Enum):
    """Possible states for a power action."""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class ActionResult(dict):
    """Result of a power action."""
    
    def __init__(self, action_id: str, action_type: str):
        super().__init__()
        self["action_id"] = action_id
        self["action_type"] = action_type
        self["state"] = ActionState.PENDING.value
        self["created_at"] = time.time()
        self["started_at"] = None
        self["completed_at"] = None
        self["error"] = None
        self["result"] = {}
    
    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "action_id": self["action_id"],
            "action_type": self["action_type"],
            "state": self["state"],
            "created_at": self["created_at"],
            "started_at": self["started_at"],
            "completed_at": self["completed_at"],
            "error": self["error"],
            "result": self["result"]
        }


class CommandHandler:
    """
    Handles server power commands and console commands.
    
    Security:
    - Strict allowlist of commands
    - No shell execution
    - Action tracking for idempotency
    - Graceful shutdown preferred
    """
    
    # Allowed console commands (Minecraft server commands)
    ALLOWED_COMMANDS = {
        "list", "say", "time", "weather", "difficulty",
        "gamemode", "whitelist", "save-all", "stop",
        "op", "deop", "ban", "pardon", "kick",
        "give", "tp", "effect", "xp", "clear",
        "unban", "blockdata", "entitydata",
        "scoreboard", "title", "me", "tell",
        "trigger", "worldborder", "recipe", "summon",
        "setidletimeout", "setworldspawn", "spreadplayers",
        "save-off", "save-on"
    }
    
    # Commands that require the server to be running
    SERVER_COMMANDS = {
        "list", "say", "time", "weather", "difficulty",
        "gamemode", "whitelist", "save-all", "stop",
        "op", "deop", "ban", "pardon", "kick",
        "give", "tp", "effect", "xp", "clear",
        "enchant", "unban", "scoreboard", "title",
        "me", "tell", "trigger", "worldborder",
        "recipe", "summon", "setidletimeout", "setworldspawn"
    }
    
    def __init__(self):
        self.actions: Dict[str, ActionResult] = {}
        self._server_process: Optional[subprocess.Popen] = None
        self._server_dir: str = SERVER_DIR
        self._server_jar: str = str(Path(self._server_dir) / "paper.jar")
        self._java_xms: str = os.environ.get("JAVA_XMS", "1024")
        self._java_xmx: str = os.environ.get("JAVA_XMX", "2048")
    
    def _get_action(self, action_id: str) -> Optional[ActionResult]:
        """Get an action by ID."""
        return self.actions.get(action_id)
    
    def _create_action(self, action_type: str) -> ActionResult:
        """Create a new action."""
        action_id = str(uuid.uuid4())
        action = ActionResult(action_id, action_type)
        self.actions[action_id] = action
        return action
    
    def _complete_action(self, action_id: str, state: ActionState, 
                         error: Optional[str] = None, result: Optional[dict] = None):
        """Mark an action as complete."""
        action = self._get_action(action_id)
        if action:
            action["state"] = state.value
            action["completed_at"] = time.time()
            if error:
                action["error"] = error
            if result:
                action["result"] = result
    
    def _check_paper_running(self) -> tuple[bool, Optional[int]]:
        """Check if this server's Paper JVM is running on this host.

        Uses the canonical detector (requires a real `java` process with this
        server's `paper.jar` on its argv). The previous `pgrep -f paper.jar`
        matched any process whose command line mentioned the substring, and so
        reported a stopped server as running.

        NOTE: the real server runs on Kaggle, not on the backend host. This
        local check exists only for the fallback executor below; the
        authoritative lifecycle is the agent's `ActionExecutor`, reached via
        `queue_for_agent` / `complete_from_agent`.
        """
        try:
            from agent.process_info import find_paper_pid
            pid = find_paper_pid(self._server_dir)
            return (pid is not None), pid
        except Exception:
            return False, None
    
    def _start_paper(self, action: ActionResult) -> bool:
        """Refuse to start a Minecraft server on the backend host.

        The server runs on Kaggle and is owned by the agent's lifecycle
        manager (`agent.lifecycle.ServerLifecycle`), which is the only
        component allowed to spawn a JVM. This Stage-1 fallback is deliberately
        a no-op: a second launcher could open the same world concurrently,
        which is precisely what the single authoritative lifecycle prevents.

        Power actions reach the server through the agent relay
        (`queue_for_agent` / `complete_from_agent`).
        """
        # Check if already running
        running, pid = self._check_paper_running()
        if running:
            action["result"] = {"already_running": True, "pid": pid}
            return True

        # Check if server jar exists
        if not Path(self._server_jar).exists():
            action["error"] = "paper.jar not found"
            return False

        action["error"] = (
            "Refusing to start Minecraft on the backend host. The server is "
            "owned by the agent's lifecycle manager on the Kaggle runtime; "
            "register the agent and use Start, which relays to it.")
        return False
    
    def _stop_paper_graceful(self, action: ActionResult) -> bool:
        """
        Stop Paper server gracefully.
        
        First tries 'stop' command via log injection, then SIGTERM, then SIGKILL.
        """
        running, pid = self._check_paper_running()
        if not running:
            action["result"] = {"already_stopped": True}
            return True
        
        # Method 1: Send stop command via log (simplest for Paper)
        # Paper reads from stdin, but we can't easily write to it
        # So we use SIGTERM which triggers graceful shutdown
        
        try:
            # Send SIGTERM for graceful shutdown
            os.killpg(os.getpgid(pid), 15)  # SIGTERM
            action["result"] = {"signal": "SIGTERM", "pid": pid}
            
            # Wait for the process to die, using the canonical detector rather
            # than `pgrep -f paper.jar`, which matched any process whose command
            # line merely mentioned the jar.
            for _ in range(30):
                time.sleep(1)
                if not self._check_paper_running()[0]:
                    break
            
            # Check if still running
            still_running, _ = self._check_paper_running()
            if still_running:
                # Force kill
                try:
                    os.killpg(os.getpgid(pid), 9)  # SIGKILL
                    action["result"]["force_killed"] = True
                except Exception as e:
                    action["error"] = f"Force kill failed: {e}"
                    return False
            
            return True
            
        except ProcessLookupError:
            # Process already died
            action["result"] = {"note": "Process already terminated"}
            return True
        except Exception as e:
            action["error"] = str(e)
            return False
    
    def start_server(self) -> ActionResult:
        """Start the Paper server."""
        action = self._create_action("start_server")
        
        try:
            action["started_at"] = time.time()
            
            if not Path(self._server_jar).exists():
                self._complete_action(
                    action["action_id"],
                    ActionState.FAILED,
                    error="paper.jar not found in server directory"
                )
                return action
            
            success = self._start_paper(action)
            
            if success:
                self._complete_action(
                    action["action_id"],
                    ActionState.SUCCESS,
                    result=action["result"]
                )
            else:
                self._complete_action(
                    action["action_id"],
                    ActionState.FAILED,
                    error=action.get("error")
                )
        except Exception as e:
            self._complete_action(
                action["action_id"],
                ActionState.FAILED,
                error=str(e)
            )
        
        return action
    
    def stop_server(self) -> ActionResult:
        """Stop the Paper server gracefully."""
        action = self._create_action("stop_server")
        
        try:
            action["started_at"] = time.time()
            
            running, pid = self._check_paper_running()
            if not running:
                self._complete_action(
                    action["action_id"],
                    ActionState.SUCCESS,
                    result={"already_stopped": True}
                )
                return action
            
            success = self._stop_paper_graceful(action)
            
            if success:
                self._complete_action(
                    action["action_id"],
                    ActionState.SUCCESS,
                    result=action["result"]
                )
            else:
                self._complete_action(
                    action["action_id"],
                    ActionState.FAILED,
                    error=action.get("error")
                )
        except Exception as e:
            self._complete_action(
                action["action_id"],
                ActionState.FAILED,
                error=str(e)
            )
        
        return action
    
    def restart_server(self) -> ActionResult:
        """Restart the Paper server (stop then start)."""
        action = self._create_action("restart_server")
        sub_actions = []
        
        try:
            action["started_at"] = time.time()
            
            # Check current state
            running, _ = self._check_paper_running()
            
            # Stop if running
            if running:
                stop_action = self.stop_server()
                sub_actions.append(stop_action)
                
                if stop_action["state"] != ActionState.SUCCESS.value:
                    self._complete_action(
                        action["action_id"],
                        ActionState.FAILED,
                        error=f"Stop failed: {stop_action.get('error')}"
                    )
                    return action
                
                # Wait for clean shutdown
                for _ in range(30):
                    if not self._check_paper_running()[0]:
                        break
                    time.sleep(1)
            
            # Start server
            start_action = self.start_server()
            sub_actions.append(start_action)
            
            if start_action["state"] == ActionState.SUCCESS.value:
                self._complete_action(
                    action["action_id"],
                    ActionState.SUCCESS,
                    result={
                        "stop_actions": [a.to_dict() for a in sub_actions[:-1]],
                        "start_action": start_action.to_dict()
                    }
                )
            else:
                self._complete_action(
                    action["action_id"],
                    ActionState.FAILED,
                    error=f"Start failed: {start_action.get('error')}",
                    result={
                        "stop_actions": [a.to_dict() for a in sub_actions[:-1]],
                        "start_action": start_action.to_dict()
                    }
                )
        except Exception as e:
            self._complete_action(
                action["action_id"],
                ActionState.FAILED,
                error=str(e)
            )
        
        return action
    
    def send_console_command(self, command: str) -> ActionResult:
        """
        Send a console command to the Paper server.
        
        This is limited to safe Minecraft server commands.
        For Stage 2, we use a simplified approach - writing to server log
        or using RCON if configured.
        """
        action = self._create_action("console_command")
        
        try:
            action["started_at"] = time.time()
            
            # Validate command
            cmd_lower = command.lower().strip()
            base_cmd = cmd_lower.split()[0] if cmd_lower else ""
            
            if base_cmd not in self.ALLOWED_COMMANDS:
                self._complete_action(
                    action["action_id"],
                    ActionState.FAILED,
                    error=f"Command '{base_cmd}' not allowed"
                )
                return action
            
            # Check if server is running
            running, _ = self._check_paper_running()
            if not running:
                self._complete_action(
                    action["action_id"],
                    ActionState.FAILED,
                    error="Server is not running"
                )
                return action
            
            # For Stage 2, we write command to server log
            # Paper reads commands from the console, but we can't easily
            # access the process stdin. A proper implementation would use:
            # 1. RCON (if configured)
            # 2. Write to a command file that the server reads
            # 3. Attach to process stdin
            
            # For now, we write to a commands file that the server can read
            cmd_file = Path(self._server_dir) / "commands.txt"
            try:
                with open(cmd_file, 'a') as f:
                    f.write(command + "\n")
                action["result"] = {
                    "command": command,
                    "written_to": str(cmd_file)
                }
                self._complete_action(
                    action["action_id"],
                    ActionState.SUCCESS,
                    result=action["result"]
                )
            except Exception as e:
                self._complete_action(
                    action["action_id"],
                    ActionState.FAILED,
                    error=f"Failed to write command: {e}"
                )
            
        except Exception as e:
            self._complete_action(
                action["action_id"],
                ActionState.FAILED,
                error=str(e)
            )
        
        return action
    
    def get_action(self, action_id: str) -> Optional[Dict[str, Any]]:
        """Get action status (including relay params/agent_id)."""
        action = self._get_action(action_id)
        if action:
            return _base_action_dict(action)
        return None
    
    def list_actions(self, limit: int = 50) -> List[Dict[str, Any]]:
        """List recent actions."""
        actions = sorted(
            self.actions.values(),
            key=lambda a: a.get("created_at", 0),
            reverse=True
        )
        return [_base_action_dict(a) for a in actions[:limit]]
    
    def cancel_action(self, action_id: str) -> bool:
        """Cancel a pending action."""
        action = self._get_action(action_id)
        if action and action["state"] in (ActionState.PENDING.value, ActionState.RUNNING.value):
            action["state"] = ActionState.CANCELLED.value
            action["completed_at"] = time.time()
            return True
        return False


# Singleton
command_handler = CommandHandler()


# =============================================================================
# Agent Relay (Stage 2)
# =============================================================================
# Paper runs on Kaggle; the backend only queues actions and the agent
# executes them. Agent polls POST /api/agent/poll and reports results to
# POST /api/agent/actions/<id>/result.

ACTION_TIMEOUT_SECONDS = int(os.environ.get("ACTION_TIMEOUT_SECONDS", "120"))
ACTION_QUEUE_LIMIT = 20


def _base_action_dict(action: ActionResult) -> dict:
    """Full action dict including Stage 2 relay fields (to_dict omits them)."""
    d = action.to_dict()
    d["params"] = dict(action.get("params") or {})
    d["agent_id"] = action.get("agent_id")
    d["dispatched_at"] = action.get("dispatched_at")
    return d


class ActionRelay:
    """Queues power/console actions for delivery to the Kaggle agent."""

    def queue_for_agent(self, action_type: str, params: dict,
                        agent_id: str = None) -> dict:
        """Create a pending action addressed to an agent. Rejects unknown types."""
        allowed = {
            "start_server", "stop_server", "restart_server", "console_command",
            # Stage 3 — player management (validated again by the agent)
            "kick_player", "ban_player", "unban_player",
            "whitelist_add", "whitelist_remove",
            "whitelist_enable", "whitelist_disable",
            "op_grant", "op_revoke",
        }
        if action_type not in allowed:
            raise ValueError(f"Unknown action type: {action_type}")

        # Reject duplicate in-flight actions of the same type
        for existing in self.actions.values():
            if (existing["action_type"] == action_type
                    and existing["state"] in ("pending", "running")
                    and existing.get("agent_id") == agent_id
                    and existing.get("params") == params):
                return _base_action_dict(existing)

        # Prevent overfilling the queue
        inflight = [a for a in self.actions.values()
                    if a["state"] in ("pending", "running")]
        if len(inflight) >= ACTION_QUEUE_LIMIT:
            raise RuntimeError("Action queue is full")

        action = self._create_action(action_type)
        action["params"] = dict(params or {})
        action["agent_id"] = agent_id
        action["dispatched_at"] = None
        return _base_action_dict(action)

    def pop_for_agent(self, agent_id: str, limit: int = 3) -> list:
        """Return pending actions for an agent and mark them running."""
        out = []
        for action in sorted(self.actions.values(),
                             key=lambda a: a.get("created_at", 0)):
            if (action.get("agent_id") in (agent_id, None)
                    and action["state"] == "pending"):
                action["state"] = ActionState.RUNNING.value
                action["dispatched_at"] = time.time()
                if action["agent_id"] is None:
                    action["agent_id"] = agent_id
                out.append(_base_action_dict(action))
                if len(out) >= limit:
                    break
        return out

    def complete_from_agent(self, action_id: str, state: str, error: str = None,
                            result: dict = None) -> dict:
        """Agent reports final state of an action."""
        valid = {"success", "failed", "timeout", "cancelled"}
        if state not in valid:
            raise ValueError(f"Invalid terminal state: {state}")
        action = self._get_action(action_id)
        if not action:
            raise KeyError(f"Action not found: {action_id}")
        if action["state"] in ("success", "failed", "timeout", "cancelled"):
            return _base_action_dict(action)  # idempotent replay
        self._complete_action(action_id, ActionState(state),
                              error=error, result=result)
        return _base_action_dict(action)

    def sweep_timeouts(self, timeout: int = None) -> int:
        """Mark dispatched actions that exceeded the timeout. Returns count."""
        timeout = timeout or ACTION_TIMEOUT_SECONDS
        now = time.time()
        n = 0
        for action in self.actions.values():
            if action["state"] != ActionState.RUNNING.value:
                continue
            started = action.get("dispatched_at") or action.get("started_at") or 0
            if now - started > timeout:
                action["state"] = ActionState.TIMEOUT.value
                action["completed_at"] = now
                action["error"] = f"Timed out after {timeout}s"
                n += 1
        return n

    def get_params(self, action_id: str) -> dict:
        action = self._get_action(action_id)
        return dict(action.get("params", {})) if action else {}


# Mix relay methods into CommandHandler and expose a singleton
CommandHandler.queue_for_agent = ActionRelay.queue_for_agent
CommandHandler.pop_for_agent = ActionRelay.pop_for_agent
CommandHandler.complete_from_agent = ActionRelay.complete_from_agent
CommandHandler.sweep_timeouts = ActionRelay.sweep_timeouts
CommandHandler.get_params = ActionRelay.get_params
