#!/usr/bin/env python3
"""
RaheeCraft Backend Validation (Stage 3)
========================================
Shared validators for player-management requests. The backend validates
BEFORE queueing (fail fast for the user) and the agent re-validates
before executing (defense in depth).

Mirrors agent/player_tracker.py — keep the regexes in sync.
"""

import re
from typing import Any, Optional, Tuple

USERNAME_RE = re.compile(r'^[A-Za-z0-9_]{3,16}$')
UUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)
GROUP_RE = re.compile(r'^[a-z0-9_]{1,32}$')
MAX_REASON_LENGTH = 100


def valid_username(name: Any) -> bool:
    """Minecraft Java username: 3-16 chars, letters/digits/underscore."""
    return isinstance(name, str) and bool(USERNAME_RE.match(name))


def valid_uuid(u: Any) -> bool:
    return isinstance(u, str) and bool(UUID_RE.match(u))


def sanitize_reason(reason: Any) -> str:
    """Return a safe reason: printable, single-line, bounded.

    Control characters (including newlines) are stripped so a reason can
    never inject a second console line, and length is capped.
    """
    if not isinstance(reason, str):
        return ""
    reason = "".join(ch for ch in reason if ch.isprintable())
    reason = " ".join(reason.split())
    return reason[:MAX_REASON_LENGTH]


def validate_player_request(data: Any, require_reason: bool = False
                            ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """Validate a {username, reason} request body.

    Returns (username, reason, error). Exactly one of username/reason is
    None when error is None; error is set (and both None) when invalid.
    """
    if not isinstance(data, dict):
        return None, None, "Request body must be a JSON object"

    username = data.get("username")
    if not valid_username(username):
        return None, None, ("Invalid username — use 3-16 characters: "
                            "letters, digits, underscore")

    reason = sanitize_reason(data.get("reason"))
    if require_reason and not reason:
        return None, None, "A reason is required"
    if data.get("reason") is not None and not isinstance(data.get("reason"), str):
        return None, None, "Reason must be a string"
    if isinstance(data.get("reason"), str) and len(data["reason"]) > 500:
        return None, None, "Reason too long (max 500 chars raw)"

    return username, reason, None
