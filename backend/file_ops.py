#!/usr/bin/env python3
"""
RaheeCraft Stage 4 — Backend read-only server file helpers
===========================================================
Descriptions of the server tree and safe metadata, derived ONLY from
`backend.config.config.SERVER_DIR`. No user-supplied path is ever trusted.

The browser never asks "give me /etc/passwd" and gets it because the backend
said so — the backend only ever returns what the agent reported from a
sandboxed browse, or what these read-only helpers derive from the configured
server directory.
"""

import json
import os
import stat
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.config import config
from backend.paths import safe_server_path, server_dir


def server_root() -> Path:
    """Live server root, resolved from config on every call."""
    return server_dir()


def server_dir_exists() -> bool:
    return server_dir().exists()


def describe_root() -> Dict[str, Any]:
    """
    Safe, read-only summary of the configured server directory for the
    file-manager root. Does not traverse outside SERVER_DIR.
    """
    if not server_dir_exists():
        return {
            "exists": False,
            "path": str(server_dir()),
            "error": "server directory does not exist on this host",
        }

    entries = []
    try:
        with os.scandir(server_dir()) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                is_dir = stat.S_ISDIR(st.st_mode)
                entries.append({
                    "name": entry.name,
                    "is_directory": is_dir,
                    "size": st.st_size if not is_dir else None,
                    "modified": st.st_mtime if not is_dir else None,
                    "is_symlink": entry.is_symlink(),
                })
    except OSError as e:
        return {
            "exists": True,
            "path": str(server_dir()),
            "error": f"could not list server directory: {e}",
            "entries": [],
        }

    entries.sort(key=lambda e: (not e["is_directory"], e["name"].lower()))
    return {
        "exists": True,
        "path": str(server_dir()),
        "entries": entries,
        "count": len(entries),
    }


def describe_directory(relative_path: str) -> Dict[str, Any]:
    """
    Read-only listing for a path inside SERVER_DIR.

    `relative_path` is validated through `safe_server_path` before any
    filesystem access. If it escapes the sandbox, we return an error — we do
    not list anything.

    Read-only, so the mutation-only reserved-name guard is disabled: the file
    manager must be able to list `world/` etc. Every traversal and symlink
    check still applies.
    """
    ok, err, resolved = safe_server_path(relative_path, for_mutation=False)
    if not ok:
        return {"error": err, "path": relative_path, "ok": False}

    if not resolved.exists():
        return {"error": "path does not exist", "path": relative_path, "ok": False}
    if not resolved.is_dir():
        return {"error": "path is not a directory", "path": relative_path, "ok": False}

    entries: List[Dict[str, Any]] = []
    try:
        with os.scandir(resolved) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                is_dir = stat.S_ISDIR(st.st_mode)
                entries.append({
                    "name": entry.name,
                    "is_directory": is_dir,
                    "size": st.st_size if not is_dir else None,
                    "modified": st.st_mtime if not is_dir else None,
                    "is_symlink": entry.is_symlink(),
                })
    except OSError as e:
        return {"error": f"could not list directory: {e}", "path": relative_path, "ok": False}

    entries.sort(key=lambda e: (not e["is_directory"], e["name"].lower()))
    return {
        "ok": True,
        "path": relative_path,
        "resolved": str(resolved),
        "entries": entries,
        "count": len(entries),
    }


def describe_file(relative_path: str) -> Dict[str, Any]:
    """
    Read-only metadata for a single file inside SERVER_DIR. Does not return
    the file contents — use the agent file_read for that.
    """
    ok, err, resolved = safe_server_path(relative_path, for_mutation=False)
    if not ok:
        return {"error": err, "path": relative_path, "ok": False}

    try:
        st = resolved.lstat()
    except OSError as e:
        return {"error": f"could not stat file: {e}", "path": relative_path, "ok": False}

    if stat.S_ISDIR(st.st_mode):
        return {"error": "path is a directory", "path": relative_path, "ok": False}

    return {
        "ok": True,
        "path": relative_path,
        "is_file": True,
        "is_directory": False,
        "size": st.st_size,
        "modified": st.st_mtime,
        "is_symlink": resolved.is_symlink(),
        "name": resolved.name,
        "extension": "".join(Path(resolved.name).suffixes).lower(),
    }


def human_size(size_bytes: Optional[int]) -> Optional[str]:
    if size_bytes is None:
        return None
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def modify_time_iso(mtime: Optional[float]) -> Optional[str]:
    if mtime is None:
        return None
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(mtime))


def disk_usage_for_tree(root_relative: str) -> Dict[str, Any]:
    """
    Bounded, read-only disk usage for a directory tree inside SERVER_DIR.

    Intentionally bounded: we never walk the entire disk, and we cap the walk
    after a reasonable number of entries so large world dirs do not stall the
    backend.
    """
    ok, err, resolved = safe_server_path(root_relative)
    if not ok:
        return {"error": err, "path": root_relative, "ok": False}
    if not resolved.is_dir():
        return {"error": "not a directory", "path": root_relative, "ok": False}

    total_size = 0
    file_count = 0
    dir_count = 0
    max_entries = 200000

    try:
        for dirpath, dirnames, filenames in os.walk(resolved):
            if file_count + len(filenames) > max_entries:
                dirnames.clear()
                continue
            for f in filenames:
                fp = os.path.join(dirpath, f)
                try:
                    if os.path.islink(fp) or not os.path.isfile(fp):
                        continue
                    total_size += os.path.getsize(fp)
                    file_count += 1
                except OSError:
                    continue
            dir_count += len(dirnames)
    except OSError as e:
        return {"error": f"could not compute usage: {e}", "path": root_relative, "ok": False}

    return {
        "ok": True,
        "path": root_relative,
        "size_bytes": total_size,
        "size": human_size(total_size),
        "file_count": file_count,
        "directory_count": dir_count,
    }
