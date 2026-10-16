"""
RaheeCraft Server Status
========================
Filesystem/process facts about the *host this backend runs on*.

Scope warning (important)
-------------------------
In the deployed topology the backend lives somewhere else (Render) while Paper
runs inside the Kaggle runtime. Nothing here can therefore observe the real
server: a `pgrep` on the backend host can only ever answer "is there a
Minecraft server on THIS box", which is normally no.

The authoritative server state comes from the agent heartbeat
(`/api/agent/heartbeat` → `AgentHandler`), and `/api/status` labels every field
with where it came from. These local readings are kept for the same-host case
(panel + agent in one Kaggle kernel) and are always reported under
`scope: "backend_host"` so no caller can mistake them for the live server.
"""

import os
import sys
import json
import subprocess
import time
from pathlib import Path
from typing import Optional, Dict, Any, Tuple
from datetime import datetime

# Server directory (from environment or default)
SERVER_DIR = os.environ.get("SERVER_DIR", "/kaggle/working/minecraft_server")


class ServerStatus:
    """Gathers actual server state."""
    
    def __init__(self, server_dir: str = SERVER_DIR):
        self.server_dir = Path(server_dir)
    
    def server_exists(self) -> bool:
        """Check if server directory exists."""
        return self.server_dir.exists()
    
    def eula_accepted(self) -> bool:
        """Check if EULA was accepted."""
        eula_path = self.server_dir / "eula.txt"
        if not eula_path.exists():
            return False
        try:
            content = eula_path.read_text()
            return "eula=true" in content.lower()
        except Exception:
            return False
    
    def server_properties(self) -> Optional[Dict[str, str]]:
        """Read server.properties."""
        props_path = self.server_dir / "server.properties"
        if not props_path.exists():
            return None
        try:
            props = {}
            for line in props_path.read_text().splitlines():
                if "=" in line and not line.startswith("#"):
                    key, val = line.split("=", 1)
                    props[key.strip()] = val.strip()
            return props
        except Exception:
            return None
    
    def paper_jar_exists(self) -> bool:
        """Check if Paper server jar exists."""
        return (self.server_dir / "paper.jar").exists()
    
    def get_paper_version(self) -> Optional[str]:
        """Try to get Paper version from jar filename or properties."""
        jar_path = self.server_dir / "paper.jar"
        if jar_path.exists():
            return jar_path.stem  # "paper-1.21.4-496" etc
        return None
    
    def get_minecraft_version(self) -> str:
        """Get Minecraft version from config or properties."""
        props = self.server_properties()
        if props and "minecraft-version" in props:
            return props["minecraft-version"]
        return os.environ.get("SERVER_VERSION", "1.21.4")
    
    def _find_local_paper_pid(self) -> Optional[int]:
        """PID of a Paper JVM on THIS host, using strict matching.

        The old implementation ran `pgrep -f paper.jar`, which matches any
        process whose command line merely mentions that string (an editor, a
        grep, a shell). The canonical detector requires a java process whose
        argv names this server's jar.
        """
        try:
            from agent.process_info import find_paper_pid
            return find_paper_pid(self.server_dir)
        except Exception:
            pass

        # Fallback for a deployment without the agent package installed: scan
        # /proc argv for a java process that names paper.jar as an argument.
        try:
            if not Path("/proc").is_dir():
                return None
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    raw = (entry / "cmdline").read_bytes()
                except OSError:
                    continue
                argv = [p.decode("utf-8", "replace")
                        for p in raw.split(b"\x00") if p]
                if not argv:
                    continue
                if os.path.basename(argv[0]).lower().startswith("java") and \
                        any(a.endswith("paper.jar") for a in argv[1:]):
                    return int(entry.name)
        except Exception:
            return None
        return None

    def is_paper_running(self) -> bool:
        """Is a Paper server running on THIS backend host?"""
        return self._find_local_paper_pid() is not None

    def get_paper_pid(self) -> Optional[int]:
        """PID of the Paper server on THIS backend host, if any."""
        return self._find_local_paper_pid()
    
    def get_log_path(self) -> Path:
        """Paper's log file on THIS host (`logs/latest.log`, the same file the
        agent tails). `LOG_FILE` overrides it, mirroring the agent's config.

        Callers use this for the log-history endpoints; the live console is
        served from the agent's streamed log buffer instead.
        """
        override = os.environ.get("LOG_FILE", "").strip()
        if override:
            return Path(override)
        return self.server_dir / "logs" / "latest.log"

    def get_world_dir(self) -> Path:
        """Get the world directory path."""
        return self.server_dir / "world"
    
    def world_exists(self) -> bool:
        """Check if world directory exists."""
        return self.get_world_dir().exists()
    
    def get_last_backup_info(self) -> Optional[Dict[str, Any]]:
        """
        Try to get last backup info.
        Reads from Drive backup module if available.
        """
        try:
            sys.path.insert(0, str(Path.cwd()))
            from drive_backup import GoogleDriveBackup
            drive = GoogleDriveBackup(dry_run=False)
            backups = drive.list_backups()
            if backups:
                return {
                    "last_backup": backups[0].get("name"),
                    "last_modified": backups[0].get("modifiedTime"),
                    "size_mb": int(backups[0].get("size", 0)) / (1024 * 1024)
                }
        except Exception:
            pass
        return None
    
    def get_playit_status(self) -> Optional[Dict[str, Any]]:
        """
        Try to get Playit status.
        Reads from Playit agent if available.
        """
        try:
            sys.path.insert(0, str(Path.cwd()))
            from playit_setup import PlayitTunnel
            tunnel = PlayitTunnel(dry_run=False)
            address = tunnel.get_public_address()
            if address:
                return {
                    "running": True,
                    "hostname": address[0],
                    "port": address[1]
                }
        except Exception:
            pass
        return {"running": False}
    
    def get_server_summary(self) -> Dict[str, Any]:
        """Local (backend-host) facts. Never the authority on the Kaggle server."""
        paper_pid = self._find_local_paper_pid()
        paper_running = paper_pid is not None

        return {
            "scope": "backend_host",
            "server_dir": str(self.server_dir),
            "server_exists": self.server_exists(),
            "paper_jar_exists": self.paper_jar_exists(),
            "eula_accepted": self.eula_accepted(),
            # Kept for backwards compatibility with the dashboard/API shape;
            # callers that care about the *live* server must use the agent
            # heartbeat, which /api/status merges in.
            "paper_running": paper_running,
            "paper_running_local": paper_running,
            "paper_pid": paper_pid,
            "minecraft_version": self.get_minecraft_version(),
            "paper_version": self.get_paper_version(),
            "world_exists": self.world_exists(),
            "properties": self.server_properties(),
            "last_backup": self.get_last_backup_info(),
            "playit": self.get_playit_status(),
            "timestamp": datetime.now().isoformat()
        }


# Singleton
server_status = ServerStatus()
