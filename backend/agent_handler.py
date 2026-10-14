"""
RaheeCraft Agent Handler
=========================
Handles agent registration, heartbeat, and status reporting.
"""

import time
import json
from typing import Optional, Dict, Any
from backend.config import config
from backend.auth import redact_for_log


class AgentHandler:
    """Handles agent lifecycle and status."""
    
    def __init__(self):
        self.agents = {}  # agent_id -> agent_info
    
    def register_agent(self, agent_id: str, secret: str, metadata: Dict[str, Any] = None) -> bool:
        """Register a new agent. Returns True if successful."""
        if agent_id in self.agents:
            return False  # Already registered
        
        self.agents[agent_id] = {
            "secret": secret,
            "status": "registered",
            "metadata": metadata or {},
            "last_heartbeat": None,
            "last_seen": time.time(),
            "paper_status": "unknown",
            "playit_status": "unknown",
            "drive_status": "unknown",
            "cpu_percent": None,
            "memory_percent": None,
            "player_count": None,
            "uptime_seconds": 0,
            "player_snapshot": None
        }
        return True
    
    def verify_agent(self, agent_id: str, secret: str) -> bool:
        """Verify agent credentials."""
        agent = self.agents.get(agent_id)
        if not agent:
            return False
        return agent["secret"] == secret
    
    def heartbeat(self, agent_id: str, secret: str, status: Dict[str, Any]) -> bool:
        """
        Process agent heartbeat.
        
        Status dict can include:
        - paper_running: bool
        - paper_pid: int
        - playit_running: bool
        - playit_address: str
        - drive_last_backup: str (ISO timestamp)
        - cpu_percent: float
        - memory_percent: float
        - player_count: int
        - uptime_seconds: int
        """
        if not self.verify_agent(agent_id, secret):
            return False
        
        agent = self.agents[agent_id]
        agent["last_heartbeat"] = time.time()
        agent["last_seen"] = time.time()
        agent["status"] = "online"
        
        # Update status fields
        if "paper_running" in status:
            agent["paper_status"] = "running" if status["paper_running"] else "stopped"
        if "paper_pid" in status:
            agent["paper_pid"] = status["paper_pid"]
        if "playit_running" in status:
            agent["playit_status"] = "running" if status["playit_running"] else "stopped"
        if "playit_address" in status:
            agent["playit_address"] = status["playit_address"]
        if "drive_last_backup" in status:
            agent["drive_last_backup"] = status["drive_last_backup"]
        if "cpu_percent" in status:
            agent["cpu_percent"] = status["cpu_percent"]
        if "memory_percent" in status:
            agent["memory_percent"] = status["memory_percent"]
        if "player_count" in status:
            agent["player_count"] = status["player_count"]
        if "uptime_seconds" in status:
            agent["uptime_seconds"] = status["uptime_seconds"]
        # Stage 3: player snapshot (online/whitelist/bans/ops/luckperms).
        # Caps enforced so a rogue agent cannot balloon backend memory.
        if isinstance(status.get("player_snapshot"), dict):
            agent["player_snapshot"] = _sanitize_snapshot(
                status["player_snapshot"])
            agent["player_snapshot_ts"] = status["player_snapshot"].get("ts")
        
        return True
    
    def get_agent_status(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """Get agent status (without secret)."""
        agent = self.agents.get(agent_id)
        if not agent:
            return None
        
        return {
            "agent_id": agent_id,
            "status": agent["status"],
            "last_heartbeat": agent["last_heartbeat"],
            "last_seen": agent["last_seen"],
            "paper_status": agent["paper_status"],
            "paper_pid": agent.get("paper_pid"),
            "playit_status": agent["playit_status"],
            "playit_address": agent.get("playit_address"),
            "drive_last_backup": agent.get("drive_last_backup"),
            "cpu_percent": agent["cpu_percent"],
            "memory_percent": agent["memory_percent"],
            "player_count": agent["player_count"],
            "uptime_seconds": agent["uptime_seconds"],
            "player_snapshot": agent.get("player_snapshot"),
            "player_snapshot_ts": agent.get("player_snapshot_ts"),
            "metadata": agent["metadata"]
        }
    
    def get_all_agents_status(self) -> list:
        """Get status for all agents."""
        result = []
        for agent_id, agent in self.agents.items():
            status = self.get_agent_status(agent_id)
            if status:
                result.append(status)
        return result
    
    def is_agent_online(self, agent_id: str) -> bool:
        """Check if agent is online (heartbeated within timeout)."""
        agent = self.agents.get(agent_id)
        if not agent:
            return False
        
        timeout = 90  # 3 * 30 seconds default
        if agent["last_heartbeat"]:
            return time.time() - agent["last_heartbeat"] < timeout
        return False
    
    def redact_for_log(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Redact sensitive data for logging."""
        if "secret" in data:
            data = data.copy()
            data["secret"] = redact_for_log(data["secret"])
        return data


def _sanitize_snapshot(snap: dict) -> dict:
    """Keep only known keys with bounded list sizes from the agent."""
    out = {"ts": snap.get("ts")}
    def _cap_list(key, limit=200):
        v = snap.get(key)
        return v[:limit] if isinstance(v, list) else []
    out["online"] = _cap_list("online", 200)
    out["bans"] = _cap_list("bans", 500)
    out["ops"] = _cap_list("ops", 100)
    wl = snap.get("whitelist")
    if isinstance(wl, dict):
        entries = wl.get("entries")
        out["whitelist"] = {
            "enabled": wl.get("enabled"),
            "entries": entries[:500] if isinstance(entries, list) else [],
        }
    else:
        out["whitelist"] = {"enabled": None, "entries": []}
    lp = snap.get("luckperms")
    out["luckperms"] = lp if isinstance(lp, dict) else {
        "installed": False, "operational": False}
    return out


agent_handler = AgentHandler()
