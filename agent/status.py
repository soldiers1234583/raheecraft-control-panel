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
        "playit_running": False,
        "playit_address": None,
        "drive_last_backup": None,
        "cpu_percent": None,
        "memory_percent": None,
        "player_count": None,
        "uptime_seconds": 0
    }
    
    # Check Paper server
    try:
        status["paper_running"], status["paper_pid"] = check_paper_process()
    except Exception as e:
        logger_debug(f"Error checking Paper: {e}")
    
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
    
    # Uptime (simplified)
    status["uptime_seconds"] = get_uptime()
    
    return status


def check_paper_process() -> tuple[bool, Optional[int]]:
    """Check if Paper server is running."""
    import subprocess
    
    try:
        # Look for java process with paper.jar
        result = subprocess.run(
            ["pgrep", "-f", "paper.jar"],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0 and result.stdout.strip():
            pid = int(result.stdout.strip().split('\n')[0])
            return True, pid
        return False, None
    except Exception:
        # Fallback: try ps
        try:
            result = subprocess.run(
                ["ps", "aux"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if "paper.jar" in result.stdout:
                # Parse PID
                for line in result.stdout.split('\n'):
                    if 'paper.jar' in line and 'grep' not in line:
                        parts = line.split()
                        if len(parts) > 1:
                            return True, int(parts[1])
            return False, None
        except Exception:
            return False, None


def check_playit_tunnel() -> tuple[bool, Optional[str]]:
    """Check if Playit agent is running and get address."""
    playit_dir = Path("/kaggle/working/playit")
    
    # Check if agent process is running
    try:
        import subprocess
        result = subprocess.run(
            ["pgrep", "-f", "playit"],
            capture_output=True,
            text=True,
            timeout=5
        )
        running = result.returncode == 0 and result.stdout.strip()
    except Exception:
        running = False
    
    if not running:
        return False, None
    
    # Try to get address from agent output or config
    # In Stage 1, we rely on the agent having stored the address
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
    
    # Check if we can get address from PlayitSetup
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
    """Get current player count from server."""
    try:
        # Try to read from server log or use RCON
        # For Stage 1, we'll check if server is running and assume 0
        # Full implementation would parse logs or use RCON
        server_dir = Path(config.SERVER_DIR)
        log_path = server_dir / "logs" / "latest.log"
        
        if log_path.exists():
            with open(log_path, 'r') as f:
                content = f.read()
                # Look for "joined the game" messages
                # This is simplified - real implementation would parse properly
                count = content.count("joined the game")
                return max(0, count)
    except Exception:
        pass
    
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
