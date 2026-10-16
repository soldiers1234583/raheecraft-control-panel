#!/usr/bin/env python3
"""
RaheeCraft Agent Player Tracker (Stage 3)
=========================================
Reads real player state from Paper's own structured files and log lines.
No data is ever fabricated — unavailable values are reported as None.

Trusted sources (all maintained by Paper itself):
  - usercache.json        username -> UUID cache
  - whitelist.json        whitelist entries [{uuid, name}]
  - banned-players.json   ban entries [{uuid, name, created, source, reason, expires}]
  - ops.json              operator entries [{uuid, name, level, bypassesPlayerLimit}]
  - server.properties     white-list=true/false
  - plugins/LuckPerms*    LuckPerms presence/version
  - logs/latest.log       "X joined the game" / "X left the game" for sessions

Session tracking: join/leave lines observed while the agent runs give real
session start times. If the agent started after a player joined, the session
start is unknown and reported as None (never guessed).
"""

import json
import re
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

# --- Validation (mirrored server-side; agent re-validates before use) -------

USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{3,16}$')
UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
GROUP_RE = re.compile(r'^[a-z0-9_]{1,32}$')
MAX_REASON_LENGTH = 100


def valid_username(name: Any) -> bool:
    return isinstance(name, str) and bool(USERNAME_RE.match(name))


def valid_uuid(u: Any) -> bool:
    return isinstance(u, str) and bool(UUID_RE.match(u))


def sanitize_reason(reason: Any) -> str:
    """Return a safe reason string: printable, single-line, bounded.

    Control characters (including newlines) are stripped so a reason can
    never inject a second console line.
    """
    if not isinstance(reason, str):
        return ""
    reason = "".join(ch for ch in reason if ch.isprintable())
    reason = " ".join(reason.split())  # collapse whitespace
    return reason[:MAX_REASON_LENGTH]


# --- Log line patterns (Paper format) ---------------------------------------

# Paper: "[12:00:00] [Server thread/INFO]: Steve joined the game"
# Handle one or two leading bracket groups (console vs file format).
_PREFIX = r'^\[[^\]]+\]\s*(?:\[[^\]]+\]:\s*)?'
JOIN_RE = re.compile(_PREFIX + r'(?P<name>[A-Za-z0-9_]{3,16}) joined the game')
LEAVE_RE = re.compile(_PREFIX + r'(?P<name>[A-Za-z0-9_]{3,16}) left the game')
# "[Server thread/INFO]: There are 2 of a max of 20 players online: Steve, Alex"
LIST_RE = re.compile(
    r'There (?:are|are no)\s*\d*\s*(?:of a max of \d+ players? online:?)?\s*(?P<names>[A-Za-z0-9_, ]*)',
    re.I)


class PlayerTracker:
    """Tracks online players from log events and reads Paper's JSON state."""

    def __init__(self, server_dir: str = None):
        # Deferred import to avoid hard dependency in tests
        if server_dir is None:
            from agent.config import config
            server_dir = config.SERVER_DIR
        self.server_dir = Path(server_dir)
        self._lock = threading.Lock()
        # name -> {"joined_at": float|None, "uuid": str|None}
        self._online: Dict[str, Dict[str, Any]] = {}
        # Whether any join/leave/list line has been observed this run. Lets a
        # caller tell "nobody online" apart from "nothing observed yet".
        self._seen_event = False

    # --- log observation (called from the log streamer) ----------------------

    def observe_line(self, line: str) -> None:
        """Update online state from a Paper log line."""
        m = JOIN_RE.match(line)
        if m:
            name = m.group("name")
            with self._lock:
                self._seen_event = True
                entry = self._online.setdefault(
                    name, {"joined_at": None, "uuid": None})
                entry["joined_at"] = time.time()
                if entry.get("uuid") is None:
                    entry["uuid"] = self.lookup_uuid(name)
            return
        m = LEAVE_RE.match(line)
        if m:
            with self._lock:
                self._seen_event = True
                self._online.pop(m.group("name"), None)
            return
        # Reconcile from `list` output when available
        if "players online" in line:
            m = LIST_RE.search(line)
            if m:
                names = [n.strip() for n in m.group("names").split(",")
                         if valid_username(n.strip())]
                with self._lock:
                    self._seen_event = True
                    seen = set(self._online)
                    for name in names:
                        if name not in seen:
                            # Observed mid-session: start time unknown
                            self._online[name] = {
                                "joined_at": None,
                                "uuid": self.lookup_uuid(name),
                            }
                    for name in seen - set(names):
                        self._online.pop(name, None)

    def observe_lines(self, lines: List[str]) -> None:
        for line in lines:
            self.observe_line(line)

    def has_observations(self) -> bool:
        """True once a join/leave/list line has been seen this run.

        Lets a caller tell "nobody is online" apart from "nothing observed
        yet", so it can report None instead of a fabricated 0.
        """
        with self._lock:
            return self._seen_event

    # --- Paper structured file readers ---------------------------------------

    def _read_json(self, filename: str) -> Any:
        path = self.server_dir / filename
        try:
            if not path.exists():
                return None
            with open(path, "r") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    def lookup_uuid(self, username: str) -> Optional[str]:
        cache = self._read_json("usercache.json")
        if isinstance(cache, list):
            for entry in cache:
                if (isinstance(entry, dict)
                        and entry.get("name") == username
                        and valid_uuid(entry.get("uuid"))):
                    return entry["uuid"]
        return None

    def lookup_name(self, uuid: str) -> Optional[str]:
        cache = self._read_json("usercache.json")
        if isinstance(cache, list):
            for entry in cache:
                if isinstance(entry, dict) and entry.get("uuid") == uuid:
                    name = entry.get("name")
                    if valid_username(name):
                        return name
        return None

    def get_online(self) -> List[Dict[str, Any]]:
        """Online players with UUID and session start when observed."""
        with self._lock:
            snapshot = {k: dict(v) for k, v in self._online.items()}
        out = []
        for name, info in snapshot.items():
            joined_at = info.get("joined_at")
            out.append({
                "username": name,
                "uuid": info.get("uuid"),
                "session_started_at": joined_at,
                "session_seconds": (int(time.time() - joined_at)
                                    if joined_at else None),
            })
        out.sort(key=lambda p: p["username"].lower())
        return out

    def get_whitelist(self) -> Dict[str, Any]:
        enabled = None
        props_path = self.server_dir / "server.properties"
        try:
            if props_path.exists():
                for line in props_path.read_text().splitlines():
                    if line.startswith("white-list="):
                        enabled = line.split("=", 1)[1].strip().lower() == "true"
        except OSError:
            pass

        entries = []
        raw = self._read_json("whitelist.json")
        if isinstance(raw, list):
            for e in raw:
                if isinstance(e, dict) and valid_username(e.get("name")):
                    entries.append({
                        "username": e["name"],
                        "uuid": e.get("uuid") if valid_uuid(e.get("uuid")) else None,
                    })
        entries.sort(key=lambda e: e["username"].lower())
        return {"enabled": enabled, "entries": entries}

    def get_bans(self) -> List[Dict[str, Any]]:
        out = []
        raw = self._read_json("banned-players.json")
        if isinstance(raw, list):
            for e in raw:
                if not isinstance(e, dict):
                    continue
                name = e.get("name")
                # Offline-player bans may have uuid but no cached name
                if name is not None and not valid_username(name):
                    name = None
                out.append({
                    "username": name,
                    "uuid": e.get("uuid") if valid_uuid(e.get("uuid")) else None,
                    "reason": sanitize_reason(e.get("reason")) or "No reason",
                    "created": e.get("created"),
                    "expires": e.get("expires"),
                    "source": sanitize_reason(e.get("source"))[:32] or None,
                })
        out.sort(key=lambda b: (b["username"] or b["uuid"] or "").lower())
        return out

    def get_ops(self) -> List[Dict[str, Any]]:
        out = []
        raw = self._read_json("ops.json")
        if isinstance(raw, list):
            for e in raw:
                if isinstance(e, dict):
                    name = e.get("name")
                    out.append({
                        "username": name if valid_username(name) else None,
                        "uuid": e.get("uuid") if valid_uuid(e.get("uuid")) else None,
                        "level": e.get("level"),
                    })
        out.sort(key=lambda o: (o["username"] or o["uuid"] or "").lower())
        return out

    def detect_luckperms(self) -> Dict[str, Any]:
        """Detect LuckPerms without executing anything.

        installed: jar or data directory present.
        operational: jar present AND the plugin data dir exists (created on
        first successful load), our best proxy for "loaded and running".
        """
        plugins = self.server_dir / "plugins"
        jar = None
        version = None
        try:
            if plugins.is_dir():
                for p in plugins.glob("LuckPerms*.jar"):
                    jar = p.name
                    m = re.search(r'(\d+\.\d+(?:\.\d+)?)', p.stem)
                    if m:
                        version = m.group(1)
                    break
        except OSError:
            pass
        data_dir = (plugins / "LuckPerms").is_dir() if plugins.is_dir() else False
        return {
            "installed": bool(jar) or data_dir,
            "operational": bool(jar) and data_dir,
            "jar": jar,
            "version": version,
        }

    def snapshot(self) -> Dict[str, Any]:
        """Full player-state snapshot for the heartbeat payload."""
        return {
            "online": self.get_online(),
            "whitelist": self.get_whitelist(),
            "bans": self.get_bans(),
            "ops": self.get_ops(),
            "luckperms": self.detect_luckperms(),
            "ts": time.time(),
        }


# Singleton
player_tracker = PlayerTracker()
