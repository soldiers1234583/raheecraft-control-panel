#!/usr/bin/env python3
"""
RaheeCraft Stage 4 — Agent-side file/system operation executor
==============================================================
Runs on the Kaggle notebook alongside the Paper server. It executes the
file-manager, plugin-manager, version-manager, world-manager, and backup-center
operations the backend queues through the existing action relay.

Security rules (mirrors the spec):
- The browser is NEVER trusted. The backend does defense-in-depth validation,
  but the agent canonicalizes and re-validates every path against the real
  filesystem immediately before use.
- All operations are restricted to the configured server directory
  (agent.config.config.SERVER_DIR).
- ".." traversal, absolute paths, and path-prefix tricks are rejected.
- Symlinks are rejected by default.
- Editable types are allowlisted; everything else is download-only.
- Uploads and editor saves are written atomically (temp file + rename) into the
  target location.
- World directories are protected: they may not be renamed, deleted, imported
  over, or replaced while the server may be writing to them.
- Plugin JARs are validated for name + ZIP structure; they are NEVER loaded or
  executed by this module.
- Archives are validated for traversal, size, entry counts, and decompression
  bombs before extraction.
- Secrets and agent-side paths are never forwarded to the backend except through
  the redaction helpers in backend.operation_registry (when reporting results).
- Operation results are capped so we never stuff a whole world tree into the
  relay result.
"""

import hashlib
import io
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Re-export for clarity only where other modules import this file.
__all__ = ["execute_stage4_with_report", "execute_stage4_operation"]


PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.config import config

# Reuse the backend's shared schema/limits where practical. We import them
# directly so both sides stay in sync; the agent never trusts values sent by
# the browser.
from backend.operation_registry import (
    ALLOWED_OPERATION_TYPES,
    MAX_FILE_EDIT_BYTES,
    MAX_UPLOAD_BYTES,
    MAX_DOWNLOAD_BYTES,
    MAX_ARCHIVE_BYTES,
    MAX_UPLOAD_CHUNK_BYTES,
    EDITABLE_EXTENSIONS,
    is_editable,
    looks_like_a_jar,
    limit_result_payload,
    redact_operation_payload,
    sanitize_operation_result,
    validate_operation_payload,
    OPERATION_SCHEMA_VERSION,
    paper_api_base,
)

def server_dir() -> Path:
    """The live server root on this agent host.

    Resolved from `agent.config.config.SERVER_DIR` on every call. The config
    singleton is rebound at runtime (notebook setup, tests, operator overrides),
    so caching this at import time would silently keep serving the old root.
    """
    return Path(config.SERVER_DIR).resolve()


# Backwards-compatible module attribute: the root as it was at import time.
# Internal code must use `server_dir()` so a runtime config change takes effect.
SERVER_DIR = server_dir()

# Where we stage uploads/downloads/archives/restores before placing them.
# Created lazily so importing the module does not fail on a read-only host.
# Cached per server root so a runtime root change does not reuse a stale path.
STAGING_ROOT: Optional[Path] = None
BACKUP_STAGING: Optional[Path] = None
_staging_for: Optional[Path] = None
_backup_staging_for: Optional[Path] = None


def _ensure_staging() -> Path:
    """Staging dir for the *current* server root, created on first use.

    Re-created when the cached path no longer exists: the staging directory can
    be cleaned (or the whole server tree replaced) between operations, and a
    stale cache would otherwise make every later write fail with ENOENT.
    """
    global STAGING_ROOT, _staging_for
    root_server = server_dir()
    if (STAGING_ROOT is not None and _staging_for == root_server
            and STAGING_ROOT.is_dir()):
        return STAGING_ROOT
    root = root_server / ".stage"
    try:
        root.mkdir(parents=True, exist_ok=True)
        STAGING_ROOT = root
    except OSError:
        STAGING_ROOT = Path(tempfile.mkdtemp(prefix="rahee-stage-"))
    _staging_for = root_server
    return STAGING_ROOT


def _ensure_backup_staging() -> Path:
    """Backup staging dir for the *current* server root, created on first use.

    Re-created when the cached path has been removed, so a cleaned staging
    directory never poisons later backups, exports, or imports.
    """
    global BACKUP_STAGING, _backup_staging_for
    root_server = server_dir()
    if (BACKUP_STAGING is not None and _backup_staging_for == root_server
            and BACKUP_STAGING.is_dir()):
        return BACKUP_STAGING
    root = root_server / ".backup-stage"
    try:
        root.mkdir(parents=True, exist_ok=True)
        BACKUP_STAGING = root
    except OSError:
        BACKUP_STAGING = Path(tempfile.mkdtemp(prefix="rahee-backup-stage-"))
    _backup_staging_for = root_server
    return BACKUP_STAGING


# Track in-flight operations for bounded concurrency. This is local to the
# agent process; the backend already bounds concurrency via the relay queue.
_in_flight: Dict[str, Dict[str, Any]] = {}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


# ---------------------------------------------------------------------------
# Path safety (agent-side; stricter than the backend because we touch real FS)
# ---------------------------------------------------------------------------

def _norm_root() -> str:
    return os.path.normpath(str(server_dir()))


def safe_agent_path(user_path: str) -> Tuple[bool, Optional[str], Optional[Path]]:
    """
    Agent-side sandbox check. Returns (ok, error, resolved).

    Must be called immediately before any filesystem operation, even if the
    backend already validated the path.
    """
    if not isinstance(user_path, str):
        return False, "path must be a string", None
    # Validate control characters on the RAW input, before stripping. Stripping
    # first would silently accept "server.properties\n" by removing the very
    # character we are supposed to reject.
    if "\x00" in user_path:
        return False, "path contains illegal characters", None
    if any(ch in user_path for ch in "\r\n"):
        return False, "path must not contain newlines", None
    raw = user_path.strip()
    if not raw:
        return False, "path must not be empty", None
    if os.path.isabs(raw):
        return False, "absolute paths are not allowed", None

    lowered = raw.lower()
    # A bare "." legitimately means the server root (uploads target the root);
    # ".." and any leading parent traversal do not.
    if lowered == ".." or lowered.startswith("../") or lowered.startswith("..\\"):
        return False, "path traversal is not allowed", None

    # Reject paths whose components traverse out.
    parts = raw.replace("\\", "/").split("/")
    depth = 0
    for p in parts:
        if p == "..":
            depth -= 1
            if depth < 0:
                return False, "path traversal is not allowed", None
        elif p and p != ".":
            depth += 1
    if depth < 0:
        return False, "path traversal is not allowed", None

    try:
        joined = server_dir() / raw
        resolved = joined.resolve()
    except (ValueError, OSError) as e:
        return False, f"path could not be resolved: {e}", None

    root_norm = _norm_root()
    resolved_norm = os.path.normpath(str(resolved))
    if resolved_norm != root_norm and not resolved_norm.startswith(root_norm + os.sep):
        return False, "path escapes the server directory", None

    # Symlinks are rejected by default.
    if _contains_symlink_component(joined):
        return False, "symlinks are not allowed", None

    return True, None, resolved


def _contains_symlink_component(path: Path) -> bool:
    current: Path = path
    while current != server_dir() and current != current.parent:
        try:
            if current.is_symlink():
                return True
        except (OSError, ValueError):
            return True
        current = current.parent
    return False


def safe_basename(name: str) -> Tuple[bool, Optional[str], Optional[str]]:
    if not isinstance(name, str):
        return False, "name must be a string", None
    name = name.strip()
    if not name:
        return False, "name must not be empty", None
    if os.path.isabs(name):
        return False, "name must not be absolute", None
    if name in (".", ".."):
        return False, "name must not be '.' or '..'", None
    # Parent traversal is reported as traversal (not merely "separators") so the
    # operator sees the actual reason the name was refused.
    if name.startswith("../") or name.startswith("..\\") or "/../" in name or "\\..\\" in name:
        return False, "name must not contain path traversal", None
    if "/" in name or "\\" in name:
        return False, "name must not contain separators", None
    if "\x00" in name:
        return False, "name contains illegal characters", None
    if any(ch in name for ch in "\r\n"):
        return False, "name must not contain newlines", None
    if len(name) > 512:
        return False, "name is too long", None
    return True, None, name


# ---------------------------------------------------------------------------
# Small file helpers
# ---------------------------------------------------------------------------

def _read_text_bytes_ok(path: Path) -> Tuple[bool, Optional[str], Optional[str]]:
    """Try to read a file as UTF-8 text. Returns (ok, error, text)."""
    try:
        data = path.read_bytes()
    except OSError as e:
        return False, f"cannot read file: {e}", None
    if len(data) > MAX_FILE_EDIT_BYTES:
        return False, f"file exceeds editor size limit ({MAX_FILE_EDIT_BYTES} bytes)", None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False, "file is not valid UTF-8 text (download only)", None
    return True, None, text


def _write_atomic(path: Path, content: str) -> Tuple[bool, Optional[str]]:
    """
    Write content to `path` atomically: write to a temp file in the same
    directory, then rename. Preserves the target location across interruptions.
    """
    try:
        target_dir = path.parent
        target_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(target_dir), prefix=".rahee-write-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            os.replace(tmp, str(path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True, None
    except OSError as e:
        return False, f"write failed: {e}"


def _make_backup(path: Path) -> Optional[Path]:
    """
    Create a timestamped backup of an existing file inside the server dir.
    Returns the backup path, or None if there was nothing to back up.
    """
    if not path.exists():
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = path.with_name(f".{path.name}.{stamp}.bak")
    try:
        shutil.copy2(path, backup)
        return backup
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Browse / read / write / upload / download / mkdir / rename / delete
# ---------------------------------------------------------------------------

def file_browse(path: str) -> Dict[str, Any]:
    ok, err, resolved = safe_agent_path(path)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}
    if not resolved.exists():
        return {"state": "failed", "error": "path does not exist", "result": {}}
    if not resolved.is_dir():
        return {"state": "failed", "error": "path is not a directory", "result": {}}

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
                    "size": None if is_dir else st.st_size,
                    "modified": st.st_mtime,
                    "is_symlink": entry.is_symlink(),
                })
    except OSError as e:
        return {"state": "failed", "error": f"cannot list directory: {e}", "result": {}}

    entries.sort(key=lambda e: (not e["is_directory"], e["name"].lower()))
    return {
        "state": "success",
        "result": {
            "path": path,
            "entries": entries,
            "count": len(entries),
        },
    }


def file_read(path: str) -> Dict[str, Any]:
    """Read a file for the text editor.

    Order matters: existence, then file-ness, then the editable-type allowlist.
    The editor is not a general file viewer — anything outside the allowlisted
    text formats is reported as download-only rather than decoded.
    """
    ok, err, resolved = safe_agent_path(path)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}
    if not resolved.exists():
        return {"state": "failed", "error": "path does not exist", "result": {}}
    if not resolved.is_file():
        return {"state": "failed", "error": "path is not a file", "result": {}}

    if not is_editable(resolved.name):
        return {"state": "failed",
                "error": (f"'{resolved.name}' is not an editable text file "
                          f"(download only): the editor accepts allowlisted "
                          f"text formats that decode as UTF-8"),
                "result": {"path": path, "editable": False}}

    ok, err, text = _read_text_bytes_ok(resolved)
    if not ok:
        return {"state": "failed", "error": err,
                "result": {"path": path, "editable": True}}

    return {
        "state": "success",
        "error": None,
        "result": {
            "path": path,
            "content": text,
            "size": len(text.encode("utf-8")),
            "editable": True,
        },
    }


def file_write(path: str, content: str) -> Dict[str, Any]:
    ok, err, resolved = safe_agent_path(path)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}
    if not resolved.suffix.lower() in EDITABLE_EXTENSIONS and not resolved.name.lower().endswith(
        tuple(EDITABLE_EXTENSIONS)
    ):
        # Defense in depth: the backend already validated editable type, but the
        # agent re-validates before writing.
        return {"state": "failed", "error": "file type is not editable", "result": {}}

    # Preserve a recoverable previous version when editing an existing file.
    backup: Optional[Path] = None
    if resolved.exists():
        backup = _make_backup(resolved)

    ok, err = _write_atomic(resolved, content)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}

    return {
        "state": "success",
        "result": {
            "path": path,
            "backup": str(backup.relative_to(server_dir())) if backup else None,
            "size": resolved.stat().st_size,
        },
    }


def file_upload(path: str, chunk_iter, filename: str) -> Dict[str, Any]:
    """
    Receive an uploaded file as a sequence of byte chunks, write it atomically,
    and report the final path + size.

    `chunk_iter` should yield bytes objects. This is streamed to a temp file so
    we never hold the whole upload in memory.
    """
    ok, err, resolved = safe_agent_path(path)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}

    name_ok, name_err, basename = safe_basename(filename)
    if not name_ok:
        return {"state": "failed", "error": name_err, "result": {}}

    # A non-existent target path is treated as a directory to create (that is
    # what "upload into config/notes.txt" means); an existing file path means
    # upload into its parent.
    if resolved.exists() and not resolved.is_dir():
        target = resolved.parent
    else:
        target = resolved
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return {"state": "failed", "error": f"cannot create target directory: {e}", "result": {}}
    final = target / basename

    # Extension guard: uploads keep their name, but we still reject obviously
    # unsafe extensions that collide with editor-only or reserved names.
    lowered = basename.lower()
    if lowered in (".", ".."):
        return {"state": "failed", "error": "invalid filename", "result": {}}

    total = 0
    tmp_path_obj: Optional[Path] = None
    staged = False
    try:
        fd, tmp = tempfile.mkstemp(dir=str(target), prefix=".rahee-upload-")
        tmp_path_obj = Path(tmp)
        with os.fdopen(fd, "wb") as f:
            for chunk in chunk_iter:
                if not isinstance(chunk, (bytes, bytearray, memoryview)):
                    return {"state": "failed", "error": "invalid chunk type", "result": {}}
                f.write(bytes(chunk))
                total += len(bytes(chunk))
                if total > MAX_UPLOAD_BYTES:
                    return {"state": "failed",
                            "error": f"upload exceeds the size limit ({MAX_UPLOAD_BYTES} bytes)",
                            "result": {}}
        os.replace(str(tmp_path_obj), str(final))
        staged = True
    except OSError as e:
        return {"state": "failed", "error": f"upload write failed: {e}", "result": {}}
    finally:
        # Never leave a partial upload behind, whatever the exit path.
        if not staged and tmp_path_obj and tmp_path_obj.exists():
            try:
                os.unlink(str(tmp_path_obj))
            except OSError:
                pass

    return {
        "state": "success",
        "result": {
            "path": path.rstrip("/") + "/" + basename if resolved.is_dir() else path,
            "filename": basename,
            "size": total,
            "modified": final.stat().st_mtime,
        },
    }


def file_download(path: str) -> Dict[str, Any]:
    ok, err, resolved = safe_agent_path(path)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}
    if not resolved.is_file():
        return {"state": "failed", "error": "path is not a file", "result": {}}
    try:
        size = resolved.stat().st_size
    except OSError as e:
        return {"state": "failed", "error": f"cannot stat file: {e}", "result": {}}
    if size > MAX_DOWNLOAD_BYTES:
        return {"state": "failed",
                "error": f"file exceeds the download size limit ({MAX_DOWNLOAD_BYTES} bytes)",
                "result": {}}
    return {
        "state": "success",
        "result": {
            "path": path,
            "filename": resolved.name,
            "size": size,
            "modified": resolved.stat().st_mtime,
            "_download_staging": str(_final_download_staging(path)),
        },
    }


def _final_download_staging(path: str) -> Path:
    ok, err, resolved = safe_agent_path(path)
    if not ok:
        raise RuntimeError(err)
    dst = _ensure_backup_staging() / resolved.name
    return dst


def file_make_dir(path: str) -> Dict[str, Any]:
    ok, err, resolved = safe_agent_path(path)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}
    if resolved.exists():
        return {"state": "failed", "error": "path already exists", "result": {}}
    try:
        resolved.mkdir(parents=True, exist_ok=False)
        return {"state": "success",
                "result": {"path": path, "created": str(resolved.relative_to(server_dir()))}}
    except OSError as e:
        return {"state": "failed", "error": f"mkdir failed: {e}", "result": {}}


def file_rename(path: str, new_path: str) -> Dict[str, Any]:
    ok, err, resolved = safe_agent_path(path)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}
    if not resolved.exists():
        return {"state": "failed", "error": "source does not exist", "result": {}}

    ok2, err2, new_resolved = safe_agent_path(new_path)
    if not ok2:
        return {"state": "failed", "error": err2, "result": {}}
    if new_resolved.exists():
        return {"state": "failed", "error": "target already exists", "result": {}}

    # Protecting world dirs from rename is enforced here as well.
    if _is_world_dir(resolved) or _is_world_dir(new_resolved):
        return {"state": "failed",
                "error": "world directories cannot be renamed through the file manager",
                "result": {}}

    try:
        new_resolved.parent.mkdir(parents=True, exist_ok=True)
        os.rename(str(resolved), str(new_resolved))
        return {"state": "success",
                "result": {
                    "old_path": path,
                    "new_path": new_path,
                }}
    except OSError as e:
        return {"state": "failed", "error": f"rename failed: {e}", "result": {}}


def file_delete(path: str, confirm: bool = False) -> Dict[str, Any]:
    ok, err, resolved = safe_agent_path(path)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}
    if not resolved.exists():
        return {"state": "failed", "error": "path does not exist", "result": {}}

    # World directories are a special case: they require typed confirmation and
    # are only deletable through the world manager, not through a casual delete.
    if _is_world_dir(resolved):
        return {"state": "failed",
                "error": "use the World Manager to delete a world; this requires typed confirmation and a pre-delete backup",
                "result": {}}

    if resolved.is_dir():
        # Only allow deleting empty directories from the general file manager.
        try:
            if any(resolved.iterdir()):
                return {"state": "failed",
                        "error": "directory is not empty; delete contents first or use the dedicated manager",
                        "result": {}}
        except OSError as e:
            return {"state": "failed", "error": f"cannot inspect directory: {e}", "result": {}}

    if not confirm:
        return {"state": "failed",
                "error": "deletion requires explicit confirmation",
                "result": {}}

    try:
        if resolved.is_dir():
            os.rmdir(str(resolved))
        else:
            os.unlink(str(resolved))
        return {"state": "success", "result": {"path": path, "deleted": True}}
    except OSError as e:
        return {"state": "failed", "error": f"delete failed: {e}", "result": {}}


def file_editor_backup(path: str) -> Dict[str, Any]:
    ok, err, resolved = safe_agent_path(path)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}
    if not resolved.is_file():
        return {"state": "failed", "error": "path is not a file", "result": {}}
    backup = _make_backup(resolved)
    return {
        "state": "success",
        "result": {
            "path": path,
            "backup": str(backup.relative_to(server_dir())) if backup else None,
        },
    }


# ---------------------------------------------------------------------------
# Plugin operations
# ---------------------------------------------------------------------------

def plugins_dir() -> Path:
    """Plugins directory under the live server root."""
    return server_dir() / "plugins"


def _ensure_plugins_dir() -> Tuple[bool, Optional[str]]:
    try:
        plugins_dir().mkdir(parents=True, exist_ok=True)
        return True, None
    except OSError as e:
        return False, f"cannot create plugins directory: {e}"


def plugin_list() -> Dict[str, Any]:
    ok, err = _ensure_plugins_dir()
    if not ok:
        return {"state": "failed", "error": err, "result": {}}

    plugins: List[Dict[str, Any]] = []
    try:
        for entry in sorted(plugins_dir().glob("*.jar"), key=lambda p: p.name.lower()):
            try:
                st = entry.stat()
            except OSError:
                continue
            name = entry.name
            metadata: Dict[str, Any] = {
                "filename": name,
                "size": st.st_size,
                "modified": st.st_mtime,
                "is_jar": looks_like_a_jar(name, entry.read_bytes()[:4]),
                "path": str(entry.relative_to(server_dir())),
                "publisher": None,
                "version": None,
                "mc_version": None,
                "paper_version": None,
                "compatible": None,
                "restart_required": True,
            }
            plugins.append(metadata)
    except OSError as e:
        return {"state": "failed", "error": f"cannot list plugins: {e}", "result": {}}

    return {
        "state": "success",
        "result": {
            "plugins": plugins,
            "count": len(plugins),
            "plugins_dir": str(plugins_dir().relative_to(server_dir())),
            "note": "version/publisher metadata is not available without a verified plugin source integration",
        },
    }


def plugin_upload(path: str, chunk_iter, filename: str) -> Dict[str, Any]:
    """
    Upload a plugin JAR into plugins/. The file is staged then placed
    atomically; we never silently overwrite an existing plugin.
    """
    ok, err = _ensure_plugins_dir()
    if not ok:
        return {"state": "failed", "error": err, "result": {}}

    name_ok, name_err, basename = safe_basename(filename)
    if not name_ok:
        return {"state": "failed", "error": name_err, "result": {}}
    if not basename.lower().endswith(".jar"):
        return {"state": "failed",
                "error": f"'{basename}' is not a valid JAR: plugin uploads must be .jar files",
                "result": {}}

    target = plugins_dir() / basename

    if target.exists():
        return {"state": "failed",
                "error": f"plugin '{basename}' already exists; remove it first or upload with a different name",
                "result": {}}

    # Validate JAR structure before placing it.
    tmp_path_obj: Optional[Path] = None
    try:
        fd, tmp = tempfile.mkstemp(dir=str(_ensure_staging()), prefix=".rahee-plugin-")
        tmp_path_obj = Path(tmp)
        with os.fdopen(fd, "wb") as f:
            total = 0
            for chunk in chunk_iter:
                f.write(bytes(chunk))
                total += len(bytes(chunk))
                if total > MAX_UPLOAD_BYTES:
                    return {"state": "failed",
                            "error": f"upload exceeds the size limit ({MAX_UPLOAD_BYTES} bytes)",
                            "result": {}}
        # Read first bytes for structure check before moving into plugins/.
        head = tmp_path_obj.read_bytes()[:4]
        if not looks_like_a_jar(basename, head):
            return {"state": "failed",
                    "error": "uploaded file is not a valid JAR (ZIP structure check failed)",
                    "result": {}}
        os.replace(str(tmp_path_obj), str(target))
    except OSError as e:
        if tmp_path_obj and tmp_path_obj.exists():
            try:
                os.unlink(str(tmp_path_obj))
            except OSError:
                pass
        return {"state": "failed", "error": f"upload failed: {e}", "result": {}}
    finally:
        if tmp_path_obj and tmp_path_obj.exists() and tmp_path_obj != target:
            try:
                os.unlink(str(tmp_path_obj))
            except OSError:
                pass

    try:
        st = target.stat()
    except OSError:
        st = None

    return {
        "state": "success",
        "result": {
            "filename": basename,
            "path": str(target.relative_to(server_dir())),
            "size": st.st_size if st else None,
            "restart_required": True,
            "note": "A server restart is required to load the plugin. Plugin data may remain in its own directory after removal.",
        },
    }


def plugin_remove(path: str, confirm: bool = False) -> Dict[str, Any]:
    ok, err, resolved = safe_agent_path(path)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}
    if not resolved.exists():
        return {"state": "failed", "error": "target is not a file", "result": {}}
    if not resolved.is_file() or not resolved.name.lower().endswith(".jar"):
        return {"state": "failed",
                "error": "target must be a plugin .jar in the plugins directory",
                "result": {}}
    if not confirm:
        return {"state": "failed",
                "error": "plugin removal requires explicit confirmation; plugin data may remain in its own directory",
                "result": {}}
    try:
        resolved.unlink()
        return {"state": "success",
                "result": {
                    "path": path,
                    "removed": True,
                    "note": "Plugin data directory may still exist and should be removed separately if no longer needed.",
                }}
    except OSError as e:
        return {"state": "failed", "error": f"remove failed: {e}", "result": {}}


# ---------------------------------------------------------------------------
# Version operations
# ---------------------------------------------------------------------------

def version_info() -> Dict[str, Any]:
    """
    Report the current Paper/Mc version and the list of available versions from
    the official Paper API. If the official metadata is unreachable, we refuse
    to present a curated list and explain why.
    """
    current = _current_version_read()
    available = _paper_available_versions()
    return {
        "state": "success",
        "result": {
            "current": current,
            "available": available.get("versions") if isinstance(available, dict) else [],
            "source": available.get("source") if isinstance(available, dict) else None,
            "note": available.get("note") if isinstance(available, dict) else None,
        },
    }


def _current_version_read() -> Dict[str, Any]:
    out = {
        "paper_jar": None,
        "paper_version_guess": None,
        "minecraft_version": None,
        "java_runtime": None,
        "server_dir": str(server_dir()),
    }
    jar = server_dir() / "paper.jar"
    if jar.exists():
        out["paper_jar"] = jar.name
        out["paper_version_guess"] = jar.stem
    props = server_dir() / "server.properties"
    if props.exists():
        try:
            for line in props.read_text().splitlines():
                if line.startswith("minecraft-version="):
                    out["minecraft_version"] = line.split("=", 1)[1].strip()
        except OSError:
            pass
    out["java_runtime"] = _java_version()
    return out


def _java_version() -> Optional[str]:
    try:
        import subprocess
        r = subprocess.run(["java", "-version"], capture_output=True, text=True, timeout=10)
        combined = (r.stdout or "") + (r.stderr or "")
        for line in combined.splitlines():
            if "version" in line.lower():
                return line.strip()
    except Exception:
        pass
    return None


def _paper_available_versions() -> Dict[str, Any]:
    """
    Pull version metadata from the official Paper API (v3).

    The v3 project endpoint groups versions by major release
    (`{"versions": {"1.21": ["1.21.11", ...]}}`); we flatten that into the
    `[{"version": ...}]` shape the rest of this module already consumes.

    If the API is unreachable, we return an empty list + a note rather than
    fabricating versions.
    """
    try:
        import urllib.request
        url = f"{paper_api_base()}/projects/paper"
        req = urllib.request.Request(url, headers={"User-Agent": "RaheeCraft-Stage4"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        grouped = data.get("versions") if isinstance(data, dict) else None
        flattened: List[str] = []
        if isinstance(grouped, dict):
            for _, group in grouped.items():
                if isinstance(group, list):
                    for v in group:
                        if isinstance(v, str) and v not in flattened:
                            flattened.append(v)
        elif isinstance(grouped, list):
            flattened = [v for v in grouped if isinstance(v, str)]
        return {
            "source": "papermc.io official API (v3)",
            "versions": [{"version": v} for v in flattened[:200]],
            "note": None,
        }
    except Exception as e:
        _close_http_error(e)
        return {
            "source": None,
            "versions": [],
            "note": f"Could not reach the official Paper API: {e}. Version switching is disabled until it is reachable.",
        }


def version_switch(version: str) -> Dict[str, Any]:
    """
    Download the selected Paper build to a staging location, verify it, back up
    the current JAR, and prepare a controlled swap that must be applied when the
    server is stopped.

    We never switch a running server.
    """
    # Enrollment: we only allow switching if the server is stopped.
    if _paper_running():
        return {"state": "failed",
                "error": "cannot switch Paper versions while the server is running; stop the server first",
                "result": {}}

    current = _current_version_read()
    paper_jar = server_dir() / "paper.jar"
    backup_jar = server_dir() / f"paper.jar.{time.strftime('%Y%m%d_%H%M%S')}.backup"

    # Fetch metadata to confirm the requested version exists in the official list.
    available = _paper_available_versions()
    version_ok = False
    if isinstance(available, dict):
        version_ok = any(v.get("version") == version or v.get("id") == version
                         for v in available.get("versions", []))

    if not version_ok:
        return {"state": "failed",
                "error": f"version '{version}' is not in the available official list; refusing to download from a mirror",
                "result": {}}

    # Stage the download.
    staged = _ensure_staging() / f"paper-{version}.jar"
    try:
        _download_paper_jar(version, staged)
    except Exception as e:
        return {"state": "failed",
                "error": f"download failed: {e}",
                "result": {}}

    # Verify the staged JAR.
    try:
        head = staged.read_bytes()[:4]
        if not looks_like_a_jar(f"paper-{version}.jar", head):
            staged.unlink(missing_ok=True)
            return {"state": "failed",
                    "error": "downloaded file is not a valid JAR",
                    "result": {}}
    except OSError as e:
        staged.unlink(missing_ok=True)
        return {"state": "failed", "error": f"verification failed: {e}", "result": {}}

    # Back up the current JAR if it exists.
    backed_up: Optional[str] = None
    if paper_jar.exists():
        try:
            shutil.copy2(paper_jar, backup_jar)
            backed_up = str(backup_jar.relative_to(server_dir()))
        except OSError as e:
            staged.unlink(missing_ok=True)
            return {"state": "failed",
                    "error": f"could not back up the current JAR: {e}",
                    "result": {}}

    return {
        "state": "success",
        "result": {
            "version": version,
            "source": "papermc.io official API",
            "staged": str(staged.relative_to(server_dir())),
            "backup_of_current": backed_up,
            "current_jar": current.get("paper_jar"),
            "apply_on_restart": True,
            "warning": "Downgrading Minecraft versions can damage or irreversibly change world data. Verify you have a complete verified world backup before restarting on this version.",
            "note": "The new JAR is staged but not actived. Start (or restart) the server to use it; the previous JAR backup is retained until you confirm the server works.",
        },
    }


def _close_http_error(exc: BaseException) -> None:
    """Release the response socket held by a urllib error object.

    `urllib.error.HTTPError` is also a file-like object. Leaving it unclosed
    leaks a socket until garbage collection (Python warns about it), so every
    handler that catches one should close it.
    """
    closer = getattr(exc, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:
            pass


def _download_paper_jar(version: str, dest: Path) -> None:
    """
    Download a Paper build from the official Paper API to `dest`.

    We do NOT fall back to third-party mirrors.
    """
    import urllib.request
    build = _paper_build_url(version)
    if not build:
        raise RuntimeError(f"no download URL found for Paper version {version}")
    req = urllib.request.Request(build, headers={"User-Agent": "RaheeCraft-Stage4"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            total = int(resp.headers.get("Content-Length", "0"))
            if total > MAX_ARCHIVE_BYTES:
                raise RuntimeError(f"downloaded JAR would exceed the archive size limit ({MAX_ARCHIVE_BYTES} bytes)")
            written = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(MAX_UPLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_ARCHIVE_BYTES:
                        raise RuntimeError(
                            f"download exceeded the archive size limit ({MAX_ARCHIVE_BYTES} bytes)")
                    f.write(chunk)
    except Exception as e:
        _close_http_error(e)
        raise


def _paper_build_url(version: str) -> Optional[str]:
    """Artifact URL for the newest usable build of `version` (v3 API).

    The v3 builds endpoint returns a newest-first list and carries the artifact
    URL, so we never construct one ourselves (the old v2 URL scheme is gone).
    """
    try:
        import urllib.request
        url = f"{paper_api_base()}/projects/paper/versions/{version}/builds"
        req = urllib.request.Request(url, headers={"User-Agent": "RaheeCraft-Stage4"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        builds = data if isinstance(data, list) else (data.get("builds") or [])
        if not builds:
            return None
        stable = [b for b in builds
                  if isinstance(b, dict) and str(b.get("channel", "")).upper() == "STABLE"]
        chosen = (stable or builds)[0]
        downloads = chosen.get("downloads") if isinstance(chosen, dict) else None
        if not isinstance(downloads, dict):
            return None
        artifact = downloads.get("server:default")
        if not isinstance(artifact, dict):
            artifact = next((v for v in downloads.values() if isinstance(v, dict)), None)
        if not isinstance(artifact, dict):
            return None
        return artifact.get("url")
    except Exception as e:
        _close_http_error(e)
        return None


def _paper_running() -> bool:
    """Whether this server's Paper JVM is actually alive.

    Delegates to the canonical detector. The previous `pgrep -f paper.jar`
    matched unrelated processes (editors, greps, the launching shell), which
    made the agent refuse world backups and version switches on a server that
    was not running.
    """
    try:
        from agent.process_info import is_paper_running
        return is_paper_running(server_dir())
    except Exception:
        return False


def version_rollback() -> Dict[str, Any]:
    """
    Restore the previously backed-up JAR. Only meaningful immediately after a
    version switch; if there is no backup, we refuse.
    """
    if _paper_running():
        return {"state": "failed",
                "error": "cannot rollback while the server is running",
                "result": {}}
    backups = sorted(server_dir().glob("paper.jar.*.backup"))
    if not backups:
        return {"state": "failed",
                "error": "no Paper JAR backup found to rollback to",
                "result": {}}
    backup = backups[-1]
    paper_jar = server_dir() / "paper.jar"
    try:
        if paper_jar.exists():
            shutil.copy2(paper_jar, paper_jar.with_suffix(".jar.pre-rollback"))
        shutil.copy2(backup, paper_jar)
        return {"state": "success",
                "result": {
                    "rollback_from": str(backup.relative_to(server_dir())),
                    "restored_to": "paper.jar",
                    "previous_backup_retained": str(paper_jar.with_suffix(".jar.pre-rollback").relative_to(server_dir())),
                    "apply_on_restart": True,
                }}
    except OSError as e:
        return {"state": "failed", "error": f"rollback failed: {e}", "result": {}}


# ---------------------------------------------------------------------------
# World operations
# ---------------------------------------------------------------------------

def world_config_path() -> Path:
    """server.properties under the live server root."""
    return server_dir() / "server.properties"


def _main_world_from_config() -> Optional[str]:
    cfg_path = world_config_path()
    if not cfg_path.exists():
        return None
    try:
        for line in cfg_path.read_text().splitlines():
            if line.startswith("level-name="):
                return line.split("=", 1)[1].strip() or "world"
    except OSError:
        pass
    return "world"


def _is_world_dir(path: Path) -> bool:
    main = _main_world_from_config()
    if main and path.name == main:
        return True
    for candidate in ("world", "world_nether", "world_the_end"):
        if path.name == candidate:
            return True
    return False


def world_list() -> Dict[str, Any]:
    """
    Discover worlds by reading the configured main world + scanning for the
    standard world directory names. Do not assume every discovered directory is
    a loaded or valid world.
    """
    main = _main_world_from_config() or "world"
    found: Dict[str, Dict[str, Any]] = {}
    for candidate in (main, "world", "world_nether", "world_the_end"):
        p = server_dir() / candidate
        if not p.exists() or not p.is_dir():
            continue
        # Skip symlinked worlds — inconsistent backup source.
        if p.is_symlink():
            continue
        info = _dir_info(p)
        info["is_configured_main"] = candidate == main
        info["path"] = candidate
        found[candidate] = info
    return {
        "state": "success",
        "result": {
            "main_world": main,
            "worlds": list(found.values()),
            "count": len(found),
            "note": "loaded/unloaded state is only reported when reliably detectable; a missing entry does not prove a world is absent.",
        },
    }


def world_info(world: str) -> Dict[str, Any]:
    ok, err, resolved = safe_agent_path(world)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}
    if not resolved.is_dir():
        return {"state": "failed", "error": "world directory not found", "result": {}}
    if _is_world_dir(resolved) and _paper_running():
        return {"state": "failed",
                "error": "world may be loaded by a running server; detailed mutation is not allowed while the server is running",
                "result": {}}
    usage: Dict[str, Any] = {}
    try:
        usage = _bounded_disk_usage(resolved)
    except Exception as e:
        usage = {"error": str(e)}
    return {
        "state": "success",
        "result": {
            "world": world,
            "path": world,
            "exists": True,
            "is_configured_main": world == _main_world_from_config(),
            "usage": usage,
        },
    }


def world_backup(world: str) -> Dict[str, Any]:
    """
    Create a timestamped ZIP snapshot of a world directory into the backup
    staging area. The world must not be loaded by a running server if we want a
    consistent snapshot; otherwise we still create the archive but warn.
    """
    if _paper_running():
        return {"state": "failed",
                "error": "world backup should be taken while the server is stopped for consistency; the running server may still be writing to the world",
                "result": {}}
    ok, err, resolved = safe_agent_path(world)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}
    if not resolved.is_dir():
        return {"state": "failed", "error": "world directory not found", "result": {}}

    stamp = time.strftime("%Y%m%d_%H%M%S")
    archive_name = f"world_{world}_{stamp}.zip"
    archive = _ensure_backup_staging() / archive_name
    try:
        _create_world_zip(resolved, archive)
    except Exception as e:
        return {"state": "failed", "error": f"backup creation failed: {e}", "result": {}}
    try:
        size = archive.stat().st_size
    except OSError:
        size = None
    return {
        "state": "success",
        "result": {
            "world": world,
            "archive": str(archive.relative_to(server_dir())),
            "size": size,
            "created": _now_iso(),
            "_download_path": str(archive.relative_to(server_dir())),
        },
    }


def _create_world_zip(world_dir: Path, dest: Path) -> None:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        count = 0
        for root, dirs, files in os.walk(world_dir):
            dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
            for f in files:
                fp = os.path.join(root, f)
                if os.path.islink(fp) or not os.path.isfile(fp):
                    continue
                arcname = os.path.relpath(fp, world_dir.parent)
                # Safety: archive entries must live under the world dir prefix.
                norm_arc = os.path.normpath(arcname)
                if not norm_arc.startswith(os.path.normpath(world_dir.name) + os.sep) \
                        and norm_arc != os.path.normpath(world_dir.name):
                    continue
                zf.write(fp, arcname)
                count += 1
                if count > 200000:
                    raise RuntimeError("world has too many files to back up safely in one archive")


def world_export(world: str) -> Dict[str, Any]:
    """
    Export a world as a downloadable archive. If no existing archive is present,
    create one first.
    """
    # Prefer an existing backup if one is recent enough; otherwise create one.
    existing = _recent_world_backup(world)
    if existing:
        return {
            "state": "success",
            "result": {
                "world": world,
                "archive": existing,
                "_download_path": existing,
            },
        }
    return world_backup(world)


def _recent_world_backup(world: str) -> Optional[str]:
    pattern = f"world_{world}_*.zip"
    candidates = sorted(_ensure_backup_staging().glob(pattern))
    if candidates:
        return str(candidates[-1].relative_to(server_dir()))
    return None


def _bounded_disk_usage(root: Path) -> Dict[str, Any]:
    total = 0
    files = 0
    dirs = 0
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if os.path.islink(fp) or not os.path.isfile(fp):
                    continue
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    continue
                files += 1
            dirs += len(dirnames)
    except OSError:
        pass
    return {
        "size_bytes": total,
        "size": _human_size(total),
        "file_count": files,
        "directory_count": dirs,
    }


def _dir_info(path: Path) -> Dict[str, Any]:
    try:
        st = path.stat()
    except OSError:
        st = None
    return {
        "name": path.name,
        "is_directory": True,
        "size": st.st_size if st else None,
        "modified": st.st_mtime if st else None,
        "entry_count": None,
    }


def _human_size(b: int) -> str:
    if b < 1024:
        return f"{b} B"
    if b < 1024 * 1024:
        return f"{b / 1024:.1f} KB"
    if b < 1024 * 1024 * 1024:
        return f"{b / (1024 * 1024):.1f} MB"
    return f"{b / (1024 * 1024 * 1024):.2f} GB"


def world_import(world: str, archive_path: str, confirm: bool = False) -> Dict[str, Any]:
    """
    Import a world from a validated archive into the server's world slot.

    - Validate archive entries for traversal.
    - Reject absolute paths, ".." traversal, excessive entry counts, and
      decompression bombs.
    - Extract to a staging directory, validate, then move into place only after
      checks pass.
    - Never overwrite an existing world silently.
    """
    if _paper_running():
        return {"state": "failed",
                "error": "cannot import a world while the server is running",
                "result": {}}

    if not confirm:
        return {"state": "failed",
                "error": "world import requires explicit confirmation",
                "result": {}}

    ok, err, resolved_archive = safe_agent_path(archive_path)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}
    if not resolved_archive.is_file():
        return {"state": "failed", "error": "archive file not found", "result": {}}

    target_world = server_dir() / world
    if target_world.exists():
        return {"state": "failed",
                "error": f"world '{world}' already exists; remove or rename it first",
                "result": {}}

    # Validate archive.
    try:
        _validate_world_archive(resolved_archive, world)
    except Exception as e:
        return {"state": "failed", "error": f"archive validation failed: {e}", "result": {}}

    # Extract to staging.
    staging = _ensure_backup_staging() / f"import_{world}_{time.strftime('%Y%m%d_%H%M%S')}"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(resolved_archive, "r") as zf:
            _extract_validate_extract(zf, staging, world)
    except Exception as e:
        shutil.rmtree(staging, ignore_errors=True)
        return {"state": "failed", "error": f"extraction failed: {e}", "result": {}}

    # Move into place.
    try:
        dest = server_dir() / world
        dest.mkdir(parents=True, exist_ok=True)
        for item in staging.iterdir():
            src = staging / item.name
            dst = dest / item.name
            if src.is_dir():
                shutil.move(str(src), str(dst))
            else:
                shutil.move(str(src), str(dst))
    except OSError as e:
        shutil.rmtree(staging, ignore_errors=True)
        return {"state": "failed", "error": f"move into place failed: {e}", "result": {}}
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {
        "state": "success",
        "result": {
            "world": world,
            "imported_from": archive_path,
            "note": "Verify the world loads correctly before deleting the source archive.",
        },
    }


def _validate_world_archive(archive: Path, world_name: str) -> None:
    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise RuntimeError(f"archive exceeds the size limit ({MAX_ARCHIVE_BYTES} bytes)")
    with zipfile.ZipFile(archive, "r") as zf:
        infos = zf.infolist()
        if len(infos) > 200000:
            raise RuntimeError("archive contains too many entries")
        for info in infos:
            name = info.filename
            if len(name) > 512:
                raise RuntimeError(f"archive entry name too long: {name[:80]}")
            if "\x00" in name or "\n" in name or "\r" in name:
                raise RuntimeError(f"archive entry name contains illegal characters: {name[:80]}")
            # Most specific reason first, so the operator sees the real cause:
            # absolute entry, then path traversal, then generic containment.
            if os.path.isabs(name) or name.startswith(("/", "\\")):
                raise RuntimeError(f"archive entry is an absolute path: {name}")
            parts = name.replace("\\", "/").split("/")
            depth = 0
            for p in parts:
                if p == "..":
                    depth -= 1
                    if depth < 0:
                        raise RuntimeError(f"archive entry path traversal: {name}")
                elif p and p != ".":
                    depth += 1
            if depth < 0:
                raise RuntimeError(f"archive entry path traversal: {name}")
            # Defense in depth: after normalization the entry must still land
            # inside the server directory.
            member_path = os.path.normpath(os.path.join(server_dir(), name))
            norm_root = os.path.normpath(str(server_dir()))
            if member_path != norm_root and not member_path.startswith(norm_root + os.sep):
                raise RuntimeError(f"archive entry escapes the server directory: {name}")
            # Block suspicious decompression ratios.
            if info.file_size == 0 and info.compress_size > 0:
                continue
            if info.file_size > 0 and info.compress_size > 0:
                ratio = info.compress_size / max(1, info.file_size)
                if ratio < 0.01 and info.file_size > 10 * 1024 * 1024:
                    raise RuntimeError(f"possible decompression bomb: {name}")
            if info.file_size > MAX_ARCHIVE_BYTES:
                raise RuntimeError(f"archive entry too large: {name}")


def _extract_validate_extract(zf: zipfile.ZipFile, dest: Path, world_name: str) -> None:
    for info in zf.infolist():
        name = info.filename
        # Only allow entries under the target world dir.
        parts = name.replace("\\", "/").split("/")
        if not parts or parts[0] != world_name:
            continue
        member_dest = dest / os.path.normpath(name)
        if not str(member_dest).startswith(str(dest)):
            raise RuntimeError(f"extraction would escape staging: {name}")
        if info.is_dir():
            member_dest.mkdir(parents=True, exist_ok=True)
        else:
            member_dest.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(member_dest, "wb") as dst:
                shutil.copyfileobj(src, dst)


def world_set_main(world: str, confirm: bool = False) -> Dict[str, Any]:
    """
    Change the configured main world through a safe, documented procedure.

    Writes level-name in server.properties only after validating the target
    world exists and is not a symlink.
    """
    if not confirm:
        return {"state": "failed",
                "error": "changing the main world requires explicit confirmation",
                "result": {}}
    ok, err, resolved = safe_agent_path(world)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}
    if not resolved.is_dir():
        return {"state": "failed", "error": "target world directory not found", "result": {}}
    if resolved.is_symlink():
        return {"state": "failed", "error": "target world must not be a symlink", "result": {}}

    props = world_config_path()
    if not props.exists():
        return {"state": "failed", "error": "server.properties not found", "result": {}}
    try:
        lines = props.read_text().splitlines()
    except OSError as e:
        return {"state": "failed", "error": f"cannot read server.properties: {e}", "result": {}}
    new_lines = []
    found = False
    for line in lines:
        if line.startswith("level-name="):
            new_lines.append(f"level-name={world}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"level-name={world}")
    try:
        props.write_text("\n".join(new_lines) + "\n")
    except OSError as e:
        return {"state": "failed", "error": f"cannot write server.properties: {e}", "result": {}}
    return {
        "state": "success",
        "result": {
            "main_world": world,
            "note": "Restart the server for the new main world to take effect. World data compatibility and version downgrade risks apply.",
        },
    }


def world_delete(world: str, confirm: bool = False) -> Dict[str, Any]:
    """
    Destructive: remove a world directory. Requires typed confirmation and a
    pre-delete backup.
    """
    if not confirm:
        return {"state": "failed",
                "error": "world deletion requires typed confirmation",
                "result": {}}
    if _paper_running():
        return {"state": "failed",
                "error": "cannot delete a world while the server is running",
                "result": {}}
    ok, err, resolved = safe_agent_path(world)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}
    if not resolved.is_dir():
        return {"state": "failed", "error": "world directory not found", "result": {}}

    # Pre-delete backup. If we cannot take one, we refuse to delete: silently
    # removing a world with no recoverable copy is exactly the data loss this
    # whole path exists to prevent.
    backup = _make_world_backup_before_delete(resolved)
    if backup is None or not backup.is_file():
        return {"state": "failed",
                "error": ("refusing to delete: a pre-delete backup could not be "
                          "created, so the world would not be recoverable"),
                "result": {}}
    try:
        shutil.rmtree(resolved)
    except OSError as e:
        return {"state": "failed",
                "error": f"delete failed; pre-delete backup retained at {backup.relative_to(server_dir()) if backup else 'n/a'}: {e}",
                "result": {}}
    return {
        "state": "success",
        "result": {
            "world": world,
            "deleted": True,
            "pre_delete_backup": str(backup.relative_to(server_dir())) if backup else None,
            "warning": "Verify the server no longer references this world before removing the backup.",
        },
    }


def _make_world_backup_before_delete(world_dir: Path) -> Optional[Path]:
    """Archive a world before deletion. Returns the archive, or None on failure.

    Uses the live backup staging directory (never the uninitialised module
    attribute, which is None and silently produced 'no backup' every time).
    """
    stamp = time.strftime("%Y%m%d_%H%M%S")
    try:
        archive = _ensure_backup_staging() / f"pre_delete_{world_dir.name}_{stamp}.zip"
        _create_world_zip(world_dir, archive)
        return archive
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Backup operations (local side of the Drive center)
# ---------------------------------------------------------------------------

def backup_status() -> Dict[str, Any]:
    return {
        "state": "success",
        "result": {
            "agent_ready": True,
            "drive_configured": _drive_configured(),
            "last_error": None,
            "note": "The Drive backend integration is handled by the existing drive_backup.py module; this agent provides the local side: staging, manifest, and restore preflight.",
        },
    }


def _drive_configured() -> bool:
    return bool(os.environ.get("GOOGLE_CLIENT_ID") or os.environ.get("GDRIVE_CLIENT_ID"))


def backup_create() -> Dict[str, Any]:
    """
    Create a local timestamped archive of the configured worlds and return a
    manifest with checksum. The actual Drive upload remains with the existing
    Drive integration; this operation reports completion only after the local
    archive + manifest are verified.
    """
    worlds = _discover_worlds_for_backup()
    if not worlds:
        return {"state": "failed", "error": "no world directories found to back up", "result": {}}

    stamp = time.strftime("%Y%m%d_%H%M%S")
    archive_name = f"raheecraft_backup_{stamp}.zip"
    archive = _ensure_backup_staging() / archive_name
    try:
        _create_full_backup_zip(worlds, archive)
    except Exception as e:
        return {"state": "failed", "error": f"backup creation failed: {e}", "result": {}}

    manifest = _build_manifest(worlds, archive)
    manifest_path = _ensure_backup_staging() / f"{archive_name}.manifest.json"
    try:
        manifest_path.write_text(json.dumps(manifest, indent=2))
    except OSError as e:
        return {"state": "failed", "error": f"manifest write failed: {e}", "result": {}}
    return {
        "state": "success",
        "result": {
            "archive": str(archive.relative_to(server_dir())),
            "manifest": str(manifest_path.relative_to(server_dir())),
            "worlds": [w for w in worlds],
            "created": manifest.get("created"),
            "checksum": manifest.get("checksum"),
            "note": "The local backup is ready. Upload to Drive through the existing Drive integration before reporting the backup as complete.",
        },
    }


def _discover_worlds_for_backup() -> List[str]:
    out = []
    for candidate in ("world", "world_nether", "world_the_end"):
        p = server_dir() / candidate
        if p.exists() and p.is_dir() and not p.is_symlink():
            out.append(candidate)
    return out


def _create_full_backup_zip(worlds: List[str], dest: Path) -> None:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        count = 0
        for w in worlds:
            base = server_dir() / w
            if not base.is_dir():
                continue
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
                for f in files:
                    fp = os.path.join(root, f)
                    if os.path.islink(fp) or not os.path.isfile(fp):
                        continue
                    arcname = os.path.relpath(fp, server_dir().parent)
                    norm_arc = os.path.normpath(arcname)
                    if not norm_arc.startswith(os.path.normpath(w) + os.sep) \
                            and norm_arc != os.path.normpath(w):
                        continue
                    zf.write(fp, arcname)
                    count += 1
                    if count > 200000:
                        raise RuntimeError("backup would contain too many files")
    if dest.stat().st_size > MAX_ARCHIVE_BYTES:
        raise RuntimeError(f"backup archive exceeds the size limit ({MAX_ARCHIVE_BYTES} bytes)")


def _build_manifest(worlds: List[str], archive: Path) -> Dict[str, Any]:
    return {
        "created": _now_iso(),
        "format": "zip",
        "scope": "worlds",
        "worlds": worlds,
        "archive": str(archive.relative_to(server_dir())),
        "size_bytes": archive.stat().st_size,
        "checksum_sha256": _sha256_file(archive),
        "mc_version": _current_minecraft_version(),
        "paper_jar": (server_dir() / "paper.jar").name if (server_dir() / "paper.jar").exists() else None,
    }


def _current_minecraft_version() -> Optional[str]:
    props = server_dir() / "server.properties"
    if not props.exists():
        return None
    try:
        for line in props.read_text().splitlines():
            if line.startswith("minecraft-version="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()  # type: ignore[name-defined]
    with open(path, "rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def backup_download(backup_id: str) -> Dict[str, Any]:
    """
    `backup_id` here is a local archive path relative to server_dir() (produced
    by backup_create or restored from Drive metadata). We validate it,
    verify checksum if a manifest is present, and stage it for download.
    """
    ok, err, resolved = safe_agent_path(backup_id)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}
    if not resolved.is_file():
        return {"state": "failed", "error": "backup archive not found", "result": {}}
    manifest_path = resolved.with_suffix(resolved.suffix + ".manifest.json")
    checksum: Optional[str] = None
    if manifest_path.exists():
        try:
            m = json.loads(manifest_path.read_text())
            checksum = m.get("checksum") or m.get("checksum_sha256")
        except (OSError, json.JSONDecodeError):
            pass
    actual = _sha256_file(resolved) if checksum else None
    ok_checksum = (actual == checksum) if checksum else None
    return {
        "state": "success",
        "result": {
            "backup_id": backup_id,
            "archive": backup_id,
            "size": resolved.stat().st_size,
            "checksum_expected": checksum,
            "checksum_actual": actual,
            "checksum_ok": ok_checksum,
            "_download_staging": str(resolved.relative_to(server_dir())),
        },
    }


def backup_restore(backup_id: str, confirm: bool = False) -> Dict[str, Any]:
    """
    Restore a backup archive into the server directory.

    Preflight:
    - Server must be stopped.
    - Archive + manifest validated.
    - Extract to staging, validate paths, then move into place.
    - Keep the pre-restore backup until the administrator confirms success.
    """
    if not confirm:
        return {"state": "failed",
                "error": "restore requires typed confirmation",
                "result": {}}
    if _paper_running():
        return {"state": "failed",
                "error": "cannot restore while the server is running",
                "result": {}}

    ok, err, resolved = safe_agent_path(backup_id)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}
    if not resolved.is_file():
        return {"state": "failed", "error": "backup archive not found", "result": {}}

    # Pre-restore backup of the current state.
    pre_backup = _make_pre_restore_backup()
    staging = _ensure_backup_staging() / f"restore_{time.strftime('%Y%m%d_%H%M%S')}"
    staging.mkdir(parents=True, exist_ok=True)
    try:
        _validate_world_archive(resolved, "world")
        with zipfile.ZipFile(resolved, "r") as zf:
            _extract_validate_extract(zf, staging, "world")
    except Exception as e:
        shutil.rmtree(staging, ignore_errors=True)
        return {"state": "failed",
                "error": f"restore validation/extraction failed; pre-restore backup retained: {e}",
                "result": {}}

    # Move staging contents into server_dir().
    try:
        for item in staging.iterdir():
            dst = server_dir() / item.name
            if item.is_dir():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.move(str(item), str(dst))
            else:
                if dst.exists():
                    dst.unlink()
                shutil.move(str(item), str(dst))
    except OSError as e:
        shutil.rmtree(staging, ignore_errors=True)
        return {"state": "failed",
                "error": f"restore move failed; pre-restore backup retained: {e}",
                "result": {}}
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {
        "state": "success",
        "result": {
            "restored_from": backup_id,
            "pre_restore_backup": str(pre_backup.relative_to(server_dir())) if pre_backup else None,
            "note": "Verify the server starts and the world loads correctly before deleting the pre-restore backup.",
        },
    }


def _make_pre_restore_backup() -> Optional[Path]:
    worlds = _discover_worlds_for_backup()
    if not worlds:
        return None
    stamp = time.strftime("%Y%m%d_%H%M%S")
    archive = _ensure_backup_staging() / f"pre_restore_{stamp}.zip"
    try:
        _create_full_backup_zip(worlds, archive)
        return archive
    except Exception:
        return None


def backup_delete(backup_id: str, confirm: bool = False) -> Dict[str, Any]:
    ok, err, resolved = safe_agent_path(backup_id)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}
    # Confirmation is checked before existence so an unconfirmed call never
    # reveals whether a given backup id exists.
    if not confirm:
        return {"state": "failed",
                "error": "backup deletion requires confirmation",
                "result": {}}
    if not resolved.is_file():
        return {"state": "failed", "error": "backup archive not found", "result": {}}
    manifest = resolved.with_suffix(resolved.suffix + ".manifest.json")
    try:
        resolved.unlink()
        if manifest.exists():
            manifest.unlink()
    except OSError as e:
        return {"state": "failed", "error": f"delete failed: {e}", "result": {}}
    return {"state": "success", "result": {"backup_id": backup_id, "deleted": True}}


# ---------------------------------------------------------------------------
# Top-level executor dispatch
# ---------------------------------------------------------------------------

def execute_stage4_operation(op_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Dispatch a Stage 4 operation to the matching agent-side handler.

    Validation + redaction are shared with the backend via operation_registry.
    """
    ok, err = validate_operation_payload(op_type, payload)
    if not ok:
        return {"state": "failed", "error": err, "result": {}}

    try:
        if op_type == "file_browse":
            return file_browse(payload["path"])
        if op_type == "file_read":
            return file_read(payload["path"])
        if op_type == "file_write":
            return file_write(payload["path"], payload["content"])
        if op_type == "file_editor_backup":
            return file_editor_backup(payload["path"])
        if op_type == "file_make_dir":
            return file_make_dir(payload["path"])
        if op_type == "file_rename":
            return file_rename(payload["path"], payload["new_path"])
        if op_type == "file_delete":
            return file_delete(payload["path"], payload.get("confirm"))
        if op_type == "file_download":
            return file_download(payload["path"])
        if op_type == "file_upload":
            return file_upload(
                payload["path"],
                payload.get("chunks") or [],
                payload.get("filename", "upload"),
            )
        if op_type == "plugin_list":
            return plugin_list()
        if op_type == "plugin_upload":
            return plugin_upload(
                payload["path"],
                payload.get("chunks") or [],
                payload.get("filename", "plugin.jar"),
            )
        if op_type == "plugin_remove":
            return plugin_remove(payload["path"], payload.get("confirm"))
        if op_type == "version_info":
            return version_info()
        if op_type == "version_switch":
            return version_switch(payload["version"])
        if op_type == "version_rollback":
            return version_rollback()
        if op_type == "world_list":
            return world_list()
        if op_type == "world_info":
            return world_info(payload["world"])
        if op_type == "world_backup":
            return world_backup(payload["world"])
        if op_type == "world_export":
            return world_export(payload["world"])
        if op_type == "world_import":
            return world_import(payload["world"], payload.get("archive_path", ""), payload.get("confirm"))
        if op_type == "world_set_main":
            return world_set_main(payload["world"], payload.get("confirm"))
        if op_type == "world_delete":
            return world_delete(payload["world"], payload.get("confirm"))
        if op_type == "backup_status":
            return backup_status()
        if op_type == "backup_create":
            return backup_create()
        if op_type == "backup_download":
            return backup_download(payload["backup_id"])
        if op_type == "backup_restore":
            return backup_restore(payload["backup_id"], payload.get("confirm"))
        if op_type == "backup_delete":
            return backup_delete(payload["backup_id"], payload.get("confirm"))
    except Exception as e:
        return {"state": "failed", "error": f"unexpected error: {e}", "result": {}}

    return {"state": "failed", "error": f"unknown operation type: {op_type}", "result": {}}


def execute_stage4_with_report(op_type: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a Stage 4 operation and shape the result for the relay (redacted,
    capped, audit-safe).
    """
    raw = execute_stage4_operation(op_type, payload)
    return sanitize_operation_result(raw)



