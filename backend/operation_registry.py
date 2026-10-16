#!/usr/bin/env python3
"""
RaheeCraft Stage 4 — Operation Registry
========================================
Single source of truth for the Stage 4 operation schema and allowed types.

Both the backend (validation + queueing) and the Kaggle agent (execution +
response shaping) import this module so the protocol, limits, and redaction
rules stay in sync.

Design notes
------------
- Every operation is a typed message with a fixed schema version. Unknown
  types or wrong-version payloads are rejected before anything filesystem-
  related happens.
- Operations are queued through the existing action relay so they get action
  IDs, agent addressing, timeouts, idempotency, and audit trails the same way
  start/stop/console/player actions do.
- Long-running work (uploads, downloads, archives, backups) reports progress
  events through the same relay result channel; the backend never trusts a
  200 as "done" — it waits for a terminal state.
- Secrets, tokens, upload payload bytes, and agent-side paths are never
  forwarded to the browser. Audit entries redact sensitive fields.
"""

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from backend.config import config

# ---------------------------------------------------------------------------
# Protocol version
# ---------------------------------------------------------------------------

OPERATION_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Allowlisted operation types
# ---------------------------------------------------------------------------
# Adding a new type means:
#  1. Add it here.
#  2. Add the backend endpoint (or reuse an existing one) that queues it.
#  3. Add the agent-side executor branch.
#  4. Add audit handling where appropriate.
#  5. Add tests.
#
# Order matches the acceptance priority: File Manager first, then Drive
# Backup Center, then Plugin Manager, Version Manager, World Manager.

FILE_OPERATIONS = {
    # Browse and read
    "file_browse",                 # list a directory
    "file_read",                  # read a text file into the editor / browser
    # Write / mutations
    "file_write",                 # save editor contents (atomic write + rename)
    "file_upload",                # chunked upload into a target path
    "file_make_dir",              # create a directory
    "file_rename",                # rename / move within the sandbox
    "file_delete",                # delete a file or empty directory
    # Download
    "file_download",              # download a single file
    # Editor-only convenience
    "file_editor_backup",         # snapshot a file before editing (for undo)
}

PLUGIN_OPERATIONS = {
    "plugin_list",                # inventory installed JARs + metadata
    "plugin_upload",              # upload a plugin JAR (staged, then placed)
    "plugin_remove",              # remove a plugin JAR (with confirmation)
}

VERSION_OPERATIONS = {
    "version_info",               # current + available versions from a trusted source
    "version_switch",             # download + staged install + rollback plan
    "version_rollback",           # restore the previously backed-up JAR
}

WORLD_OPERATIONS = {
    "world_list",                 # discover worlds from server config + FS
    "world_info",                 # per-world metadata + disk usage (safe)
    "world_backup",               # snapshot a world to a local archive
    "world_export",               # download a world archive
    "world_import",               # import a validated archive into a world slot
    "world_set_main",             # change the configured main world (safe procedure)
    "world_delete",               # destructive: requires typed confirmation
}

BACKUP_OPERATIONS = {
    "backup_list",                # list Drive backups + status
    "backup_create",              # trigger a local snapshot + Drive upload
    "backup_download",            # download a backup archive
    "backup_restore",             # staged restore with preflight + confirmation
    "backup_delete",              # delete a Drive backup
    "backup_status",              # last success / last error / progress
}

ALLOWED_OPERATION_TYPES = (
    FILE_OPERATIONS
    | PLUGIN_OPERATIONS
    | VERSION_OPERATIONS
    | WORLD_OPERATIONS
    | BACKUP_OPERATIONS
)

# ---------------------------------------------------------------------------
# Size / patience limits
# ---------------------------------------------------------------------------

MAX_FILE_EDIT_BYTES = int(os.environ.get("STAGE4_MAX_EDIT_BYTES", str(2 * 1024 * 1024)))
MAX_UPLOAD_BYTES = int(os.environ.get("STAGE4_MAX_UPLOAD_BYTES", str(256 * 1024 * 1024)))
MAX_DOWNLOAD_BYTES = int(os.environ.get("STAGE4_MAX_DOWNLOAD_BYTES", str(256 * 1024 * 1024)))
MAX_ARCHIVE_BYTES = int(os.environ.get("STAGE4_MAX_ARCHIVE_BYTES", str(4 * 1024 * 1024 * 1024)))
MAX_UPLOAD_CHUNK_BYTES = int(os.environ.get("STAGE4_UPLOAD_CHUNK_BYTES", str(4 * 1024 * 1024)))
MAX_CONCURRENT_OPERATIONS = int(os.environ.get("STAGE4_MAX_CONCURRENT_OPERATIONS", "6"))
OPERATION_RESULT_BYTES_CAP = int(os.environ.get("STAGE4_RESULT_BYTES_CAP", str(256 * 1024)))

# Text-editor editable types (download-only for everything else).
EDITABLE_EXTENSIONS = frozenset({
    ".properties", ".yml", ".yaml", ".json", ".toml", ".txt",
    ".conf", ".cfg", ".ini", ".sample", ".example",
    ".md", ".log", ".csv",
})

# Plugin JAR limits. A JAR is NEVER considered "safe" just because it is a
# zip — we only install files whose name and structure match what we expect,
# and even then we never load them into the production JVM.
PLUGIN_JAR_EXTENSION = ".jar"
PLUGIN_UPLOAD_MAX_BYTES = int(os.environ.get("STAGE4_PLUGIN_UPLOAD_BYTES",
                                               str(64 * 1024 * 1024)))

# World import / backup limits.
WORLD_IMPORT_MAX_ARCHIVE_BYTES = MAX_ARCHIVE_BYTES
WORLD_IMPORT_MAX_ENTRY_COUNT = 200000
WORLD_IMPORT_MAX_FILENAME_LENGTH = 512
WORLD_IMPORT_MAX_FILESYSTEM_PATH_LENGTH = 4096
WORLD_IMPORT_BLOCKED_TOP_NAMES = frozenset({
    "..", ".", "", "/", "\\",
})

# ---------------------------------------------------------------------------
# Trust sources for version metadata / plugin downloads
# ---------------------------------------------------------------------------
# The version manager only talks to the official Paper API for metadata. For
# plugin downloads we do NOT invent a marketplace integration; if no verified
# provider is configured we expose upload-only and document the rest as
# unavailable.

# PaperMC retired v2 (it now answers HTTP 410 "sunset"); the current API is v3
# at fill.papermc.io. Read live so a deployment (or a test) can point the code
# at a different base without editing this file.
DEFAULT_PAPER_API_BASE = "https://fill.papermc.io/v3"


def paper_api_base() -> str:
    """PaperMC API base URL, read from the environment on every call."""
    return os.environ.get("PAPER_API_BASE", DEFAULT_PAPER_API_BASE).rstrip("/")

# If a trusted plugin provider is ever configured, name it here so the UI can
# advertise available sources rather than inventing them.
PLUGIN_PROVIDERS_CONFIGURED = False  # set True only when a verified provider exists

# ---------------------------------------------------------------------------
# Redaction helpers (audit + logging)
# ---------------------------------------------------------------------------

SENSITIVE_KEYS = frozenset({
    "token", "refresh_token", "secret", "password", "confirm_password",
    "agent_secret", "drive_token", "credentials",
})

PATH_FIELD_NAMES = frozenset({
    "path", "target_path", "source_path", "old_path", "new_path",
    "file_path", "dir_path", "world_dir", "target",
})


def redact_operation_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Return a copy of an operation payload with sensitive values replaced by
    placeholders. Used for audit and logging only.
    """
    out: Dict[str, Any] = {}
    for k, v in payload.items():
        key = str(k).lower()
        if key in SENSITIVE_KEYS:
            out[k] = "***"
        elif isinstance(v, str) and len(v) > 0:
            # Paths are kept but truncated for logs; secrets inside paths stay hidden.
            if key in PATH_FIELD_NAMES:
                out[k] = _redact_path(v)
            else:
                out[k] = v
        elif isinstance(v, dict):
            out[k] = redact_operation_payload(v)
        elif isinstance(v, list):
            out[k] = [
                redact_operation_payload(item) if isinstance(item, dict) else item
                for item in v
            ]
        else:
            out[k] = v
    return out


def _redact_path(path: str) -> str:
    """Truncate a path for logs without exposing full server layout."""
    if not path:
        return ""
    parts = path.replace("\\", "/").split("/")
    if len(parts) <= 3:
        return path[:120]
    return "/".join([parts[0], parts[1], "…" if len(parts) > 4 else parts[-2], parts[-1]])[:120]


# ---------------------------------------------------------------------------
# Small helpers used by both sides
# ---------------------------------------------------------------------------

def is_editable(filename: str) -> bool:
    """Whether a file may be opened in the text editor."""
    name = filename.lower()
    return any(name.endswith(ext) for ext in EDITABLE_EXTENSIONS)


def looks_like_a_jar(filename: str, first_bytes: bytes) -> bool:
    """
    Best-effort JAR plausibility check. Returns True only when the name ends
    with .jar AND the file starts with a ZIP local-file header. This is not a
    safety guarantee — it is a minimum filter before any further checks.
    """
    if not filename.lower().endswith(".jar"):
        return False
    if not first_bytes or len(first_bytes) < 4:
        return False
    # ZIP local file header signature
    return first_bytes[:4] == b"PK\x03\x04"


def limit_result_payload(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Cap the size of a result dict returned through the relay. Large payloads
    (for example, full directory trees) must be streamed or paged, not stuffed
    into the terminal result.
    """
    import json
    text = json.dumps(result, default=str)
    if len(text) <= OPERATION_RESULT_BYTES_CAP:
        return result
    # Preserve the decision fields; trim the bulky ones.
    trimmed: Dict[str, Any] = {}
    for k in ("state", "error", "status", "action_id", "operation_id",
              "success", "warning", "note"):
        if k in result:
            trimmed[k] = result[k]
    for k in ("path", "target", "filename", "operation_type", "kind"):
        if k in result:
            trimmed[k] = result[k]
    trimmed["_truncated"] = True
    trimmed["_reason"] = "result too large for relay; request the resource via its dedicated endpoint"
    return trimmed


def validate_operation_payload(
    op_type: str, payload: Any
) -> Tuple[bool, Optional[str]]:
    """
    Backend-side structural validation before queueing.

    Returns (ok, error).
    """
    if not isinstance(payload, dict):
        return False, "Operation payload must be a JSON object"

    if op_type in ("file_read", "file_browse", "file_download",
                   "file_editor_backup"):
        path = payload.get("path")
        if not isinstance(path, str) or not path.strip():
            return False, "path is required"
        if not isinstance(payload.get("path"), str):
            return False, "path must be a string"

    if op_type == "file_write":
        path = payload.get("path")
        content = payload.get("content")
        if not isinstance(path, str) or not path.strip():
            return False, "path is required"
        if content is None:
            return False, "content is required"
        if not isinstance(content, str):
            return False, "content must be a string"
        if len(content.encode("utf-8", errors="replace")) > MAX_FILE_EDIT_BYTES:
            return False, f"content exceeds the editor size limit ({MAX_FILE_EDIT_BYTES} bytes)"

    if op_type == "file_upload":
        path = payload.get("path")
        if not isinstance(path, str) or not path.strip():
            return False, "path is required"
        suggested_name = payload.get("filename") or "upload"
        if not isinstance(suggested_name, str) or not suggested_name.strip():
            return False, "filename is required"

    if op_type in ("file_make_dir", "file_rename", "file_delete"):
        path = payload.get("path")
        if not isinstance(path, str) or not path.strip():
            return False, "path is required"

    if op_type == "file_rename":
        new_path = payload.get("new_path")
        if not isinstance(new_path, str) or not new_path.strip():
            return False, "new_path is required"

    if op_type in PLUGIN_OPERATIONS:
        if op_type == "plugin_upload":
            path = payload.get("path")
            if not isinstance(path, str) or not path.strip():
                return False, "path is required"
            if not path.lower().endswith(PLUGIN_JAR_EXTENSION):
                return False, "plugin uploads must target a .jar path"
        if op_type == "plugin_remove":
            path = payload.get("path")
            if not isinstance(path, str) or not path.strip():
                return False, "path is required"
            if not path.lower().endswith(PLUGIN_JAR_EXTENSION):
                return False, "plugin removal targets a .jar path"

    if op_type in VERSION_OPERATIONS:
        if op_type == "version_switch":
            version = payload.get("version")
            if not isinstance(version, str) or not version.strip():
                return False, "version is required"
        if op_type == "version_rollback":
            pass

    if op_type in WORLD_OPERATIONS:
        if op_type == "world_set_main":
            world = payload.get("world")
            if not isinstance(world, str) or not world.strip():
                return False, "world is required"
        if op_type == "world_delete":
            world = payload.get("world")
            if not isinstance(world, str) or not world.strip():
                return False, "world is required"
            if not payload.get("confirm_delete_world"):
                return False, "typed confirmation is required for world deletion"
        if op_type in ("world_import", "world_export"):
            world = payload.get("world")
            if not isinstance(world, str) or not world.strip():
                return False, "world is required"

    if op_type in BACKUP_OPERATIONS:
        if op_type == "backup_restore":
            backup_id = payload.get("backup_id")
            if not isinstance(backup_id, str) or not backup_id.strip():
                return False, "backup_id is required"
            if not payload.get("confirm_restore"):
                return False, "typed confirmation is required for restore"
        if op_type == "backup_delete":
            backup_id = payload.get("backup_id")
            if not isinstance(backup_id, str) or not backup_id.strip():
                return False, "backup_id is required"

    return True, None


def sanitize_operation_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Shape an agent result for the browser: keep decision-relevant fields,
    cap size, redact anything secret-bearing.
    """
    out = limit_result_payload(result)
    return redact_operation_payload(out)
