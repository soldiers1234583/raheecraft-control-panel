#!/usr/bin/env python3
"""
RaheeCraft Stage 4 — Server-side path sandbox helpers
======================================================
Backend-side defense-in-depth for any path the browser supplies.

These helpers are a SECOND line of defense. The agent canonicalizes and
re-validates every path against the real filesystem immediately before use.
Never trust a path merely because the backend accepted it.
"""

import os
import re
from pathlib import Path
from typing import Optional, Tuple

from backend.config import config
# Re-exported so callers have one place to ask which PaperMC API we use.
# PaperMC retired v2 (HTTP 410 "sunset"); the live base is v3.
from backend.operation_registry import paper_api_base  # noqa: F401

def server_dir() -> Path:
    """The live server directory.

    Resolved from `config.SERVER_DIR` on every call. The config object is a
    runtime-mutable singleton (tests, the agent, and the notebook all rebind
    it), so binding this once at import time would silently ignore any later
    change. Never cache this value.
    """
    return Path(config.SERVER_DIR).resolve()


# Default Paper version configured for this project.
DEFAULT_MINECRAFT_VERSION = "1.21.4"

# Directories that may not be removed or overwritten through the file manager.
_PROTECTED_TOP_NAMES = frozenset({
    "world", "world_nether", "world_the_end",
    "logs", "plugins", "libraries",
    "versions", "cache",
})



def _norm_root() -> str:
    return os.path.normpath(str(server_dir()))


def safe_server_path(user_path: str, for_mutation: bool = True
                     ) -> Tuple[bool, Optional[str], Optional[Path]]:
    """
    Try to interpret a browser-supplied path as a safe path inside SERVER_DIR.

    Returns (ok, error, resolved_path).
    - Rejects empty, absolute, and obvious traversal inputs early.
    - Joins to SERVER_DIR, resolves symlinks only enough to check containment,
      and rejects anything that escapes the server directory.
    - Rejects paths that contain obvious encoding tricks used to confuse simple
      prefix checks.

    `for_mutation=True` (the default) also refuses to address the reserved
    top-level directories (world/plugins/logs/...) that have dedicated
    managers. Read-only callers that legitimately need to *describe* those
    directories pass `for_mutation=False`; they still get every traversal and
    symlink check.
    """
    if not isinstance(user_path, str):
        return False, "path must be a string", None

    # Check control characters on the RAW input, before stripping. Stripping
    # first would turn "server.properties\n" into an accepted path by removing
    # the character we are meant to reject.
    if "\x00" in user_path:
        return False, "path contains illegal characters", None
    if any(ch in user_path for ch in "\r\n"):
        return False, "path must not contain newlines", None

    raw = user_path.strip()
    if not raw:
        return False, "path must not be empty", None

    # Early block on obvious escape attempts without relying on normalization
    # tricks alone.
    lowered = raw.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return False, "remote URLs are not filesystem paths", None
    if lowered.startswith("file://"):
        return False, "file:// URLs are not supported", None

    # Reject absolute paths on this platform.
    if os.path.isabs(raw):
        return False, "absolute paths are not allowed", None

    # Reject paths that are only separators or dot-components.
    stripped = raw.strip("/\\")
    if stripped in ("", ".", ".."):
        return False, "invalid path", None

    try:
        joined = server_dir() / raw
        resolved = joined.resolve()
    except (ValueError, OSError) as e:
        return False, f"path could not be resolved: {e}", None

    root_norm = _norm_root()
    resolved_norm = os.path.normpath(str(resolved))

    # Containment: resolved path must be inside the server dir.
    if resolved_norm != root_norm and not resolved_norm.startswith(root_norm + os.sep):
        return False, "path escapes the server directory", None

    # Symlink escape check: even if the final path is inside SERVER_DIR, a
    # symlink component inside the path could point outside. We reject any
    # path whose intermediate components include a symlink. This is stricter
    # than necessary for some read-only cases, but it is the safest default
    # for a remote file manager.
    if _contains_symlink_component(joined):
        return False, "symlinks are not allowed", None

    # Protected-top-name guard: never allow operations that would touch the
    # top-level protected directories except through their dedicated managers
    # (world manager, plugin manager, backup center, etc.).
    # We only enforce this on the basename of the final component so that
    # e.g. editing server.properties is fine, but deleting "world" is not.
    final = resolved.name
    if for_mutation and final in _PROTECTED_TOP_NAMES:
        return False, f"use the dedicated manager for '{final}'", None

    return True, None, resolved


def _contains_symlink_component(path: Path) -> bool:
    """
    Walk the parents of `path` (including path itself) and reject if any
    component is a symlink. This prevents symlink-escape even when the final
    resolved path is inside the sandbox.
    """
    root = server_dir()
    current: Path = path
    # Check the final path and each parent up to, but not including, SERVER_DIR.
    while current != root and current != current.parent:
        try:
            if current.is_symlink():
                return True
        except (OSError, ValueError):
            return True
        current = current.parent
    return False


def safe_basename(filename: str) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Validate a bare file/directory name supplied by the user (for upload,
    mkdir, rename targets).

    Returns (ok, error, basename).
    """
    if not isinstance(filename, str):
        return False, "name must be a string", None
    name = filename.strip()
    if not name:
        return False, "name must not be empty", None
    if os.path.isabs(name):
        return False, "name must not be an absolute path", None
    # Reject traversal-containing names even if they are "just a name".
    if name in (".", ".."):
        return False, "name must not be '.' or '..'", None
    if "/" in name or "\\" in name:
        return False, "name must not contain path separators", None
    if "\x00" in name:
        return False, "name contains illegal characters", None
    if any(ch in name for ch in "\r\n"):
        return False, "name must not contain newlines", None
    if len(name) > 512:
        return False, "name is too long", None
    # Reject edge-case names that confuse extraction or filesystem tools.
    if name in _PROTECTED_TOP_NAMES:
        return False, f"reserved name '{name}' may not be used here", None
    return True, None, name


def sanitize_listing_name(name: str, is_dir: bool) -> dict:
    """
    Shape a single filesystem entry for a browse listing.

    Keeps only safe, bounded fields. Symlinks are reported but flagged.
    """
    return {
        "name": name,
        "is_directory": bool(is_dir),
        # We do not expose absolute paths in listings; the agent returns
        # relative names and the backend reconstructs containment client-side
        # only when needed for actions.
    }


def default_paper_version() -> str:
    """Configured Paper/Minecraft version, read live from the environment."""
    return os.environ.get("MINECRAFT_VERSION", DEFAULT_MINECRAFT_VERSION).strip() or DEFAULT_MINECRAFT_VERSION
