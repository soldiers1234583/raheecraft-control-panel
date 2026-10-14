#!/usr/bin/env python3
"""
RaheeCraft Agent Log Streamer
=============================
Tails Paper's logs/latest.log and reports only NEW lines to the backend.

Offset-based: the byte offset persists across reconnects within a session,
so a dropped connection never replays the whole log history to the backend.
"""

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.config import config

MAX_LINES_PER_PUSH = 200
MAX_BYTES_PER_PUSH = 64 * 1024


class LogStreamer:
    """Tracks a byte offset in latest.log and yields only new lines."""

    def __init__(self, log_path: Path = None):
        self.log_path = Path(log_path) if log_path else config.get_log_path()
        self._offset = 0
        self._initialized = False

    def _reset_if_rotated(self, size: int) -> None:
        """If the log shrank (rotation/new session), start from the top."""
        if size < self._offset:
            self._offset = 0

    def read_new_lines(self) -> List[str]:
        """Return new lines since the last call (empty list if none)."""
        try:
            if not self.log_path.exists():
                return []
            size = self.log_path.stat().st_size
            self._reset_if_rotated(size)

            if not self._initialized:
                # First read: only take the last 100 lines so an initial
                # connect doesn't push the entire session history.
                self._initialized = True
                if size == 0:
                    return []
                start = max(0, size - MAX_BYTES_PER_PUSH)
                with open(self.log_path, "r", errors="replace") as f:
                    f.seek(start)
                    data = f.read()
                lines = data.splitlines()[-100:]
                self._offset = size
                return [l for l in lines if l.strip()]

            if size <= self._offset:
                return []

            with open(self.log_path, "r", errors="replace") as f:
                f.seek(self._offset)
                data = f.read(MAX_BYTES_PER_PUSH)
                self._offset = f.tell()

            lines = [l for l in data.splitlines() if l.strip()]
            if len(lines) > MAX_LINES_PER_PUSH:
                lines = lines[-MAX_LINES_PER_PUSH:]
            return lines
        except Exception:
            return []

    def reset(self) -> None:
        """Forget the offset (next read initializes fresh)."""
        self._offset = 0
        self._initialized = False


def classify_level(line: str) -> str:
    """Classify a Paper log line as info/warn/error/debug."""
    lowered = line.lower()
    if "[error]" in lowered or " severe " in f" {lowered} ":
        return "error"
    if "[warn]" in lowered or "warning" in lowered:
        return "warn"
    if "[debug]" in lowered:
        return "debug"
    return "info"


def build_log_payload(streamer: LogStreamer) -> Dict[str, Any]:
    """Build the /api/agent/logs payload from new lines."""
    lines = streamer.read_new_lines()
    return {
        "lines": [
            {"text": l, "level": classify_level(l)}
            for l in lines
        ],
        "ts": time.time(),
    }


# Singleton
log_streamer = LogStreamer()
