#!/usr/bin/env python3
"""
Filebase (S3-compatible) backup tests.

Filebase is the backup target when its access key and secret are present. The
backend must reuse the same archive scope as Google Drive (worlds, root player
state, plugins), select itself through the shared factory, need no interactive
OAuth, and never write outside the server directory on restore.

The S3 client is faked, so these run with no boto3 and no network.
"""

import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

os.environ.setdefault("SERVER_DIR", "/kaggle/working/minecraft_server")

from drive_backup import get_backup_client  # noqa: E402
from filebase_backup import FilebaseBackup, OBJECT_PREFIX  # noqa: E402


class FakeS3:
    """Minimal stand-in for boto3's S3 client: no network, in-memory objects."""

    def __init__(self):
        self.objects = {}
        self.deleted = []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        client = self

        class _Paginator:
            def paginate(self, Bucket, Prefix):
                stamp = datetime.now(timezone.utc)
                contents = [
                    {"Key": key, "Size": len(body), "LastModified": stamp}
                    for key, body in sorted(client.objects.items())
                    if key.startswith(Prefix)
                ]
                return [{"Contents": contents}]

        return _Paginator()

    def upload_file(self, path, bucket, key):
        with open(path, "rb") as fh:
            self.objects[key] = fh.read()

    def download_file(self, bucket, key, path):
        with open(path, "wb") as fh:
            fh.write(self.objects[key])

    def head_object(self, Bucket, Key):
        return {"ContentLength": len(self.objects[Key]),
                "LastModified": datetime.now(timezone.utc)}

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)


def _make_server(root: str) -> Path:
    root = Path(root)
    (root / "world" / "playerdata").mkdir(parents=True)
    (root / "world" / "level.dat").write_bytes(b"LEVEL")
    (root / "world" / "playerdata" / "u.dat").write_bytes(b"INV")
    (root / "ops.json").write_text("[]")
    (root / "plugins").mkdir()
    (root / "plugins" / "LuckPerms.jar").write_bytes(b"JAR")
    return root


class TestBackendSelection(unittest.TestCase):
    """The factory picks Filebase when it is configured, Drive otherwise."""

    def test_filebase_is_chosen_when_keys_are_present(self):
        env = {"FILEBASE_ACCESS_KEY": "k", "FILEBASE_SECRET_KEY": "s",
               "BACKUP_BACKEND": ""}
        with patch.dict(os.environ, env):
            self.assertIsInstance(get_backup_client(dry_run=True), FilebaseBackup)

    def test_drive_is_the_fallback_when_filebase_is_unset(self):
        env = {"FILEBASE_ACCESS_KEY": "", "FILEBASE_SECRET_KEY": "",
               "BACKUP_BACKEND": ""}
        with patch.dict(os.environ, env):
            self.assertNotIsInstance(get_backup_client(dry_run=True), FilebaseBackup)

    def test_backend_can_be_forced_to_drive(self):
        env = {"FILEBASE_ACCESS_KEY": "k", "FILEBASE_SECRET_KEY": "s",
               "BACKUP_BACKEND": "gdrive"}
        with patch.dict(os.environ, env):
            self.assertNotIsInstance(get_backup_client(dry_run=True), FilebaseBackup)


class _FilebaseTmp(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = _make_server(self._tmp.name)
        patch("drive_backup.SERVER_DIR", str(self.root)).start()
        patch("filebase_backup.SERVER_DIR", str(self.root)).start()
        self.addCleanup(patch.stopall)
        self.s3 = FakeS3()
        self.backup = FilebaseBackup(client=self.s3, bucket="test-bucket")

    def _members(self):
        key = next(iter(self.s3.objects))
        with zipfile.ZipFile(io.BytesIO(self.s3.objects[key])) as zf:
            return set(zf.namelist())


class TestCreateBackup(_FilebaseTmp):

    def test_uploaded_archive_carries_world_player_and_plugin_state(self):
        name = self.backup.create_backup(force=True)
        self.assertTrue(name and name.startswith("world_backup"))
        self.assertEqual(len(self.s3.objects), 1)
        self.assertTrue(next(iter(self.s3.objects)).startswith(OBJECT_PREFIX))
        members = self._members()
        self.assertIn("world/level.dat", members)
        self.assertIn("world/playerdata/u.dat", members)
        self.assertIn("ops.json", members)
        self.assertIn("plugins/LuckPerms.jar", members)

    def test_list_maps_objects_to_backup_metadata(self):
        self.backup.create_backup(force=True)
        listed = self.backup.list_backups()
        self.assertEqual(len(listed), 1)
        self.assertTrue(listed[0]["name"].startswith("world_backup"))
        self.assertGreater(listed[0]["size"], 0)
        self.assertIn("modifiedTime", listed[0])

    def test_an_unchanged_world_skips_a_second_backup(self):
        self.backup.create_backup(force=True)
        name = self.backup.create_backup()          # not forced
        self.assertEqual(len(self.s3.objects), 1, "no second upload expected")
        self.assertTrue(name and name.startswith("world_backup"))

    def test_no_credentials_is_reported_not_crashed(self):
        env = {"FILEBASE_ACCESS_KEY": "", "FILEBASE_SECRET_KEY": ""}
        with patch.dict(os.environ, env):
            unconfigured = FilebaseBackup()
            self.assertFalse(unconfigured.authenticated)
            self.assertIsNone(unconfigured.create_backup())
            self.assertFalse(unconfigured.restore_backup())
            self.assertEqual(unconfigured.list_backups(), [])

    def test_dry_run_needs_no_credentials(self):
        self.assertTrue(FilebaseBackup(dry_run=True).authenticated)

    def test_describe_never_leaks_the_keys(self):
        env = {"FILEBASE_ACCESS_KEY": "AKIA-SECRET", "FILEBASE_SECRET_KEY": "shh"}
        with patch.dict(os.environ, env):
            described = FilebaseBackup(bucket="test-bucket").describe()
        self.assertIn("test-bucket", described)
        self.assertNotIn("AKIA-SECRET", described)
        self.assertNotIn("shh", described)

    def test_old_backups_are_trimmed_to_the_cap(self):
        import filebase_backup
        with patch.object(filebase_backup, "MAX_BACKUPS", 2):
            for i in range(3):
                self.s3.objects[f"{OBJECT_PREFIX}world_backup_2026010{i}_000000.zip"] = b"x"
            self.backup._cleanup_old_backups()
        self.assertEqual(len(self.s3.objects), 2)
        self.assertEqual(len(self.s3.deleted), 1)


class TestRestoreBackup(_FilebaseTmp):

    def test_restore_returns_the_files_to_the_server_dir(self):
        self.backup.create_backup(force=True)
        shutil.rmtree(str(self.root / "world"))
        self.assertFalse((self.root / "world").exists())
        self.assertTrue(self.backup.restore_backup(force_replace=True))
        self.assertTrue((self.root / "world" / "level.dat").exists())
        self.assertTrue((self.root / "ops.json").exists())

    def test_restore_refuses_a_traversal_archive(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../escaped.txt", "x")
        key = f"{OBJECT_PREFIX}world_backup_evil.zip"
        self.s3.objects[key] = buf.getvalue()
        self.assertFalse(self.backup.restore_backup(backup_id=key,
                                                    force_replace=True))
        self.assertFalse((Path(self._tmp.name).parent / "escaped.txt").exists())

    def test_restore_without_a_backup_is_false_not_an_error(self):
        self.assertFalse(self.backup.restore_backup(force_replace=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
