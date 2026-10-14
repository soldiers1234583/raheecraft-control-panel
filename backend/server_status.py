"""
RaheeCraft Server Status
========================
Reads actual server state from the Kaggle filesystem.
"""

import os
import json
import subprocess
import time
from pathlib import Path
from typing import Optional, Dict, Any
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
    
    def is_paper_running(self) -> bool:
        """Check if Paper server process is running."""
        try:
            # Check for java processes with paper.jar
            result = subprocess.run(
                ["pgrep", "-f", "paper.jar"],
                capture_output=True,
                text=True
            )
            return result.returncode == 0 and result.stdout.strip()
        except Exception:
            # Alternative: check via ps
            try:
                result = subprocess.run(
                    ["ps", "aux"],
                    capture_output=True,
                    text=True
                )
                return "paper.jar" in result.stdout
            except Exception:
                return False
    
    def get_paper_pid(self) -> Optional[int]:
        """Get Paper server PID if running."""
        try:
            result = subprocess.run(
                ["pgrep", "-f", "paper.jar"],
                capture_output=True,
                text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.strip().split("\n")[0])
        except Exception:
            pass
        return None
    
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
        """Get complete server summary."""
        paper_running = self.is_paper_running()
        paper_pid = self.get_paper_pid() if paper_running else None
        
        return {
            "server_exists": self.server_exists(),
            "paper_jar_exists": self.paper_jar_exists(),
            "eula_accepted": self.eula_accepted(),
            "paper_running": paper_running,
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
