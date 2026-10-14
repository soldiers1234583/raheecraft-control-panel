#!/usr/bin/env python3
"""
RaheeCraft Log Buffer
=====================
In-memory ring buffer of Paper console lines streamed by the Kaggle agent.

Lines carry monotonically increasing sequence numbers so the dashboard can
tail with ?since=<seq> and reconnects never replay the whole history.
Sensitive values are redacted before storage.
"""

import time
import threading
from collections import deque
from typing import Any, Dict, List, Optional

from backend.auth import redact_text

MAX_LINES = 2000
MAX_LINE_LENGTH = 500


class LogBuffer:
    """Thread-safe ring buffer of log lines with sequence numbers."""

    def __init__(self, max_lines: int = MAX_LINES):
        self._lines: deque = deque(maxlen=max_lines)
        self._seq = 0
        self._lock = threading.Lock()
        self.last_agent_push: Optional[float] = None

    def append(self, text: str, source: str = "agent",
               level: Optional[str] = None) -> Dict[str, Any]:
        """Append one log line; returns the stored record."""
        line = str(text)[:MAX_LINE_LENGTH]
        # Redact secret-looking patterns (tokens, keys, passwords) before storing
        line = redact_text(line)
        with self._lock:
            self._seq += 1
            record = {
                "seq": self._seq,
                "ts": time.time(),
                "source": source,
                "level": level or _classify_level(line),
                "text": line,
            }
            self._lines.append(record)
            if source == "agent":
                self.last_agent_push = time.time()
            return record

    def extend(self, lines: List[Any], source: str = "agent") -> int:
        """Append many lines (strings or {text, level} dicts). Returns count."""
        n = 0
        for item in lines:
            if isinstance(item, dict):
                text = item.get("text", "")
                level = item.get("level")
            else:
                text, level = item, None
            if text:
                self.append(text, source=source, level=level)
                n += 1
        return n

    def tail(self, since: int = 0, limit: int = 200,
             level: Optional[str] = None,
             search: Optional[str] = None) -> Dict[str, Any]:
        """Return lines after sequence `since`, optionally filtered."""
        with self._lock:
            snapshot = list(self._lines)
            current_seq = self._seq
        result = [r for r in snapshot if r["seq"] > since]
        if level:
            result = [r for r in result if r["level"] == level]
        if search:
            needle = search.lower()
            result = [r for r in result if needle in r["text"].lower()]
        return {
            "lines": result[-limit:],
            "seq": current_seq,
            "count": len(result),
            "last_agent_push": self.last_agent_push,
        }

    def clear(self) -> None:
        """Clear the view (client-side clear does not need this; server keeps
        history for reconnects — used only by tests/maintenance)."""
        with self._lock:
            self._lines.clear()


def _classify_level(line: str) -> str:
    """Classify Paper/Minecraft log lines (handles [Server thread/ERROR] style)."""
    lowered = line.lower()
    if ("error]" in lowered or "/error" in lowered
            or " severe " in f" {lowered} "
            or lowered.startswith("error")):
        return "error"
    if "warn]" in lowered or "/warn" in lowered or "warning" in lowered:
        return "warn"
    if "debug]" in lowered or "/debug" in lowered:
        return "debug"
    return "info"


# Singleton
log_buffer = LogBuffer()
