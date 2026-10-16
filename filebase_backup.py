#!/usr/bin/env python3
"""
Filebase (S3-compatible) backup backend
=======================================
Stores the world/player/plugin backup archive in a Filebase bucket instead of
Google Drive. Filebase speaks the S3 API, so a normal access-key/secret-key pair
authenticates every call — no interactive OAuth, which is the part of the Drive
flow that cannot run headless in a Kaggle kernel.

What it reuses
--------------
`FilebaseBackup` subclasses `GoogleDriveBackup`, so the part that decides *what*
to back up is shared with the Drive backend: the world folders
(`world`, `world_nether`, `world_the_end`), the server-root player state
(`ops.json`, `whitelist.json`, `banned-*.json`, `usercache.json`) and the
`plugins/` tree, plus the change detection, ZIP-integrity check and
path-traversal guard. Only the object-storage transport is different.

Configuration (environment / Kaggle Secrets; never hardcoded)
-------------------------------------------------------------
  FILEBASE_ACCESS_KEY  Filebase access key
  FILEBASE_SECRET_KEY  Filebase secret key
  FILEBASE_BUCKET      bucket to store backups in (must already exist)
  FILEBASE_ENDPOINT    optional; default https://s3.filebase.com
  FILEBASE_GATEWAY     optional; the <account>.myfilebase.com IPFS gateway,
                       informational only (it is NOT the S3 API endpoint)
  BACKUP_BACKEND       optional; "filebase" (default when configured) or "gdrive"

Endpoint note: the S3 API lives at `https://s3.filebase.com` (objects are then
addressed as `<bucket>.s3.filebase.com`). `https://<account>.myfilebase.com` is
the public IPFS *gateway* and cannot be used to upload.
"""

import os
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from drive_backup import (  # noqa: E402
    GoogleDriveBackup,
    MAX_BACKUPS,
    SERVER_DIR,
    BACKUP_INTERVAL_SECONDS,
)

# Filebase's S3 API endpoint. NOT the <account>.myfilebase.com IPFS gateway.
DEFAULT_S3_ENDPOINT = "https://s3.filebase.com"
# Filebase accepts any region string; SigV4 still requires one.
DEFAULT_REGION = "us-east-1"
DEFAULT_BUCKET = "raheecraft-backups"
# All backups live under this key prefix inside the bucket.
OBJECT_PREFIX = "backups/"
BACKUP_NAME_PREFIX = "world_backup"

ACCESS_KEY_VAR = "FILEBASE_ACCESS_KEY"
SECRET_KEY_VAR = "FILEBASE_SECRET_KEY"
BUCKET_VAR = "FILEBASE_BUCKET"
ENDPOINT_VAR = "FILEBASE_ENDPOINT"
GATEWAY_VAR = "FILEBASE_GATEWAY"


class FilebaseBackup(GoogleDriveBackup):
    """World backups stored in a Filebase (S3) bucket."""

    def __init__(self, dry_run: bool = False, client: Any = None,
                 bucket: str = None, endpoint: str = None):
        super().__init__(dry_run=dry_run)
        self.bucket = (bucket if bucket is not None
                       else os.environ.get(BUCKET_VAR, "").strip() or DEFAULT_BUCKET)
        self.endpoint = ((endpoint if endpoint is not None
                          else os.environ.get(ENDPOINT_VAR, "").strip())
                         or DEFAULT_S3_ENDPOINT)
        self.gateway = os.environ.get(GATEWAY_VAR, "").strip()
        self.access_key = os.environ.get(ACCESS_KEY_VAR, "").strip()
        self.secret_key = os.environ.get(SECRET_KEY_VAR, "").strip()
        self._client = client
        # A dry run needs no credentials, and an injected client (tests) is
        # already authenticated.
        self.authenticated = bool(
            dry_run or client is not None
            or (self.access_key and self.secret_key))

    # --- transport -----------------------------------------------------------

    def _s3(self):
        """The boto3 S3 client, created lazily so tests need no boto3."""
        if self._client is not None:
            return self._client
        try:
            import boto3  # imported here: only a real run needs it
        except ImportError as e:  # a clear message beats a bare ImportError
            raise RuntimeError(
                "boto3 is required for Filebase backups "
                "(`pip install boto3`)"
            ) from e
        self._client = boto3.client(
            "s3",
            endpoint_url=self.endpoint,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=DEFAULT_REGION,
        )
        return self._client

    def _key(self, name: str) -> str:
        return f"{OBJECT_PREFIX}{name}"

    @staticmethod
    def _name_from_key(key: str) -> str:
        return key.rsplit("/", 1)[-1]

    def _upload(self, zip_path: str, name: str) -> None:
        self._s3().upload_file(zip_path, self.bucket, self._key(name))

    def _download(self, backup: dict, zip_path: str) -> None:
        self._s3().download_file(self.bucket, backup["id"], zip_path)

    # --- listing -------------------------------------------------------------

    def _list_backups(self, folder_id: str = None) -> List[dict]:
        """Backups in the bucket, newest first, in Drive's metadata shape."""
        client = self._s3()
        paginator = client.get_paginator("list_objects_v2")
        out: List[dict] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=OBJECT_PREFIX):
            for obj in page.get("Contents", []):
                key = obj.get("Key", "")
                name = self._name_from_key(key)
                if not name.startswith(BACKUP_NAME_PREFIX):
                    continue
                modified = obj.get("LastModified")
                if isinstance(modified, datetime):
                    if modified.tzinfo is None:
                        modified = modified.replace(tzinfo=timezone.utc)
                    stamp = modified.astimezone(timezone.utc).isoformat()
                else:
                    stamp = str(modified or "")
                out.append({
                    "id": key,
                    "name": name,
                    "size": obj.get("Size", 0),
                    "modifiedTime": stamp,
                })
        out.sort(key=lambda b: b["modifiedTime"], reverse=True)
        return out

    def list_backups(self) -> List[dict]:
        """List available backups (newest first)."""
        if not self.authenticated:
            print("❌ Filebase credentials not configured "
                  f"({ACCESS_KEY_VAR} / {SECRET_KEY_VAR}).")
            return []
        try:
            return self._list_backups()
        except Exception as e:
            print(f"⚠️  Could not list Filebase backups: {e}")
            return []

    def _get_latest_backup(self) -> Optional[dict]:
        try:
            backups = self._list_backups()
        except Exception:
            return None
        return backups[0] if backups else None

    def _get_backup_by_id(self, backup_id: str) -> Optional[dict]:
        """Metadata for one object, addressed by its key."""
        try:
            head = self._s3().head_object(Bucket=self.bucket, Key=backup_id)
        except Exception:
            return None
        modified = head.get("LastModified")
        stamp = (modified.astimezone(timezone.utc).isoformat()
                 if isinstance(modified, datetime) else str(modified or ""))
        return {
            "id": backup_id,
            "name": self._name_from_key(backup_id),
            "size": head.get("ContentLength", 0),
            "modifiedTime": stamp,
        }

    def _cleanup_old_backups(self, folder_id: str = None) -> None:
        """Keep only the newest MAX_BACKUPS objects under the prefix."""
        try:
            backups = self._list_backups()
            if len(backups) <= MAX_BACKUPS:
                return
            to_delete = backups[MAX_BACKUPS:]
            print(f"\n🧹 Cleaning up {len(to_delete)} old backup(s)...")
            for backup in to_delete:
                try:
                    self._s3().delete_object(Bucket=self.bucket, Key=backup["id"])
                    size_mb = int(backup.get("size", 0)) / (1024 * 1024)
                    print(f"   Deleted: {backup['name']} ({size_mb:.1f} MB)")
                except Exception as e:
                    print(f"   Failed to delete {backup['name']}: {e}")
        except Exception as e:
            print(f"⚠️  Cleanup skipped: {e}")

    # --- create / restore ----------------------------------------------------

    def create_backup(self, force: bool = False) -> Optional[str]:
        """Back up worlds, player state and plugins to Filebase."""
        print("\n" + "=" * 60)
        print("CREATING WORLDBACKUP (Filebase)")
        print("=" * 60)

        if self.dry_run:
            print("🔍 DRY RUN - Would create backup")
            self._simulate_backup()
            return None

        if not self.authenticated:
            print(f"❌ Filebase not configured. Set {ACCESS_KEY_VAR}, "
                  f"{SECRET_KEY_VAR} and {BUCKET_VAR}.")
            return None

        worlds = self._find_world_directories()
        if not worlds:
            print("⚠️  No world directories found")
            print("   Has the server started yet?")
            return None

        if not force:
            last_backup = self._get_latest_backup()
            if last_backup and self._worlds_unchanged(worlds, last_backup):
                print("✅ Nothing changed since the last backup, skipping")
                return last_backup["name"]

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{BACKUP_NAME_PREFIX}_{timestamp}.zip"
        temp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(temp_dir, backup_name)

        try:
            print(f"\n📦 Creating backup: {backup_name}")
            self._create_world_zip(worlds, zip_path)
            size_mb = os.path.getsize(zip_path) / (1024 * 1024)
            print(f"   Size: {size_mb:.1f} MB")

            print(f"\n☁️  Uploading to Filebase ({self.bucket})...")
            self._upload(zip_path, backup_name)
            print("✅ Backup uploaded successfully")
            print(f"   Object: {self._key(backup_name)}")

            self._cleanup_old_backups()
            return backup_name
        except Exception as e:
            print(f"❌ Backup failed: {e}")
            return None
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def restore_backup(self, backup_id: Optional[str] = None,
                       force_replace: bool = False) -> bool:
        """Restore the newest (or a named) backup from Filebase."""
        print("\n" + "=" * 60)
        print("RESTORING WORLD FROM BACKUP (Filebase)")
        print("=" * 60)

        if self.dry_run:
            print("🔍 DRY RUN - Would restore backup")
            print("   No actual changes made")
            return True

        if not self.authenticated:
            print(f"❌ Filebase not configured. Set {ACCESS_KEY_VAR}, "
                  f"{SECRET_KEY_VAR} and {BUCKET_VAR}.")
            return False

        backup = (self._get_backup_by_id(backup_id) if backup_id
                  else self._get_latest_backup())
        if not backup:
            print("❌ No backup found to restore")
            return False

        backup_name = backup["name"]
        backup_size = int(backup.get("size", 0))
        print(f"\n📦 Restoring: {backup_name}")
        print(f"   Size: {backup_size / (1024 * 1024):.1f} MB")

        existing_world = os.path.join(SERVER_DIR, "world")
        if os.path.isdir(existing_world) and not force_replace:
            print("\n⚠️  Existing world detected!")
            print("   To preserve it, creating safety backup first...")
            if self.create_backup(force=True):
                print("✅ Safety backup created")
            else:
                print("⚠️  Could not create safety backup")
            print("\n❓ Replace existing world? (y/n)")
            response = input().strip().lower() if sys.stdin.isatty() else "n"
            if response != "y":
                print("❌ Restoration cancelled")
                return False

        temp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(temp_dir, backup_name)
        try:
            print("\n☁️  Downloading from Filebase...")
            self._download(backup, zip_path)
            print("✅ Download complete")

            print("\n🔍 Verifying backup integrity...")
            if not self._verify_zip(zip_path, backup_size):
                print("❌ Backup file corrupted or incomplete")
                return False

            # Refuse any archive that could write outside the server directory.
            with zipfile.ZipFile(zip_path, "r") as zf:
                for member in zf.namelist():
                    member_path = os.path.normpath(os.path.join(SERVER_DIR, member))
                    if not member_path.startswith(os.path.normpath(SERVER_DIR)):
                        print(f"❌ Security: Refusing to extract {member} "
                              "(path traversal)")
                        return False

            print("\n📂 Restoring world files...")
            shutil.unpack_archive(zip_path, SERVER_DIR, "zip")
            print("✅ World restored successfully")
            print("\n📋 Next steps:")
            print("   1. Verify world loaded correctly")
            print("   2. Run server normally (Cell 5)")
            return True
        except Exception as e:
            print(f"❌ Restoration failed: {e}")
            return False
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def describe(self) -> str:
        """One-line, secret-free description for the notebook/panel."""
        return (f"Filebase bucket '{self.bucket}' via {self.endpoint} "
                f"(prefix {OBJECT_PREFIX})")


def test_backup_system(dry_run: bool = True) -> bool:
    """Exercise the Filebase backend without touching storage."""
    print("\n" + "=" * 60)
    print("FILEBASE BACKUP SYSTEM TEST (DRY RUN)" if dry_run
          else "FILEBASE BACKUP SYSTEM TEST")
    print("=" * 60)
    backup = FilebaseBackup(dry_run=dry_run)
    print(f"   {backup.describe()}")
    print(f"   access key: {'set' if backup.access_key else 'MISSING'} (hidden)")
    print(f"   secret key: {'set' if backup.secret_key else 'MISSING'} (hidden)")
    if backup.gateway:
        print(f"   gateway   : {backup.gateway} (IPFS gateway, not the S3 API)")
    backup.create_backup()
    return True


if __name__ == "__main__":
    test_backup_system(dry_run=True)
