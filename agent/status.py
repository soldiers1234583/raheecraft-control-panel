#!/usr/bin/env python3
"""
RaheeCraft Agent Status Reporter
==================================
Gathers server status and prepares heartbeat data.
"""

import os
import sys
import json
import subprocess
import time
from pathlib import Path
from typing import Dict, Any, Optional

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.config import config


def get_agent_status() -> Dict[str, Any]:
    """
    Gather current agent and server status.
    
    Returns dict with:
    - paper_running: bool
    - paper_pid: int or None
    - playit_running: bool
    - playit_address: str or None
    - drive_last_backup: str (ISO timestamp) or None
    - cpu_percent: float or None
    - memory_percent: float or None
    - player_count: int or None
    - uptime_seconds: int
    """
    status = {
        "paper_running": False,
        "paper_pid": None,
        "paper_startup_state": "unknown",
        "server_state": "STOPPED",
        "desired_state": "STOPPED",
        "exit_code": None,
        "restart_attempts": 0,
        "last_error": None,
        "playit_running": False,
        "playit_address": None,
        "drive_last_backup": None,
        "cpu_percent": None,
        "memory_percent": None,
        "player_count": None,
        "uptime_seconds": 0
    }
    
    # Check Paper server. The lifecycle manager is authoritative: it knows the
    # state machine (STARTING/RUNNING/STOPPING/CRASHED), whether the server was
    # meant to be up, and why it last died. The canonical detector is used only
    # to notice a server started outside the manager.
    try:
        status.update(lifecycle_status())
    except Exception as e:
        logger_debug(f"Error reading lifecycle status: {e}")
        try:
            status["paper_running"], status["paper_pid"] = check_paper_process()
            status["paper_startup_state"] = get_paper_startup_state()
        except Exception as e2:
            logger_debug(f"Error checking Paper: {e2}")
    
    # Check Playit tunnel
    try:
        status["playit_running"], status["playit_address"] = check_playit_tunnel()
    except Exception as e:
        logger_debug(f"Error checking Playit: {e}")
    
    # Check Google Drive backup
    try:
        status["drive_last_backup"] = get_drive_last_backup()
    except Exception as e:
        logger_debug(f"Error checking Drive backup: {e}")
    
    # System metrics (best effort)
    try:
        status["cpu_percent"] = get_cpu_percent()
    except Exception:
        pass
    
    try:
        status["memory_percent"] = get_memory_percent()
    except Exception:
        pass
    
    # Player count from server
    try:
        status["player_count"] = get_player_count()
    except Exception:
        pass
    
    # Never report uptime for a server that is not up: this is measured from
    # the moment Paper itself reported ready.
    status["uptime_seconds"] = (
        int(status.get("uptime_seconds") or 0) if status["paper_running"] else 0)
    
    return status


def lifecycle_status() -> Dict[str, Any]:
    """Authoritative server state from the one lifecycle manager.

    `paper_running`, `paper_pid` and `paper_startup_state` stay in place because
    the backend and dashboard already consume them; `server_state` and
    `desired_state` are what the dashboard renders as the real power state.
    """
    from agent.lifecycle import lifecycle

    reported = lifecycle.status()
    detected_pid = None
    try:
        from agent.process_info import find_paper_pid
        detected_pid = find_paper_pid(config.SERVER_DIR)
    except Exception:
        detected_pid = None

    state = reported["server_state"]
    note = None
    if detected_pid is not None and state in ("STOPPED", "CRASHED"):
        # A JVM is running that the manager did not start (started by hand, or
        # by a previous kernel). Report it truthfully rather than "stopped".
        state = "RUNNING"
        note = "server process detected but not started by the lifecycle manager"

    out = {
        "paper_running": detected_pid is not None,
        "paper_pid": detected_pid if detected_pid is not None
                      else reported.get("paper_pid"),
        "server_state": state,
        "desired_state": reported["desired_state"],
        "paper_startup_state": (
            "ready" if state == "RUNNING"
            else reported.get("paper_startup_state", "unknown")),
        "restart_attempts": reported.get("restart_attempts", 0),
        "restart_scheduled_in": reported.get("restart_scheduled_in"),
        "exit_code": reported.get("exit_code"),
        "last_error": reported.get("last_error"),
        "ready_marker": reported.get("ready_marker"),
        "uptime_seconds": (int(reported.get("uptime_seconds") or 0)
                           if detected_pid is not None else 0),
    }
    if note:
        out["state_note"] = note
    return out


def check_paper_process() -> tuple[bool, Optional[int]]:
    """Check if Paper server is running.

    Uses the canonical detector so a stopped server is never reported as
    running just because some unrelated command line mentions "paper.jar".
    """
    try:
        from agent.process_info import find_paper_pid
        pid = find_paper_pid(config.SERVER_DIR)
        return (pid is not None), pid
    except Exception:
        return False, None


def get_paper_startup_state() -> str:
    """Paper's startup state from its own log: ready/failed/loading/unknown.

    Separate from liveness — a JVM can be alive while Minecraft is still
    loading, and the dashboard must not claim players can join until Paper
    itself says so.
    """
    try:
        from agent.process_info import paper_startup_state
        return paper_startup_state(config.SERVER_DIR,
                                   config.get_log_path())["state"]
    except Exception:
        return "unknown"


def check_playit_tunnel() -> tuple[bool, Optional[str]]:
    """Check if Playit agent is running and get its public address.

    The address is discovered by whichever process actually started the
    connector (the notebook cell) and written to a shared file. This function
    runs in the agent's thread with its own PlayitTunnel instance, which never
    started a connector, so it can only read that file — a fresh instance's
    in-memory address is always None, which is why the panel used to show the
    tunnel as running with no address.
    """
    playit_dir = Path("/kaggle/working/playit")

    # Executable-name detection only: a substring match would report a tunnel
    # whenever any process (or this file, being edited) mentions "playit".
    try:
        from agent.process_info import find_playit_pid
        running = find_playit_pid() is not None
    except Exception:
        running = False
    
    if not running:
        return False, None
    
    # The real address, as reported by the connector and persisted for us.
    # Never invented: no file (or no real host:port in it) means unknown.
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from playit_setup import read_public_address
        address = read_public_address()
        if address:
            return True, address
    except Exception:
        pass

    # Fallbacks for older layouts: a legacy config.json, then a same-process
    # PlayitTunnel (only useful if this process itself started the connector).
    config_path = playit_dir / "config.json"
    if config_path.exists():
        try:
            with open(config_path) as f:
                config_data = json.load(f)
                address = config_data.get("public_address")
                if address:
                    return True, address
        except Exception:
            pass
    
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from playit_setup import PlayitTunnel
        tunnel = PlayitTunnel(dry_run=False)
        address = tunnel.get_public_address()
        if address:
            return True, f"{address[0]}:{address[1]}"
    except Exception:
        pass
    
    return True, None  # Process running but address unknown


def get_drive_last_backup() -> Optional[str]:
    """Get timestamp of last Google Drive backup."""
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from drive_backup import GoogleDriveBackup
        
        drive = GoogleDriveBackup(dry_run=False)
        backups = drive.list_backups()
        
        if backups:
            # Return ISO timestamp
            return backups[0].get("modifiedTime")
    except Exception:
        pass
    
    return None


def get_cpu_percent() -> Optional[float]:
    """Get CPU usage percentage."""
    try:
        import psutil
        return psutil.cpu_percent(interval=0.1)
    except ImportError:
        # Fallback: read from /proc/stat
        try:
            with open('/proc/stat', 'r') as f:
                line = f.readline()
                parts = line.split()
                if parts[0] == 'cpu':
                    # Simple calculation
                    idle = int(parts[4])
                    total = sum(int(x) for x in parts[1:])
                    return (total - idle) / total * 100 if total > 0 else 0
        except Exception:
            pass
    except Exception:
        pass
    
    return None


def get_memory_percent() -> Optional[float]:
    """Get memory usage percentage."""
    try:
        import psutil
        mem = psutil.virtual_memory()
        return mem.percent
    except ImportError:
        # Fallback: read from /proc/meminfo
        try:
            with open('/proc/meminfo', 'r') as f:
                lines = f.readlines()
            
            mem_total = None
            mem_available = None
            
            for line in lines:
                if line.startswith('MemTotal:'):
                    mem_total = int(line.split()[1])
                elif line.startswith('MemAvailable:'):
                    mem_available = int(line.split()[1])
                    break
            
            if mem_total and mem_available:
                used = mem_total - mem_available
                return (used / mem_total) * 100
        except Exception:
            pass
    
    return None


def get_player_count() -> Optional[int]:
    """Players currently online, from the tracker's real join/leave state.

    This used to count every "joined the game" line in latest.log, which spans
    the whole session: it reported the number of joins ever seen (a stale,
    ever-growing number), not who is online now. The Stage 3 tracker is fed by
    the same log stream (join/leave/list) and is the authoritative source; when
    it has observed nothing to base an answer on, return None rather than 0 or a
    derived guess.
    """
    try:
        from agent.player_tracker import player_tracker
        if not player_tracker.has_observations():
            return None
        return len(player_tracker.get_online())
    except Exception:
        return None


def get_uptime() -> int:
    """Get agent uptime in seconds."""
    # In Stage 1, we return 0 (agent uptime tracked by backend)
    return 0


def logger_debug(msg: str):
    """Simple debug logging."""
    import logging
    logging.getLogger(__name__).debug(msg)


if __name__ == "__main__":
    # Test when run directly
    status = get_agent_status()
    print(json.dumps(status, indent=2))
