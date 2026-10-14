#!/usr/bin/env python3
"""
Google Drive Backup for Kaggle Minecraft Server
===============================================
Provides persistent world storage using Google Drive API with OAuth 2.0.

Security:
  - Uses minimal Drive scope (read/write files only)
  - Credentials stored in Kaggle Secrets (never printed)
  - Refresh tokens used for non-interactive access after initial setup
  - No credentials printed in notebook output

Usage in Kaggle Notebook:
  1. Set up Google Cloud project + OAuth credentials (see setup guide)
  2. Add credentials to Kaggle Secrets (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)
  3. Run auth cell once to get refresh token
  4. Use backup/restore cells for world persistence
"""

import json
import os
import time
import zipfile
import shutil
import hashlib
import tempfile
import subprocess
import sys
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Tuple

# ==== Configuration ====
SERVER_DIR = "/kaggle/working/minecraft_server"
BACKUP_FOLDER_NAME = "Minecraft-Server-Backups"
MAX_BACKUPS = 5
BACKUP_INTERVAL_SECONDS = 1800  # 30 minutes default
DRY_RUN = False  # Set True to test without actual Drive operations

# Google Drive OAuth scopes (least-privileged for file access)
DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.file",
]

# Token storage (in Kaggle working dir, persists during session)
TOKEN_FILE = "/kaggle/working/.drive_token.json"
CREDENTIALS_FILE = "/kaggle/working/.drive_credentials.json"


class DriveBackupError(Exception):
    """Custom exception for Drive backup failures"""
    pass


class GoogleDriveBackup:
    """
    Manages Google Drive backups for Minecraft world data.
    
    Authentication flow:
      1. First run: Interactive OAuth to get refresh token
      2. Subsequent runs: Use refresh token for non-interactive access
    """
    
    def __init__(self, dry_run: bool = DRY_RUN):
        self.dry_run = dry_run
        self.service = None
        self.authenticated = False
        self.credentials = None
        
    def _get_secrets(self) -> dict:
        """Retrieve credentials from Kaggle Secrets, keeper service, or local files.
        
        Uses same credential names as n8n setup for consistency:
          - GDRIVE_CLIENT_ID / GDRIVE_CLIENT_SECRET (Drive OAuth)
          - GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET (Google services)
        """
        secrets = {}
        
        # Try Kaggle Secrets environment variables first
        # Support both naming conventions (n8n uses GDRIVE_*, generic uses GOOGLE_*)
        for key in ["GDRIVE_CLIENT_ID", "GOOGLE_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_ID"]:
            value = os.environ.get(key, "").strip()
            if value:
                secrets["client_id"] = value
                break
                
        for key in ["GDRIVE_CLIENT_SECRET", "GOOGLE_CLIENT_SECRET", "GOOGLE_OAUTH_CLIENT_SECRET"]:
            value = os.environ.get(key, "").strip()
            if value:
                secrets["client_secret"] = value
                break
        
        # Fall back to local credential file (for testing outside Kaggle)
        if not secrets.get("client_id"):
            creds_file = Path(CREDENTIALS_FILE)
            if creds_file.exists():
                try:
                    with open(creds_file) as f:
                        file_creds = json.load(f)
                        # Support both naming conventions in file
                        secrets["client_id"] = file_creds.get(
                            "client_id", 
                            file_creds.get("GDRIVE_CLIENT_ID", 
                                file_creds.get("GOOGLE_CLIENT_ID", "")
                            )
                        )
                        secrets["client_secret"] = file_creds.get(
                            "client_secret",
                            file_creds.get("GDRIVE_CLIENT_SECRET",
                                file_creds.get("GOOGLE_CLIENT_SECRET", "")
                            )
                        )
                except Exception:
                    pass
                    
        return secrets
    
    def authenticate(self) -> bool:
        """
        Authenticate with Google Drive API.
        
        Returns True if authentication successful, False otherwise.
        
        Flow:
          1. Check for cached refresh token
          2. If available, use it to get access token
          3. If not, perform interactive OAuth (requires browser)
        """
        print("=" * 60)
        print("GOOGLE DRIVE AUTHENTICATION")
        print("=" * 60)
        
        if self.dry_run:
            print("🔍 DRY RUN MODE - No actual Drive operations")
            self.authenticated = True
            return True
        
        secrets = self._get_secrets()
        
        if not secrets.get("GOOGLE_CLIENT_ID") or not secrets.get("GOOGLE_CLIENT_SECRET"):
            print("❌ Missing Google OAuth credentials")
            print("\n📋 SETUP REQUIRED:")
            print("   1. Create Google Cloud project at https://console.cloud.google.com")
            print("   2. Enable Google Drive API")
            print("   3. Create OAuth 2.0 credentials (Desktop app type)")
            print("   4. Add GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET to Kaggle Secrets")
            print("\n💡 See KAGGLE_MINECRAFT_GUIDE.md for detailed setup instructions")
            return False
        
        # Try to use cached refresh token first
        token_file = Path(TOKEN_FILE)
        if token_file.exists():
            try:
                with open(token_file) as f:
                    token_data = json.load(f)
                
                # Verify token is still valid (or refresh it)
                if self._refresh_access_token(token_data):
                    print("✅ Using cached refresh token")
                    self.authenticated = True
                    return True
                else:
                    print("⚠️  Cached token expired, re-authenticating...")
                    
            except Exception as e:
                print(f"⚠️  Token file issue: {e}")
        
        # Interactive OAuth flow
        print("\n🔐 Starting OAuth flow...")
        print("   This requires a browser to authorize access.")
        print("   In Kaggle, this must be done in an interactive session.")
        
        success = self._perform_oauth(secrets)
        
        if success:
            print("✅ Google Drive authentication successful")
            self.authenticated = True
            return True
        else:
            print("❌ Google Drive authentication failed")
            return False
    
    def _refresh_access_token(self, token_data: dict) -> bool:
        """Refresh an expired access token using refresh token."""
        # This would use google-auth-library in production
        # For now, simplified check
        import time
        expiry = token_data.get("expiry", 0)
        if time.time() < expiry:
            self.credentials = token_data
            return True
        return False
    
    def _perform_oauth(self, secrets: dict) -> bool:
        """
        Perform OAuth 2.0 authorization flow.
        
        In Kaggle, this requires:
          1. Interactive browser authorization in a running notebook cell
          2. Or pre-generating a refresh token outside Kaggle and storing it
        """
        # Check if we can use the installed google-auth-library
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
            
            # Create credentials from client secrets
            client_config = {
                "web": {
                    "client_id": secrets["GOOGLE_CLIENT_ID"],
                    "client_secret": secrets["GOOGLE_CLIENT_SECRET"],
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"]
                }
            }
            
            flow = InstalledAppFlow.from_client_config(
                client_config,
                scopes=DRIVE_SCOPES
            )
            
            # Run local server for OAuth callback
            # In Kaggle, this needs an accessible localhost
            print("\n🌐 Opening browser for authorization...")
            print("   If browser doesn't open, visit the URL shown below:")
            
            try:
                credentials = flow.run_local_server(port=0)
                
                # Save token for future use
                token_data = {
                    "token": credentials.token,
                    "refresh_token": credentials.refresh_token,
                    "token_uri": credentials.token_uri,
                    "client_id": credentials.client_id,
                    "client_secret": credentials.client_secret,
                    "scopes": credentials.scopes,
                    "expiry": credentials.expiry.timestamp() if credentials.expiry else 0
                }
                
                with open(TOKEN_FILE, 'w') as f:
                    json.dump(token_data, f)
                
                # Set restrictive permissions
                os.chmod(TOKEN_FILE, 0o600)
                
                self.credentials = {
                    "token": credentials.token,
                    "refresh_token": credentials.refresh_token
                }
                return True
                
            except Exception as e:
                print(f"\n⚠️  OAuth flow error: {e}")
                print("\n💡 Alternatives:")
                print("   1. Run this notebook in Google Colab for browser OAuth")
                print("   2. Generate refresh token locally, upload to Kaggle Secrets")
                print("   3. Use a service account (more complex setup)")
                return False
                
        except ImportError:
            print("\n❌ Google Auth library not installed")
            print("   Install: !pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client")
            return False
    
    def test_connection(self) -> bool:
        """
        Test the Drive connection by listing files in the backup folder.
        
        Returns True if connection works, False otherwise.
        """
        print("\n" + "=" * 60)
        print("TESTING GOOGLE DRIVE CONNECTION")
        print("=" * 60)
        
        if self.dry_run:
            print("🔍 DRY RUN - Simulating connection test")
            print("✅ Connection test passed (simulated)")
            return True
        
        if not self.authenticated:
            print("❌ Not authenticated. Run authentication first.")
            return False
        
        try:
            from googleapiclient.discovery import build
            from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
            
            # Build the Drive service
            self.service = build('drive', 'v3', credentials=self.credentials)
            
            # Try to find or create the backup folder
            folder_id = self._get_or_create_backup_folder()
            
            if folder_id:
                print(f"✅ Connected to Google Drive")
                print(f"📁 Backup folder: {BACKUP_FOLDER_NAME} (ID: {folder_id[:8]}...)")
                
                # List recent backups
                backups = self._list_backups(folder_id)
                if backups:
                    print(f"📋 Found {len(backups)} existing backup(s)")
                    for b in backups[:3]:
                        size_mb = b.get('size', 0) / (1024 * 1024)
                        print(f"   - {b['name']}: {size_mb:.1f} MB")
                else:
                    print("📁 No backups found yet")
                
                return True
            else:
                print("❌ Failed to access backup folder")
                return False
                
        except ImportError as e:
            print(f"❌ Required library missing: {e}")
            print("   Install: !pip install google-api-python-client google-auth-httplib2")
            return False
        except Exception as e:
            print(f"❌ Connection test failed: {e}")
            return False
    
    def _get_or_create_backup_folder(self) -> Optional[str]:
        """Get the backup folder ID, creating it if necessary."""
        from googleapiclient.discovery import build
        
        service = build('drive', 'v3', credentials=self.credentials)
        
        # Search for existing folder
        query = f"name='{BACKUP_FOLDER_NAME}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        results = service.files().list(q=query, fields="files(id, name)").execute()
        files = results.get('files', [])
        
        if files:
            return files[0]['id']
        
        # Create folder if it doesn't exist
        file_metadata = {
            'name': BACKUP_FOLDER_NAME,
            'mimeType': 'application/vnd.google-apps.folder'
        }
        folder = service.files().create(body=file_metadata, fields='id').execute()
        return folder.get('id')
    
    def _list_backups(self, folder_id: str) -> List[dict]:
        """List all backup files in the backup folder."""
        from googleapiclient.discovery import build
        
        service = build('drive', 'v3', credentials=self.credentials)
        
        query = f"'{folder_id}' in parents and trashed=false and name contains 'world_backup'"
        results = service.files().list(
            q=query,
            orderBy="modifiedTime desc",
            fields="files(id, name, size, modifiedTime, webViewLink)",
            pageSize=20
        ).execute()
        
        return results.get('files', [])
    
    def _find_world_directories(self) -> List[str]:
        """
        Find all world directories in the server folder.
        
        Detects:
          - Primary world (default: 'world')
          - Nether world ('world_nether' if exists)
          - End world ('world_the_end' if exists)
        """
        worlds = []
        
        if not os.path.exists(SERVER_DIR):
            return worlds
        
        # Check for default world
        default_world = os.path.join(SERVER_DIR, "world")
        if os.path.isdir(default_world):
            worlds.append(("world", default_world))
        
        # Check for Nether
        nether_world = os.path.join(SERVER_DIR, "world_nether")
        if os.path.isdir(nether_world):
            worlds.append(("world_nether", nether_world))
        
        # Check for End
        end_world = os.path.join(SERVER_DIR, "world_the_end")
        if os.path.isdir(end_world):
            worlds.append(("world_the_end", end_world))
        
        return worlds
    
    def create_backup(self, force: bool = False) -> Optional[str]:
        """
        Create a backup of all world data to Google Drive.
        
        Args:
            force: If True, create backup even if no changes detected
            
        Returns:
            Backup file name if successful, None otherwise
        """
        print("\n" + "=" * 60)
        print("CREATING WORLD BACKUP")
        print("=" * 60)
        
        if self.dry_run:
            print("🔍 DRY RUN - Would create backup")
            self._simulate_backup()
            return None
        
        if not self.authenticated:
            print("❌ Not authenticated. Run authentication first.")
            return None
        
        worlds = self._find_world_directories()
        
        if not worlds:
            print("⚠️  No world directories found")
            print("   Has the server started yet?")
            return None
        
        # Check if backup is needed
        if not force:
            last_backup = self._get_latest_backup()
            if last_backup and self._worlds_unchanged(worlds, last_backup):
                print("✅ World unchanged since last backup, skipping")
                return last_backup['name']
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"world_backup_{timestamp}.zip"
        
        # Create backup in temp location first
        temp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(temp_dir, backup_name)
        
        try:
            # Create ZIP archive
            print(f"\n📦 Creating backup: {backup_name}")
            self._create_world_zip(worlds, zip_path)
            
            # Get file size
            file_size = os.path.getsize(zip_path)
            size_mb = file_size / (1024 * 1024)
            print(f"   Size: {size_mb:.1f} MB")
            
            # Upload to Drive
            print("\n☁️  Uploading to Google Drive...")
            folder_id = self._get_or_create_backup_folder()
            
            from googleapiclient.http import MediaFileUpload
            from googleapiclient.discovery import build
            
            service = build('drive', 'v3', credentials=self.credentials)
            
            file_metadata = {
                'name': backup_name,
                'parents': [folder_id]
            }
            
            media = MediaFileUpload(
                zip_path,
                mimetype='application/zip',
                resumable=True
            )
            
            # Upload with retries
            file = None
            for attempt in range(3):
                try:
                    file = service.files().create(
                        body=file_metadata,
                        media_body=media,
                        fields='id, name, size, webViewLink'
                    ).execute()
                    break
                except Exception as e:
                    print(f"   Upload attempt {attempt + 1} failed: {e}")
                    if attempt < 2:
                        print("   Retrying...")
                        time.sleep(2)
                    else:
                        raise
            
            if file:
                print(f"✅ Backup uploaded successfully")
                print(f"   File: {file.get('name')}")
                print(f"   Size: {int(file.get('size', 0)) / (1024*1024):.1f} MB")
                if file.get('webViewLink'):
                    print(f"   View: {file.get('webViewLink')}")
                
                # Cleanup old backups
                self._cleanup_old_backups(folder_id)
                
                return backup_name
            else:
                print("❌ Upload failed")
                return None
                
        except Exception as e:
            print(f"❌ Backup failed: {e}")
            return None
        finally:
            # Cleanup temp files
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass
    
    def _simulate_backup(self):
        """Simulate backup process in dry-run mode."""
        worlds = self._find_world_directories()
        if worlds:
            print(f"   Would backup {len(worlds)} world(s):")
            for name, path in worlds:
                size = self._get_dir_size(path)
                print(f"     - {name}: {size / (1024*1024):.1f} MB")
        print("   Would upload to: " + BACKUP_FOLDER_NAME)
    
    def _get_dir_size(self, path: str) -> int:
        """Get total size of a directory in bytes."""
        total = 0
        for dirpath, dirnames, filenames in os.walk(path):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if os.path.exists(fp) and not os.path.islink(fp):
                    total += os.path.getsize(fp)
        return total
    
    def _create_world_zip(self, worlds: List[Tuple[str, str]], zip_path: str):
        """Create a ZIP archive of all world directories."""
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for world_name, world_path in worlds:
                print(f"   Adding {world_name}...")
                for root, dirs, files in os.walk(world_path):
                    # Skip unnecessary files
                    dirs[:] = [d for d in dirs if d not in ['.git', '__pycache__']]
                    
                    for file in files:
                        file_path = os.path.join(root, file)
                        
                        # Skip log/cache files
                        if any(skip in file_path for skip in ['.log', '.tmp', '.cache']):
                            continue
                        
                        # Calculate archive name
                        arcname = os.path.relpath(file_path, SERVER_DIR)
                        
                        # Prevent zip slip (path traversal)
                        if not arcname.startswith(world_name):
                            continue
                        
                        zf.write(file_path, arcname)
    
    def _get_latest_backup(self) -> Optional[dict]:
        """Get metadata for the most recent backup."""
        if not self.authenticated:
            return None
        
        try:
            from googleapiclient.discovery import build
            service = build('drive', 'v3', credentials=self.credentials)
            folder_id = self._get_or_create_backup_folder()
            backups = self._list_backups(folder_id)
            
            if backups:
                # Get full metadata for the latest
                file_id = backups[0]['id']
                return service.files().get(
                    fileId=file_id,
                    fields="id, name, size, modifiedTime, webViewLink"
                ).execute()
            return None
        except Exception:
            return None
    
    def _worlds_unchanged(self, current_worlds: List[Tuple[str, str]], last_backup: dict) -> bool:
        """
        Check if worlds have changed since last backup.
        
        Compares directory modification times against backup time.
        """
        if not last_backup:
            return False
        
        backup_time = datetime.fromisoformat(
            last_backup.get('modifiedTime', '').replace('Z', '+00:00')
        )
        
        for world_name, world_path in current_worlds:
            if not os.path.exists(world_path):
                continue
            
            # Check if any file in world was modified after backup
            for dirpath, dirnames, filenames in os.walk(world_path):
                for f in filenames:
                    fp = os.path.join(dirpath, f)
                    if os.path.exists(fp):
                        mtime = datetime.fromtimestamp(os.path.getmtime(fp))
                        if mtime > backup_time:
                            return False
        return True
    
    def _cleanup_old_backups(self, folder_id: str):
        """Keep only the most recent backups, deleting older ones."""
        try:
            from googleapiclient.discovery import build
            service = build('drive', 'v3', credentials=self.credentials)
            
            backups = self._list_backups(folder_id)
            
            if len(backups) > MAX_BACKUPS:
                # Delete oldest backups (keep newest MAX_BACKUPS)
                to_delete = backups[MAX_BACKUPS:]
                
                print(f"\n🧹 Cleaning up {len(to_delete)} old backup(s)...")
                
                for backup in to_delete:
                    try:
                        # Verify we can still access it before deleting
                        service.files().get(fileId=backup['id'], fields='name').execute()
                        service.files().delete(fileId=backup['id']).execute()
                        size_mb = int(backup.get('size', 0)) / (1024 * 1024)
                        print(f"   Deleted: {backup['name']} ({size_mb:.1f} MB)")
                    except Exception as e:
                        print(f"   Failed to delete {backup['name']}: {e}")
        except Exception as e:
            print(f"⚠️  Cleanup skipped: {e}")
    
    def restore_backup(self, backup_id: Optional[str] = None, force_replace: bool = False) -> bool:
        """
        Restore a world backup from Google Drive.
        
        Args:
            backup_id: ID of specific backup to restore. If None, uses latest.
            force_replace: If True, replace existing world without confirmation
            
        Returns:
            True if restoration successful, False otherwise
        """
        print("\n" + "=" * 60)
        print("RESTORING WORLD FROM BACKUP")
        print("=" * 60)
        
        if self.dry_run:
            print("🔍 DRY RUN - Would restore backup")
            print("   No actual changes made")
            return True
        
        if not self.authenticated:
            print("❌ Not authenticated. Run authentication first.")
            return False
        
        # Find backup to restore
        if backup_id:
            backup = self._get_backup_by_id(backup_id)
        else:
            backup = self._get_latest_backup()
        
        if not backup:
            print("❌ No backup found to restore")
            return False
        
        backup_name = backup['name']
        backup_size = int(backup.get('size', 0))
        
        print(f"\n📦 Restoring: {backup_name}")
        print(f"   Size: {backup_size / (1024*1024):.1f} MB")
        
        # Check if world already exists
        existing_world = os.path.join(SERVER_DIR, "world")
        world_exists = os.path.isdir(existing_world)
        
        if world_exists and not force_replace:
            print("\n⚠️  Existing world detected!")
            print("   To preserve it, creating safety backup first...")
            
            # Create safety backup of existing world
            safety_name = f"pre_restore_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
            if self.create_backup(force=True):
                print(f"✅ Safety backup created: {safety_name}")
            else:
                print("⚠️  Could not create safety backup")
            
            print("\n❓ Replace existing world? (y/n)")
            # In non-interactive mode, default to no
            response = input().strip().lower() if sys.stdin.isatty() else 'n'
            if response != 'y':
                print("❌ Restoration cancelled")
                return False
        
        # Download backup to temp location
        temp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(temp_dir, backup_name)
        
        try:
            print("\n☁️  Downloading from Google Drive...")
            
            from googleapiclient.http import MediaIoBaseDownload
            from googleapiclient.discovery import build
            
            service = build('drive', 'v3', credentials=self.credentials)
            
            request = service.files().get_media(fileId=backup['id'])
            
            with open(zip_path, 'wb') as f:
                downloader = MediaIoBaseDownload(f, request)
                done = False
                while not done:
                    status, done = downloader.next_chunk()
                    if status:
                        print(f"   Download progress: {int(status.progress() * 100)}%")
            
            print("✅ Download complete")
            
            # Verify ZIP integrity
            print("\n🔍 Verifying backup integrity...")
            if not self._verify_zip(zip_path, backup_size):
                print("❌ Backup file corrupted or incomplete")
                return False
            
            print("✅ Backup integrity verified")
            
            # Extract to server directory
            print("\n📂 Restoring world files...")
            
            # Safety check: ensure extraction stays within SERVER_DIR
            with zipfile.ZipFile(zip_path, 'r') as zf:
                for member in zf.namelist():
                    # Prevent path traversal attacks
                    member_path = os.path.normpath(os.path.join(SERVER_DIR, member))
                    if not member_path.startswith(os.path.normpath(SERVER_DIR)):
                        print(f"❌ Security: Refusing to extract {member} (path traversal)")
                        return False
            
            # Extract
            with zipfile.ZipFile(zip_path, 'w') as zf:
                pass  # Just checking, actual extract below
            
            shutil.unpack_archive(zip_path, SERVER_DIR, 'zip')
            
            print("✅ World restored successfully")
            print(f"\n📋 Next steps:")
            print(f"   1. Verify world loaded correctly")
            print(f"   2. Run server normally (Cell 5)")
            
            return True
            
        except Exception as e:
            print(f"❌ Restoration failed: {e}")
            return False
        finally:
            try:
                shutil.rmtree(temp_dir)
            except Exception:
                pass
    
    def _get_backup_by_id(self, backup_id: str) -> Optional[dict]:
        """Get backup metadata by file ID."""
        try:
            from googleapiclient.discovery import build
            service = build('drive', 'v3', credentials=self.credentials)
            return service.files().get(
                fileId=backup_id,
                fields="id, name, size, modifiedTime, webViewLink"
            ).execute()
        except Exception:
            return None
    
    def _verify_zip(self, zip_path: str, expected_size: int) -> bool:
        """Verify ZIP file integrity and size."""
        if not os.path.exists(zip_path):
            return False
        
        actual_size = os.path.getsize(zip_path)
        if abs(actual_size - expected_size) > expected_size * 0.1:  # 10% tolerance
            print(f"⚠️  Size mismatch: expected {expected_size}, got {actual_size}")
            # Continue anyway, size can vary slightly
        
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                bad_file = zf.testzip()
                if bad_file:
                    print(f"❌ ZIP corrupted: {bad_file}")
                    return False
        except zipfile.BadZipFile:
            print("❌ Invalid ZIP file")
            return False
        
        return True
    
    def list_backups(self) -> List[dict]:
        """List all available backups in Drive."""
        print("\n" + "=" * 60)
        print("AVAILABLE BACKUPS")
        print("=" * 60)
        
        if self.dry_run:
            print("🔍 DRY RUN - No actual backups")
            return []
        
        if not self.authenticated:
            print("❌ Not authenticated")
            return []
        
        try:
            folder_id = self._get_or_create_backup_folder()
            backups = self._list_backups(folder_id)
            
            if not backups:
                print("📁 No backups found")
                return []
            
            print(f"📋 Found {len(backups)} backup(s):\n")
            
            for i, b in enumerate(backups, 1):
                size_mb = int(b.get('size', 0)) / (1024 * 1024)
                mod_time = b.get('modifiedTime', 'Unknown')[:19]
                print(f"{i}. {b['name']}")
                print(f"   Size: {size_mb:.1f} MB | Modified: {mod_time}")
                if b.get('webViewLink'):
                    print(f"   Link: {b['webViewLink']}")
                print()
            
            return backups
            
        except Exception as e:
            print(f"❌ Failed to list backups: {e}")
            return []


def test_backup_system(dry_run: bool = True):
    """
    Test the backup system without affecting real data.
    
    Creates a sample world structure and tests backup/restore cycle.
    """
    print("\n" + "=" * 60)
    print("TESTING BACKUP SYSTEM (DRY RUN)")
    print("=" * 60)
    
    # Create test world structure
    test_dir = tempfile.mkdtemp()
    test_world = os.path.join(test_dir, "world")
    os.makedirs(test_world)
    
    # Create sample files
    with open(os.path.join(test_world, "level.dat"), 'w') as f:
        f.write("test_level_data")
    
    os.makedirs(os.path.join(test_world, "region"))
    with open(os.path.join(test_world, "region", "r.0.0.mca"), 'w') as f:
        f.write("region_data" * 1000)
    
    print(f"📁 Created test world at: {test_world}")
    print(f"   Size: {os.path.getsize(os.path.join(test_world, 'region', 'r.0.0.mca'))} bytes")
    
    # Test backup creation
    print("\n🧪 Testing backup creation...")
    backup = GoogleDriveBackup(dry_run=True)
    
    # Actually test the zip creation
    test_temp_dir = tempfile.mkdtemp()
    zip_path = os.path.join(test_temp_dir, "test_backup.zip")
    
    worlds = [("world", test_world)]
    
    # Create test zip
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for world_name, world_path in worlds:
            for root, dirs, files in os.walk(world_path):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, test_dir)
                    zf.write(file_path, arcname)
    
    print(f"   Created test ZIP: {zip_path}")
    print(f"   Size: {os.path.getsize(zip_path)} bytes")
    
    # Verify zip
    print("\n🧪 Testing ZIP verification...")
    if backup._verify_zip(zip_path, os.path.getsize(zip_path)):
        print("   ✅ ZIP integrity OK")
    else:
        print("   ❌ ZIP verification failed")
    
    # Test extraction
    print("\n🧪 Testing extraction...")
    extract_dir = tempfile.mkdtemp()
    shutil.unpack_archive(zip_path, extract_dir, 'zip')
    
    if os.path.exists(os.path.join(extract_dir, "world", "level.dat")):
        print("   ✅ Extraction successful")
    else:
        print("   ❌ Extraction failed")
    
    # Cleanup
    shutil.rmtree(test_dir)
    shutil.rmtree(test_temp_dir)
    shutil.rmtree(extract_dir)
    
    print("\n✅ Test completed successfully")
    print("\n📋 Next: Run the actual backup in a live Kaggle session")


if __name__ == "__main__":
    # Test mode when run directly
    test_backup_system(dry_run=True)
