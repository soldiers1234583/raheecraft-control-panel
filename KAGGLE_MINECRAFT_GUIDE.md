# 🎮 Free Minecraft Server on Kaggle — Quick Start Guide

This guide helps you set up a free Minecraft server using Kaggle Notebook's free compute resources.

---

## ⚠️ Important Limitations

Before you begin, understand these Kaggle constraints:

| Limitation | What It Means |
|------------|---------------|
| **9-hour max session** | Notebook runs for ~9 hours max, then stops |
| **No persistent storage** | World data is deleted when session ends |
| **No inbound connections** | Friends can't connect directly — need a tunnel |
| **RAM limited** | Free tier: ~13-16 GB, but use only 1-2 GB for Minecraft |
| **CPU limited** | Typically 2 cores on free tier |
| **Internet may be restricted** | Some Kaggle accounts have limited outbound access |

### Google Drive Backup Feature

This notebook includes **Google Drive backup** to persist your world between sessions:

- **Automatic backups** every 30 minutes (configurable)
- **Manual backups** on demand
- **Restore** previous worlds from Drive
- **Keep up to 5** recent backups (configurable)

⚠️ **Important:** Kaggle may terminate sessions without warning. Periodic backups help but are not guaranteed. Always do a manual backup before important sessions.

---

## 📋 Step-by-Step Setup

### Step 1: Create a Kaggle Notebook

1. Go to https://www.kaggle.com/code
2. Click "New Notebook"
3. Choose "Python" kernel
4. Rename it to something like "Minecraft Server"

### Step 2: Add the Setup Code

Copy the cells from `minecraft_kaggle_setup.ipynb` into your notebook.

**Also copy `drive_backup.py`** to the same directory in your Kaggle notebook project.

### Step 3: Run Cell 1 — Environment Diagnostics

This checks your environment and confirms Java, RAM, and internet access work.

**Expected output:**
- OS: Linux
- RAM: 13+ GB
- Java: Version 17+ (required for recent Minecraft versions)

If Java is missing, run this first:
```bash
!apt-get update && apt-get install -y default-jdk
```

### Step 4: Run Cell 2 — Configuration

Edit these settings before running:

```python
SERVER_EDITION = "java"  # Java Edition (recommended for Kaggle)
VIEW_DISTANCE = 6           # Lower = less lag
MAX_PLAYERS = 5
DIFFICULTY = "normal"
EULA_ACCEPTED = True        # Must be True!
ENABLE_DRIVE_BACKUP = True  # Enable Google Drive persistence
```

**Important:** 
- Set `EULA_ACCEPTED = True` only after reading https://minecraft.net/eula
- Set `ENABLE_DRIVE_BACKUP = True` to enable Google Drive world persistence

### Step 5: Run Cell 3 — Google Drive Authentication

**This is required for world persistence between sessions.**

1. Create a Google Cloud project at https://console.cloud.google.com
2. Enable the Google Drive API
3. Create OAuth 2.0 credentials (Desktop app type)
4. Add credentials to Kaggle Secrets:
   - `GOOGLE_CLIENT_ID`
   - `GOOGLE_CLIENT_SECRET`
5. Run Cell 3 to authenticate

When you run authentication, a browser window will open for you to grant access.

### Step 6: Run Cell 4 — Drive Connection Test

Verifies your Drive connection works and shows existing backups.

### Step 7: Run Cell 5 — World Restoration (Before Server Start)

Restores your world from Google Drive if a backup exists.

- If no world exists, it restores from the latest backup
- If world exists, it asks before overwriting (safety backup created first)

### Step 8: Run Cell 6 — Server Configuration

Generates:
- `eula.txt` — EULA acceptance
- `server.properties` — Server settings

### Step 9: Run Cell 7 — Start Server with Background Backups

Starts the server AND begins periodic Drive backups (every 30 min by default).

### Step 10: Run Cell 8 — Set Up Tunnel (Playit.gg)

For friends to connect, you need a tunnel since Kaggle has no public IP.

**Setup Playit.gg:**
1. Sign up at https://Playit.gg.com
2. Get your auth token from https://dashboard.Playit.gg.com/get-started/your-authtoken
3. Paste it in Cell 8 where it says `PLAYIT_TOKEN = ""`

```python
# Install pyPlayit.gg
!pip install pyPlayit.gg -q

from pyPlayit.gg import Playit.gg
Playit.gg.set_auth_token("YOUR_TOKEN_HERE")
tunnel = Playit.gg.connect(25565, "tcp")
print(f"Connect to: {tunnel.get_public_address()}")
```

### Step 11: Run Cell 9 — Connection Info

Shows your server config and the Playit.gg address to share with friends.

### Step 12: Run Cell 10 — Backup Status & Manual Operations

Shows backup history and provides manual backup/restore commands.

### Step 13: Run Cell 11 — Logs, Troubleshooting, Shutdown

Final cell with troubleshooting and shutdown procedures.

---

## 🎮 How Friends Connect

### For Java Edition (recommended):
1. Tell friends the Playit.gg address (e.g., `0.tcp.Playit.gg.io:12345`)
2. They open Minecraft → Multiplayer → Add Server
3. Server Address: `0.tcp.Playit.gg.io`
4. Server Port: `12345` (from Playit.gg URL)

### For Bedrock Edition:
If running Bedrock via tunnel:
- Address: Playit.gg TCP URL
- Port: 19132 (default Bedrock port, but tunnel forwards it)

---

## ☁️ Google Drive World Persistence

### Setup Google Cloud Project

1. Go to https://console.cloud.google.com
2. Create a new project (or select existing)
3. Enable **Google Drive API**:
   - APIs & Services → Library → Search "Google Drive API" → Enable
4. Configure OAuth consent screen:
   - APIs & Services → OAuth consent screen
   - Choose "External" user type
   - Fill in app name, user support email
   - Add scope: `https://www.googleapis.com/auth/drive.file`
   - Add test users (your email)
5. Create OAuth 2.0 credentials:
   - APIs & Services → Credentials → Create Credentials → OAuth client ID
   - Application type: **Desktop app**
   - Download the JSON file

### Add Credentials to Kaggle Secrets

1. In your Kaggle notebook, click **Secrets** (key icon on left panel)
2. Add credentials using one of these naming conventions:
   
   **Option A: Use your existing n8n credentials (if available)**
   - `GDRIVE_CLIENT_ID` — from your n8n/Drive OAuth setup
   - `GDRIVE_CLIENT_SECRET` — from your n8n/Drive OAuth setup
   
   **Option B: Generic naming**
   - `GOOGLE_CLIENT_ID` — from your OAuth credentials JSON
   - `GOOGLE_CLIENT_SECRET` — from your OAuth credentials JSON
   
   **Option C: n8n Google services naming**
   - `GOOGLE_OAUTH_CLIENT_ID`
   - `GOOGLE_OAUTH_CLIENT_SECRET`
3. Mark secrets as **Secret** (hidden from notebook output)

> 💡 **Tip:** If you already have Google credentials from your n8n setup (`kaggle-n8n/`), you can reuse them! Just copy the `GDRIVE_CLIENT_ID` and `GDRIVE_CLIENT_SECRET` values to Kaggle Secrets.

### How It Works

1. **First run:** Interactive OAuth in browser to grant access
2. **Refresh token saved:** Used for future non-interactive access
3. **Automatic backups:** Every 30 minutes during server runtime
4. **Manual backups:** Run `drive.create_backup()` anytime
5. **Restore:** Run `drive.restore_backup()` to restore from Drive

> 💡 **Connection to your n8n setup:** This uses the same Google Drive API as your `kaggle-n8n/` project. If you have a keeper service running that provides `GDRIVE_CLIENT_ID`/`GDRIVE_CLIENT_SECRET`, those credentials are used automatically. Otherwise, it falls back to Kaggle Secrets or local credential files.

### Backup Scope

- Backs up all world folders: `world`, `world_nether`, `world_the_end`
- Excludes logs, caches, temp files
- Creates timestamped ZIP archives
- Stored in Drive folder: **Minecraft-Server-Backups**
- Keeps last 5 backups (configurable via `MAX_BACKUPS_TO_KEEP`)

### Safety Features

- **Dry run mode:** Set `DRY_RUN = True` to test without actual Drive operations
- **Integrity checks:** ZIP verified before restore
- **Path traversal protection:** Extracts safely within server directory
- **Pre-restore backup:** Existing world backed up before overwrite
- **No silent overwrites:** Confirm before replacing existing world

---

## 🛠️ Troubleshooting

| Problem | Solution |
|---------|----------|
| **Java not found** | Run `!apt-get install -y default-jdk` first |
| **Server won't start** | Check EULA is accepted (`eula=true` in eula.txt) |
| **Out of memory** | Reduce `JAVA_XMX` to 1024 or 512 |
| **Can't connect from outside** | Verify Playit.gg tunnel is running, check the address |
| **Lag with multiple players** | Reduce `VIEW_DISTANCE` to 4, reduce player count |
| **World not saving** | Always run `stop` command in-game, don't just kill process |

---

## 📝 Notes

- **Session Timeout:** Kaggle stops notebooks after ~9 hours. Set a reminder.
- **World Loss:** Always backup before closing. Kaggle storage is temporary.
- **Address Changes:** Playit.gg URL changes every session. Share new one each time.
- **Player Limit:** Start small (2-3 players) and add more if performance is good.
- **Performance:** Paper server is already optimized. Additional plugins not recommended on free tier.
- **Drive Backup Reliability:** Kaggle may terminate sessions without warning. Periodic backups help but are not guaranteed. Always do manual backup before closing.
- **OAuth Limitation:** Interactive OAuth requires a browser. First auth must be done in an interactive Kaggle session with browser access.

---

## ✅ Checklist

### Setup
- [ ] Created Kaggle Notebook
- [ ] Uploaded `minecraft_kaggle_setup.ipynb` cells
- [ ] Uploaded `drive_backup.py` to notebook
- [ ] Ran Cell 1 (environment check)
- [ ] Edited Cell 2 settings (edition, players, difficulty)
- [ ] Set EULA_ACCEPTED = True after reading EULA
- [ ] Set ENABLE_DRIVE_BACKUP = True (optional)

### Google Drive Setup
- [ ] Created Google Cloud project
- [ ] Enabled Google Drive API
- [ ] Created OAuth 2.0 credentials (Desktop app)
- [ ] Added GOOGLE_CLIENT_ID to Kaggle Secrets
- [ ] Added GOOGLE_CLIENT_SECRET to Kaggle Secrets
- [ ] Ran Cell 3 (authentication)
- [ ] Verified Drive connection (Cell 4)

### Server Setup
- [ ] Ran Cell 3 (downloaded Paper server)
- [ ] Ran Cell 5 (world restoration from Drive)
- [ ] Ran Cell 6 (server configuration)
- [ ] Ran Cell 7 (started server)
- [ ] Verified server booted (check logs)

### Network & Backup
- [ ] Ran Cell 8 (Playit.gg tunnel)
- [ ] Shared Playit.gg address with friends
- [ ] Verified automatic backups working
- [ ] Created manual backup before closing

### Shutdown
- [ ] Typed 'save-all' in server console
- [ ] Typed 'stop' to shutdown gracefully
- [ ] Ran final Drive backup
- [ ] Stopped notebook kernel

---

## 📚 Resources

- [Minecraft EULA](https://minecraft.net/eula)
- [PaperMC Downloads](https://papermc.io/downloads)
- [Playit.gg](https://Playit.gg.com)
- [Kaggle Notebooks](https://www.kaggle.com/code)
- [Google Cloud Console](https://console.cloud.google.com)
- [Google Drive API Docs](https://developers.google.com/drive/api)

---

## 🛠️ Troubleshooting Drive Backup

| Problem | Solution |
|---------|----------|
| **OAuth fails** | Verify OAuth consent screen has your email as test user |
| **Credentials not found** | Check Kaggle Secrets has correct GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET |
| **Drive API not enabled** | Enable Google Drive API in Google Cloud Console |
| **Backup folder not created** | Check Drive permissions, re-run authentication |
| **Backup fails silently** | Set DRY_RUN=False to see actual errors |
| **World not restoring** | Verify backup exists with drive.list_backups() |

---

*For comparison with your existing Aternos Bedrock setup (with Better on Bedrock, Essentials+, Dynamic Light add-ons), see `~/minecraft-server-kit/GUIDE.md`. Note: those add-ons are for Bedrock/Aternos and won't work in this Kaggle Java setup.*
