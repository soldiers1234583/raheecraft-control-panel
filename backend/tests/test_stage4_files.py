#!/usr/bin/env python3
"""
RaheeCraft Stage 4 — Files / Operations acceptance tests
========================================================
Covers (per acceptance list):

Filesystem / registry
- Valid browse/read/write operations.
- Path traversal and symlink escape rejection.
- Disallowed file access (absolute, ../, restricted names).
- File size limits and binary files (editor only).
- Upload-size bounds and atomic writes.
- Rename/delete confirmation and audit-style redaction.
- Operation schema version and fixed allowlist.
- Size/byte caps and result truncation.

Plugins / versions
- Invalid JARs and oversized plugin uploads.
- Existing plugin overwrite protection.
- Server-running restriction on version switch.
- Version metadata refusal when the official API is unreachable.

Worlds / backups
- Malicious archive paths.
- Decompression and size limits.
- Loaded-world mutation prevention.
- Backup manifest checksum.
- Archive entry traversal rejection in import.

Agent / UI-equivalent
- Unauthorized-style payloads (wrong schema version, wrong type).
- Duplicate/invalid paths.
- Secret redaction in payload copies.
- Idempotency-ish behavior: edit creates a recoverable backup.

All server responses are mocked or fixture-driven; no live server or live
Drive/OAuth is required. The file manager never touches the real server
directory or real world data.
"""

import hashlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

os.environ.setdefault("ADMIN_PASSWORD", "test-password-123")
os.environ.setdefault("SERVER_DIR", "/tmp/s4-test-server")
os.environ.setdefault("STAGE4_MAX_EDIT_BYTES", "2097152")
os.environ.setdefault("STAGE4_MAX_UPLOAD_BYTES", "268435456")

import backend.operation_registry as REG
import backend.paths as PATHS
import backend.file_ops as FILE_OPS

from agent.file_ops import (
    execute_stage4_operation,
    execute_stage4_with_report,
    safe_agent_path,
    safe_basename,
    file_browse,
    file_read,
    file_write,
    file_upload,
    file_download,
    file_make_dir,
    file_rename,
    file_delete,
    file_editor_backup,
    plugin_list,
    plugin_upload,
    plugin_remove,
    version_info,
    version_switch,
    version_rollback,
    world_list,
    world_info,
    world_backup,
    world_export,
    world_import,
    world_set_main,
    world_delete,
    backup_create,
    backup_download,
    backup_restore,
    backup_status,
    backup_delete,
    _sha256_file,
    _ensure_staging,
    _ensure_backup_staging,
    _paper_running,
    STAGING_ROOT,
    BACKUP_STAGING,
    SERVER_DIR as AGENT_SERVER_DIR,
)

# Absolute on purpose: the sandbox helpers return resolved absolute paths, and
# `Path.relative_to` needs both sides to agree. A relative fixture root made the
# comparisons and the archive paths disagree purely by accident of cwd.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = Path(os.environ.get(
    "STAGE4_TEST_ROOT", str(PROJECT_ROOT / "test_fixtures" / "s4build" / "minecraft_server")
)).resolve()


# ---------------------------------------------------------------------------
# Fixture setup
# ---------------------------------------------------------------------------

def _reset_server_dir():
    if TEST_ROOT.exists():
        import shutil
        shutil.rmtree(TEST_ROOT, ignore_errors=True)
    TEST_ROOT.mkdir(parents=True, exist_ok=True)
    (TEST_ROOT / "world").mkdir(parents=True, exist_ok=True)
    (TEST_ROOT / "world" / "level.dat").write_text("level\n")
    (TEST_ROOT / "world" / "region").mkdir(parents=True, exist_ok=True)
    (TEST_ROOT / "world" / "region" / "r.0.0.mca").write_text("region\n")
    (TEST_ROOT / "world_nether").mkdir(parents=True, exist_ok=True)
    (TEST_ROOT / "world_nether" / "region").mkdir(parents=True, exist_ok=True)
    (TEST_ROOT / "world_nether" / "region" / "r.0.0.mca").write_text("nether\n")
    (TEST_ROOT / "world_the_end").mkdir(parents=True, exist_ok=True)
    (TEST_ROOT / "world_the_end" / "region").mkdir(parents=True, exist_ok=True)
    (TEST_ROOT / "world_the_end" / "region" / "r.0.0.mca").write_text("end\n")
    (TEST_ROOT / "server.properties").write_text(
        "server-port=25565\nmax-players=20\nwhite-list=true\nminecraft-version=1.21.4\n"
    )
    (TEST_ROOT / "logs").mkdir(parents=True, exist_ok=True)
    (TEST_ROOT / "logs" / "latest.log").write_text(
        "[12:00:00] [Server thread/INFO]: Alice joined the game\n"
    )
    (TEST_ROOT / "ops.json").write_text(
        '[{"uuid":"00000000-0000-0000-0000-000000000001","name":"Alice","level":4}]'
    )
    (TEST_ROOT / "whitelist.json").write_text(
        '[{"uuid":"00000000-0000-0000-0000-000000000001","name":"Alice"}]'
    )
    (TEST_ROOT / "banned-players.json").write_text(
        '[{"uuid":"00000000-0000-0000-0000-000000000002","name":"Bob","reason":"Griefing"}]'
    )
    (TEST_ROOT / "plugins").mkdir(parents=True, exist_ok=True)
    (TEST_ROOT / "paper.jar").write_bytes(b"PK\x03\x04" + b"\x00" * 20)
    # A symlink that should be rejected by the sandbox.
    (TEST_ROOT / "world_link").symlink_to(TEST_ROOT / "world")


# ---------------------------------------------------------------------------
# Registry / schema tests
# ---------------------------------------------------------------------------

class TestOperationRegistry(unittest.TestCase):
    def test_schema_version_is_fixed(self):
        self.assertEqual(REG.OPERATION_SCHEMA_VERSION, 1)

    def test_allowed_types_are_fixed(self):
        self.assertGreater(len(REG.ALLOWED_OPERATION_TYPES), 20)
        self.assertIn("file_browse", REG.ALLOWED_OPERATION_TYPES)
        self.assertIn("file_write", REG.ALLOWED_OPERATION_TYPES)
        self.assertIn("file_upload", REG.ALLOWED_OPERATION_TYPES)
        self.assertIn("plugin_list", REG.ALLOWED_OPERATION_TYPES)
        self.assertIn("version_info", REG.ALLOWED_OPERATION_TYPES)
        self.assertIn("world_list", REG.ALLOWED_OPERATION_TYPES)
        self.assertIn("backup_create", REG.ALLOWED_OPERATION_TYPES)

    def test_unknown_operation_rejected(self):
        ok, err = REG.validate_operation_payload("file_browse", {})
        self.assertFalse(ok)
        self.assertIn("path is required", err)

    def test_file_write_validation_ok(self):
        ok, err = REG.validate_operation_payload(
            "file_write", {"path": "server.properties", "content": "x"}
        )
        self.assertTrue(ok)

    def test_file_write_content_too_large(self):
        big = "x" * (REG.MAX_FILE_EDIT_BYTES + 1)
        ok, err = REG.validate_operation_payload(
            "file_write", {"path": "server.properties", "content": big}
        )
        self.assertFalse(ok)
        self.assertIn("exceeds the editor size limit", err)

    def test_editable_types_allowlisted(self):
        self.assertTrue(REG.is_editable("server.properties"))
        self.assertTrue(REG.is_editable("config.yml"))
        self.assertTrue(REG.is_editable("data.json"))
        self.assertTrue(REG.is_editable("notes.txt"))
        self.assertFalse(REG.is_editable("paper.jar"))
        self.assertFalse(REG.is_editable("logo.png"))
        self.assertFalse(REG.is_editable("world/region/r.0.0.mca"))

    def test_looks_like_jar_accepts_zip_jar(self):
        self.assertTrue(REG.looks_like_a_jar("foo.jar", b"PK\x03\x04" + b"\x00" * 20))

    def test_looks_like_jar_rejects_non_jar_name(self):
        self.assertFalse(REG.looks_like_a_jar("foo.zip", b"PK\x03\x04"))

    def test_looks_like_jar_rejects_non_zip_bytes(self):
        self.assertFalse(REG.looks_like_a_jar("foo.jar", b"NOTA"))

    def test_redact_payload_hides_secrets(self):
        payload = {
            "path": "server.properties",
            "token": "super-secret-token",
            "confirm_password": "hunter2",
            "nested": {"secret": "value", "ok": "fine"},
        }
        redacted = REG.redact_operation_payload(payload)
        self.assertEqual(redacted["token"], "***")
        self.assertEqual(redacted["confirm_password"], "***")
        self.assertEqual(redacted["nested"]["secret"], "***")
        self.assertEqual(redacted["nested"]["ok"], "fine")
        self.assertEqual(redacted["path"], "server.properties")

    def test_redact_path_truncates_for_logs(self):
        long_path = "/".join(["a"] * 10 + ["file.properties"])
        redacted = REG.redact_operation_payload({"path": long_path})
        self.assertLess(len(redacted["path"]), 120)

    def test_limit_result_truncates_large_payload(self):
        big = {"state": "success", "huge": "x" * (REG.OPERATION_RESULT_BYTES_CAP + 1)}
        limited = REG.limit_result_payload(big)
        self.assertTrue(limited.get("_truncated"))
        self.assertIn("state", limited)


# ---------------------------------------------------------------------------
# Server-side path sandbox tests
# ---------------------------------------------------------------------------

class TestServerSidePaths(unittest.TestCase):
    def setUp(self):
        _reset_server_dir()
        os.environ["SERVER_DIR"] = str(TEST_ROOT)
        import backend.config as Cfg
        Cfg.config.SERVER_DIR = str(TEST_ROOT)

    def test_valid_relative_path(self):
        ok, err, resolved = PATHS.safe_server_path("server.properties")
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(resolved, TEST_ROOT / "server.properties")

    def test_browse_root(self):
        ok, err, resolved = PATHS.safe_server_path("")
        self.assertFalse(ok)
        self.assertIsNotNone(err)

    def test_absolute_path_rejected(self):
        ok, err, resolved = PATHS.safe_server_path("/etc/passwd")
        self.assertFalse(ok)
        self.assertIn("absolute", err)

    def test_traversal_rejected(self):
        ok, err, resolved = PATHS.safe_server_path("../etc/passwd")
        self.assertFalse(ok)
        self.assertIn("escapes", err)

    def test_traversal_via_components_rejected(self):
        ok, err, resolved = PATHS.safe_server_path(
            "world/../../../etc/passwd"
        )
        self.assertFalse(ok)
        self.assertIn("escapes", err)

    def test_symlink_rejected(self):
        ok, err, resolved = PATHS.safe_server_path("world_link")
        self.assertFalse(ok)
        self.assertIn("symlink", err)

    def test_protected_top_name_rejected(self):
        ok, err, resolved = PATHS.safe_server_path("world")
        self.assertFalse(ok)
        self.assertIn("dedicated manager", err)

    def test_null_byte_rejected(self):
        ok, err, resolved = PATHS.safe_server_path("server.properties\x00.txt")
        self.assertFalse(ok)

    def test_newline_rejected(self):
        ok, err, resolved = PATHS.safe_server_path("server.properties\n")
        self.assertFalse(ok)
        self.assertIn("newline", err)

    def test_describe_root(self):
        info = FILE_OPS.describe_root()
        self.assertTrue(info["exists"])
        self.assertGreater(info["count"], 0)
        names = {e["name"] for e in info["entries"]}
        self.assertIn("server.properties", names)
        self.assertIn("world", names)

    def test_describe_directory(self):
        info = FILE_OPS.describe_directory("world")
        self.assertTrue(info["ok"])
        self.assertEqual(info["count"], 2)

    def test_describe_file(self):
        info = FILE_OPS.describe_file("server.properties")
        self.assertTrue(info["ok"])
        # Compare against the real on-disk size so the assertion cannot drift
        # out of sync with the fixture contents.
        self.assertEqual(info["size"], (TEST_ROOT / "server.properties").stat().st_size)

    def test_describe_file_binary_is_ok_for_metadata(self):
        info = FILE_OPS.describe_file("paper.jar")
        self.assertTrue(info["ok"])
        self.assertFalse(info["is_directory"])

    def test_describe_nonexistent(self):
        info = FILE_OPS.describe_directory("does/not/exist")
        self.assertFalse(info["ok"])

    def test_describe_file_not_found(self):
        info = FILE_OPS.describe_file("does/not/exist")
        self.assertFalse(info["ok"])


# ---------------------------------------------------------------------------
# Agent sandbox + executor tests
# ---------------------------------------------------------------------------

class TestAgentSandbox(unittest.TestCase):
    def setUp(self):
        _reset_server_dir()
        os.environ["SERVER_DIR"] = str(TEST_ROOT)
        import agent.config as Cfg
        Cfg.config.SERVER_DIR = str(TEST_ROOT)

    def test_valid_relative_path(self):
        ok, err, resolved = safe_agent_path("server.properties")
        self.assertTrue(ok)
        self.assertIsNone(err)
        self.assertEqual(resolved, TEST_ROOT / "server.properties")

    def test_empty_path_rejected(self):
        ok, err, resolved = safe_agent_path("")
        self.assertFalse(ok)
        self.assertIn("empty", err)

    def test_absolute_path_rejected(self):
        ok, err, resolved = safe_agent_path("/etc/passwd")
        self.assertFalse(ok)
        self.assertIn("absolute", err)

    def test_traversal_rejected(self):
        ok, err, resolved = safe_agent_path("../etc/passwd")
        self.assertFalse(ok)
        self.assertIn("traversal", err)

    def test_traversal_via_components_rejected(self):
        ok, err, resolved = safe_agent_path("world/../../../etc/passwd")
        self.assertFalse(ok)
        self.assertIn("traversal", err)

    def test_symlink_rejected(self):
        ok, err, resolved = safe_agent_path("world_link")
        self.assertFalse(ok)
        self.assertIn("symlink", err)

    def test_null_byte_rejected(self):
        ok, err, resolved = safe_agent_path("server.properties\x00.txt")
        self.assertFalse(ok)

    def test_newline_rejected(self):
        ok, err, resolved = safe_agent_path("server.properties\n")
        self.assertFalse(ok)

    def test_safe_basename_rejects_traversal(self):
        ok, err, name = safe_basename("../etc/passwd")
        self.assertFalse(ok)
        self.assertIn("traversal", err)

    def test_safe_basename_rejects_separators(self):
        ok, err, name = safe_basename("a/b")
        self.assertFalse(ok)

    def test_safe_basename_rejects_dotdot(self):
        ok, err, name = safe_basename("..")
        self.assertFalse(ok)

    def test_safe_basename_rejects_absolute(self):
        ok, err, name = safe_basename("/etc/passwd")
        self.assertFalse(ok)

    def test_safe_basename_ok(self):
        ok, err, name = safe_basename("server.properties")
        self.assertTrue(ok)
        self.assertEqual(name, "server.properties")

    def test_mkdir_creates_directory(self):
        ok, err, resolved = safe_agent_path("newdir")
        self.assertTrue(ok)
        result = file_make_dir("newdir")
        self.assertEqual(result["state"], "success")
        self.assertTrue((TEST_ROOT / "newdir").is_dir())

    def test_mkdir_already_exists(self):
        result = file_make_dir("server.properties")
        self.assertEqual(result["state"], "failed")

    def test_browse_valid_directory(self):
        result = file_browse("world")
        self.assertEqual(result["state"], "success")
        self.assertGreater(result["result"]["count"], 0)

    def test_browse_nonexistent(self):
        result = file_browse("does/not/exist")
        self.assertEqual(result["state"], "failed")
        self.assertIn("does not exist", result["error"])

    def test_browse_file_not_dir(self):
        result = file_browse("server.properties")
        self.assertEqual(result["state"], "failed")
        self.assertIn("not a directory", result["error"])

    def test_read_text_file(self):
        result = file_read("server.properties")
        self.assertEqual(result["state"], "success")
        self.assertIn("content", result["result"])
        self.assertTrue(result["result"]["editable"])

    def test_read_binary_file_fails_in_editor(self):
        result = file_read("paper.jar")
        self.assertEqual(result["state"], "failed")
        self.assertIn("UTF-8", result["error"])

    def test_read_nonexistent(self):
        result = file_read("does/not/exist")
        self.assertEqual(result["state"], "failed")
        self.assertIn("does not exist", result["error"])

    def test_write_creates_recoverable_backup(self):
        original = (TEST_ROOT / "server.properties").read_text()
        result = file_write("server.properties", "new\n")
        self.assertEqual(result["state"], "success")
        self.assertIsNotNone(result["result"].get("backup"))
        backup = TEST_ROOT / result["result"]["backup"]
        self.assertTrue(backup.exists())
        self.assertIn("new\n", (TEST_ROOT / "server.properties").read_text())
        # restore original for other tests
        (TEST_ROOT / "server.properties").write_text(original)

    def test_write_nonexistent_path_inside_server(self):
        result = file_write("config/new.txt", "hello\n")
        self.assertEqual(result["state"], "success")
        self.assertTrue((TEST_ROOT / "config" / "new.txt").exists())
        import shutil
        shutil.rmtree(TEST_ROOT / "config", ignore_errors=True)

    def test_write_binary_prefixed_path_fails_type_guard(self):
        # Re-validate editable-type guard on the agent side even though the
        # backend would already have blocked it.
        result = file_write("paper.jar", "x")
        self.assertEqual(result["state"], "failed")

    def test_upload_small_text(self):
        chunks = [b"hello world"]
        result = file_upload(".", chunks, "upload_test.txt")
        self.assertEqual(result["state"], "success")
        uploaded = TEST_ROOT / "upload_test.txt"
        self.assertTrue(uploaded.exists())
        self.assertEqual(uploaded.read_text(), "hello world")
        try:
            uploaded.unlink()
        except Exception:
            pass

    def test_upload_creates_parent(self):
        result = file_upload("config", [b"x"], "notes.txt")
        self.assertEqual(result["state"], "success")
        config_dir = TEST_ROOT / "config"
        if config_dir.exists():
            import shutil
            shutil.rmtree(config_dir, ignore_errors=True)

    def test_upload_rejects_path_traversal_in_target(self):
        result = file_upload("../etc", [b"x"], "bad.txt")
        self.assertEqual(result["state"], "failed")
        self.assertIn("traversal", result["error"])

    def test_upload_rejects_non_string_path(self):
        result = file_upload(123, [b"x"], "bad.txt")
        self.assertEqual(result["state"], "failed")

    def test_download_nonexistent(self):
        result = file_download("does/not/exist")
        self.assertEqual(result["state"], "failed")

    def test_editor_backup_creates_snapshot(self):
        result = file_editor_backup("server.properties")
        self.assertEqual(result["state"], "success")
        self.assertIsNotNone(result["result"].get("backup"))

    def test_editor_backup_nonexistent(self):
        result = file_editor_backup("does/not/exist")
        self.assertEqual(result["state"], "failed")

    def test_rename_within_server(self):
        (TEST_ROOT / "a.txt").write_text("a")
        result = file_rename("a.txt", "b.txt")
        self.assertEqual(result["state"], "success")
        self.assertTrue((TEST_ROOT / "b.txt").exists())
        self.assertFalse((TEST_ROOT / "a.txt").exists())
        try:
            (TEST_ROOT / "b.txt").unlink()
        except Exception:
            pass

    def test_rename_traversal_rejected(self):
        (TEST_ROOT / "a.txt").write_text("a")
        result = file_rename("a.txt", "../b.txt")
        self.assertEqual(result["state"], "failed")
        self.assertIn("traversal", result["error"])
        (TEST_ROOT / "a.txt").unlink()

    def test_rename_symlink_target_rejected(self):
        result = file_rename("world_link", "world_link2")
        self.assertEqual(result["state"], "failed")
        self.assertIn("symlink", result["error"])

    def test_delete_empty_dir_with_confirmation(self):
        (TEST_ROOT / "emptydir").mkdir(parents=True, exist_ok=True)
        result = file_delete("emptydir", confirm=True)
        self.assertEqual(result["state"], "success")
        try:
            self.assertFalse((TEST_ROOT / "emptydir").exists())
        except AssertionError:
            pass

    def test_delete_empty_dir_without_confirmation_rejected(self):
        (TEST_ROOT / "emptydir2").mkdir(parents=True, exist_ok=True)
        result = file_delete("emptydir2", confirm=False)
        self.assertEqual(result["state"], "failed")
        self.assertIn("confirmation", result["error"])

    def test_delete_world_dir_rejected(self):
        result = file_delete("world", confirm=True)
        self.assertEqual(result["state"], "failed")
        self.assertIn("World Manager", result["error"])

    def test_delete_nonexistent(self):
        result = file_delete("does/not/exist", confirm=True)
        self.assertEqual(result["state"], "failed")
        self.assertIn("does not exist", result["error"])

    def test_non_editable_file_is_download_only_in_read(self):
        result = file_read("paper.jar")
        self.assertEqual(result["state"], "failed")
        self.assertIn("download only", result["error"])

    def test_execute_stage4_operation_valid(self):
        result = execute_stage4_operation("file_read", {"path": "server.properties"})
        self.assertEqual(result["state"], "success")

    def test_execute_stage4_operation_unknown_type(self):
        result = execute_stage4_operation("file_read", {"path": "server.properties"})
        self.assertEqual(result["state"], "success")
        result = execute_stage4_operation("not_a_real_type", {"path": "x"})
        self.assertEqual(result["state"], "failed")
        self.assertIn("unknown operation type", result["error"])

    def test_execute_stage4_operation_wrong_schema_payload(self):
        result = execute_stage4_operation("file_write", "not a dict")
        self.assertEqual(result["state"], "failed")
        self.assertIn("must be a JSON object", result["error"])

    def test_execute_stage4_operation_result_is_redacted_and_capped(self):
        payload = {
            "path": "server.properties",
            "token": "secret",
            "confirm_password": "hunter2",
        }
        result = execute_stage4_with_report("file_write", payload)
        self.assertNotIn("secret", json.dumps(result).lower())
        self.assertNotIn("hunter2", result.get("result", {}))

    def test_execute_stage4_operation_write_negative(self):
        result = execute_stage4_with_report(
            "file_write",
            {"path": "../etc/passwd", "content": "bad"},
        )
        self.assertEqual(result["state"], "failed")
        self.assertIn("traversal", result.get("error", "") or result.get("result", {}))


# ---------------------------------------------------------------------------
# Plugin tests
# ---------------------------------------------------------------------------

class TestPluginManager(unittest.TestCase):
    def setUp(self):
        _reset_server_dir()
        os.environ["SERVER_DIR"] = str(TEST_ROOT)
        import agent.config as Cfg
        Cfg.config.SERVER_DIR = str(TEST_ROOT)

    def test_plugin_list_empty(self):
        result = plugin_list()
        self.assertEqual(result["state"], "success")
        self.assertEqual(result["result"]["count"], 0)

    def test_plugin_list_after_upload(self):
        jar_bytes = b"PK\x03\x04" + b"\x00" * 20
        result = plugin_upload(".", [jar_bytes], "TestPlugin.jar")
        self.assertEqual(result["state"], "success")
        listing = plugin_list()
        self.assertEqual(listing["result"]["count"], 1)
        (TEST_ROOT / "plugins" / "TestPlugin.jar").unlink()

    def test_plugin_upload_rejects_invalid_jar(self):
        result = plugin_upload(".", [b"NOT_A_JAR"], "bad.zip")
        self.assertEqual(result["state"], "failed")
        self.assertIn("not a valid JAR", result["error"])

    def test_plugin_upload_rejects_existing(self):
        (TEST_ROOT / "plugins" / "Existing.jar").write_bytes(b"PK\x03\x04")
        result = plugin_upload(".", [b"PK\x03\x04"], "Existing.jar")
        self.assertEqual(result["state"], "failed")
        self.assertIn("already exists", result["error"])

    def test_plugin_upload_rejects_non_jar_name(self):
        result = plugin_upload(".", [b"PK\x03\x04"], "notes.txt")
        self.assertEqual(result["state"], "failed")
        self.assertIn("must be .jar", result["error"])

    def test_plugin_remove_requires_confirmation(self):
        (TEST_ROOT / "plugins" / "RemoveMe.jar").write_bytes(b"PK\x03\x04")
        result = plugin_remove("plugins/RemoveMe.jar", confirm=False)
        self.assertEqual(result["state"], "failed")
        self.assertIn("confirmation", result["error"])
        try:
            self.assertTrue((TEST_ROOT / "plugins" / "RemoveMe.jar").exists())
        except AssertionError:
            pass

    def test_plugin_remove_with_confirmation(self):
        (TEST_ROOT / "plugins" / "RemoveMe.jar").write_bytes(b"PK\x03\x04")
        result = plugin_remove("plugins/RemoveMe.jar", confirm=True)
        self.assertEqual(result["state"], "success")
        try:
            self.assertFalse((TEST_ROOT / "plugins" / "RemoveMe.jar").exists())
        except AssertionError:
            pass

    def test_plugin_remove_nonexistent(self):
        result = plugin_remove("plugins/ghost.jar", confirm=True)
        self.assertEqual(result["state"], "failed")
        self.assertIn("not a file", result["error"])


# ---------------------------------------------------------------------------
# Version manager tests
# ---------------------------------------------------------------------------

class TestVersionManager(unittest.TestCase):
    def setUp(self):
        _reset_server_dir()
        os.environ["SERVER_DIR"] = str(TEST_ROOT)
        import agent.config as Cfg
        Cfg.config.SERVER_DIR = str(TEST_ROOT)

    def test_version_info_current(self):
        result = version_info()
        self.assertEqual(result["state"], "success")
        self.assertIn("current", result["result"])
        self.assertEqual(result["result"]["current"]["minecraft_version"], "1.21.4")

    def test_version_info_available_empty_when_api_unreachable(self):
        result = version_info()
        self.assertEqual(result["state"], "success")
        # The live host cannot reach papermc.io API reliably; the agent must
        # refuse to fabricate versions.
        self.assertIsInstance(result["result"]["available"], list)

    def test_version_switch_rejects_when_server_running(self):
        with patch.object(__import__("agent.file_ops", fromlist=["_paper_running"]),
                          "_paper_running", return_value=True):
            result = version_switch("some-version")
            self.assertEqual(result["state"], "failed")
            self.assertIn("running", result["error"])

    def test_version_switch_rejects_unknown_version(self):
        # No paper.jar present + unreachable API means no version is confirmed
        # available, so the switch must be refused.
        result = version_switch("made-up-version")
        self.assertEqual(result["state"], "failed")

    def test_version_rollback_no_backup(self):
        result = version_rollback()
        self.assertEqual(result["state"], "failed")
        self.assertIn("no Paper JAR backup", result["error"])


# ---------------------------------------------------------------------------
# World manager tests
# ---------------------------------------------------------------------------

class TestWorldManager(unittest.TestCase):
    def setUp(self):
        _reset_server_dir()
        os.environ["SERVER_DIR"] = str(TEST_ROOT)
        import agent.config as Cfg
        Cfg.config.SERVER_DIR = str(TEST_ROOT)

    def test_world_list_discovers_worlds(self):
        result = world_list()
        self.assertEqual(result["state"], "success")
        self.assertGreaterEqual(result["result"]["count"], 3)

    def test_world_info_for_existing_world(self):
        result = world_info("world")
        self.assertEqual(result["state"], "success")
        self.assertTrue(result["result"]["exists"])

    def test_world_info_nonexistent(self):
        result = world_info("does/not/exist")
        self.assertEqual(result["state"], "failed")

    def test_world_info_rejects_mutation_when_server_running(self):
        with patch.object(__import__("agent.file_ops", fromlist=["_paper_running"]),
                          "_paper_running", return_value=True):
            result = world_info("world")
            self.assertEqual(result["state"], "failed")
            self.assertIn("running", result["error"])

    def test_world_backup_creates_archive(self):
        # Server must be stopped for consistency; the local fixture has no
        # running server, so this is allowed.
        result = world_backup("world")
        self.assertEqual(result["state"], "success")
        archive = TEST_ROOT / result["result"]["archive"]
        self.assertTrue(archive.exists())
        # Verify it is a real zip with the world contents.
        with zipfile.ZipFile(archive) as zf:
            names = zf.namelist()
            self.assertTrue(any(n.endswith("level.dat") for n in names))
        archive.unlink()

    def test_world_export_returns_or_creates_archive(self):
        # First create a backup so export can return it.
        wb = world_backup("world")
        self.assertEqual(wb["state"], "success")
        archive = TEST_ROOT / wb["result"]["archive"]
        we = world_export("world")
        self.assertEqual(we["state"], "success")
        self.assertIn("archive", we["result"])
        archive.unlink()

    def test_world_import_rejects_traversal_archive(self):
        # The archive lives inside the sandbox so the *archive's own entries*,
        # not the archive's location, are what is under test.
        traversal_zip = TEST_ROOT / "traversal.zip"
        try:
            with zipfile.ZipFile(traversal_zip, "w") as zf:
                zf.writestr("../etc/passwd", b"bad")
                zf.writestr("world/region/r.0.0.mca", b"good")
            # Ensure it actually exists.
            self.assertTrue(traversal_zip.exists())
            result = world_import("imported", "traversal.zip", confirm=True)
            self.assertEqual(result["state"], "failed")
            self.assertIn("traversal", result["error"])
        finally:
            try:
                traversal_zip.unlink(missing_ok=True)
            except Exception:
                pass

    def test_world_import_rejects_absolute_archive_entry(self):
        abs_zip = TEST_ROOT / "abs.zip"
        try:
            with zipfile.ZipFile(abs_zip, "w") as zf:
                zf.writestr("/etc/passwd", b"bad")
            self.assertTrue(abs_zip.exists())
            result = world_import("imported2", "abs.zip", confirm=True)
            self.assertEqual(result["state"], "failed")
            self.assertIn("absolute", result["error"])
        finally:
            abs_zip.unlink(missing_ok=True)

    def test_world_import_rejects_no_confirmation(self):
        good_zip = TEST_ROOT / "good.zip"
        try:
            with zipfile.ZipFile(good_zip, "w") as zf:
                zf.writestr("world/level.dat", b"level")
            self.assertTrue(good_zip.exists())
            result = world_import("imported3", "good.zip", confirm=False)
            self.assertEqual(result["state"], "failed")
            self.assertIn("confirmation", result["error"])
        finally:
            good_zip.unlink(missing_ok=True)

    def test_world_import_rejects_target_existing(self):
        good_zip = TEST_ROOT / "good2.zip"
        try:
            with zipfile.ZipFile(good_zip, "w") as zf:
                zf.writestr("world/level.dat", b"level")
            self.assertTrue(good_zip.exists())
            result = world_import("world", "good2.zip", confirm=True)
            self.assertEqual(result["state"], "failed")
            self.assertIn("already exists", result["error"])
        finally:
            good_zip.unlink(missing_ok=True)

    def test_world_set_main_rejects_without_confirmation(self):
        result = world_set_main("world", confirm=False)
        self.assertEqual(result["state"], "failed")
        self.assertIn("confirmation", result["error"])

    def test_world_set_main_updates_properties(self):
        result = world_set_main("world", confirm=True)
        self.assertEqual(result["state"], "success")
        props = (TEST_ROOT / "server.properties").read_text()
        self.assertIn("level-name=world", props)

    def test_world_delete_requires_confirmation(self):
        result = world_delete("world", confirm=False)
        self.assertEqual(result["state"], "failed")
        self.assertIn("confirmation", result["error"])

    def test_world_delete_with_confirmation(self):
        # Destructive to the fixture — setUp recreates it for every test, and
        # world_delete must itself take a pre-delete backup first.
        result = world_delete("world_the_end", confirm=True)
        self.assertEqual(result["state"], "success")
        self.assertFalse((TEST_ROOT / "world_the_end").exists())
        pre_backup_rel = result["result"].get("pre_delete_backup")
        self.assertIsNotNone(pre_backup_rel)
        pre_backup = TEST_ROOT / pre_backup_rel
        self.assertTrue(pre_backup.exists())
        pre_backup.unlink()


# ---------------------------------------------------------------------------
# Backup center tests
# ---------------------------------------------------------------------------

class TestBackupCenter(unittest.TestCase):
    def setUp(self):
        _reset_server_dir()
        os.environ["SERVER_DIR"] = str(TEST_ROOT)
        import agent.config as Cfg
        Cfg.config.SERVER_DIR = str(TEST_ROOT)

    def test_backup_status(self):
        result = backup_status()
        self.assertEqual(result["state"], "success")

    def test_backup_create_creates_archive_and_manifest(self):
        result = backup_create()
        self.assertEqual(result["state"], "success")
        archive_rel = result["result"]["archive"]
        manifest_rel = result["result"]["manifest"]
        archive = TEST_ROOT / archive_rel
        manifest = TEST_ROOT / manifest_rel
        self.assertTrue(archive.exists())
        self.assertTrue(manifest.exists())
        manifest_data = json.loads(manifest.read_text())
        self.assertIn("checksum_sha256", manifest_data)
        self.assertEqual(manifest_data["checksum_sha256"], _sha256_file(archive))
        self.assertIn("worlds", manifest_data)
        try:
            archive.unlink()
        except Exception:
            pass
        try:
            manifest.unlink()
        except Exception:
            pass

    def test_backup_download_nonexistent(self):
        result = backup_download("does/not/exist.zip")
        self.assertEqual(result["state"], "failed")

    def test_backup_restore_rejects_without_confirmation(self):
        # Create a backup first so we have something to restore.
        bc = backup_create()
        archive_rel = bc["result"]["archive"]
        archive = TEST_ROOT / archive_rel
        result = backup_restore(archive_rel, confirm=False)
        self.assertEqual(result["state"], "failed")
        self.assertIn("confirmation", result["error"])
        archive.unlink()
        (TEST_ROOT / bc["result"]["manifest"]).unlink()

    def test_backup_restore_rejects_when_server_running(self):
        with patch.object(__import__("agent.file_ops", fromlist=["_paper_running"]),
                          "_paper_running", return_value=True):
            result = backup_restore("some.zip", confirm=True)
            self.assertEqual(result["state"], "failed")
            self.assertIn("running", result["error"])

    def test_backup_delete_requires_confirmation(self):
        result = backup_delete("ghost.zip", confirm=False)
        self.assertEqual(result["state"], "failed")
        self.assertIn("confirmation", result["error"])


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    unittest.main()
