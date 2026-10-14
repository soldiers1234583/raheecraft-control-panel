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

---

## 📋 Step-by-Step Setup

### Step 1: Create a Kaggle Notebook

1. Go to https://www.kaggle.com/code
2. Click "New Notebook"
3. Choose "Python" kernel
4. Rename it to something like "Minecraft Server"

### Step 2: Add the Setup Code

Copy the cells from `minecraft_kaggle_setup.ipynb` into your notebook, OR run this cell to download the full setup:

```python
# Download the complete setup notebook
!wget -q https://raw.githubusercontent.com/your-repo/minecraft-kaggle/main/setup.ipynb
```

Or manually copy the cells below.

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
SERVER_EDITION = "bedrock"  # or "java"
VIEW_DISTANCE = 6           # Lower = less lag
MAX_PLAYERS = 5
DIFFICULTY = "normal"
EULA_ACCEPTED = True        # Must be True!
```

**Important:** 
- Set `EULA_ACCEPTED = True` only after reading https://minecraft.net/eula
- For Bedrock, you need the Bedrock server binary (see note below)

### Step 5: Run Cell 3 — Download Server

For **Java Edition**, this downloads Paper (optimized Minecraft server):
```
https://api.papermc.io/v2/projects/paper/versions/1.21.4/builds/496/downloads/paper-1.21.4-496.jar
```

For **Bedrock Edition**, Kaggle doesn't provide native Bedrock binaries. You have two options:
- **Option A:** Use Java Edition (recommended for Kaggle)
- **Option B:** Run Bedrock locally and tunnel via ngrok

### Step 6: Run Cell 4 — Server Configuration

This generates:
- `eula.txt` — EULA acceptance
- `server.properties` — Server settings

### Step 7: Run Cell 5 — Start Server

This launches the Minecraft server process. Wait 1-3 minutes for world generation.

**To verify it's running:**
```python
# Check process
!ps aux | grep java
```

### Step 8: Run Cell 6 — Set Up Tunnel (ngrok)

For friends to connect, you need a tunnel since Kaggle has no public IP.

**Setup ngrok:**
1. Sign up at https://ngrok.com
2. Get your auth token from https://dashboard.ngrok.com/get-started/your-authtoken
3. Paste it in Cell 6 where it says `NGROK_AUTH_TOKEN = ""`

```python
# Install pyngrok
!pip install pyngrok -q

from pyngrok import ngrok
ngrok.set_auth_token("YOUR_TOKEN_HERE")
tunnel = ngrok.connect(25565, "tcp")
print(f"Connect to: {tunnel.public_url}")
```

### Step 9: Run Cell 7 — Connection Info

This shows your server config and the ngrok address to share with friends.

---

## 🎮 How Friends Connect

### For Java Edition (recommended):
1. Tell friends the ngrok address (e.g., `0.tcp.ngrok.io:12345`)
2. They open Minecraft → Multiplayer → Add Server
3. Server Address: `0.tcp.ngrok.io`
4. Server Port: `12345` (from ngrok URL)

### For Bedrock Edition:
If running Bedrock via tunnel:
- Address: ngrok TCP URL
- Port: 19132 (default Bedrock port, but tunnel forwards it)

---

## 💾 Backup Your World

**CRITICAL:** World data is lost when the session ends!

Before closing the notebook:
```python
# Create backup
!zip -r world_backup.zip /kaggle/working/minecraft_server/world/

# Download the backup
from google.colab import files
files.download('/kaggle/working/world_backup.zip')
```

### To Restore Later:
1. Upload `world_backup.zip` to a new Kaggle notebook
2. Extract: `!unzip world_backup.zip -d /kaggle/working/minecraft_server/`
3. Run Cell 5 to start the server

---

## 🛠️ Troubleshooting

| Problem | Solution |
|---------|----------|
| **Java not found** | Run `!apt-get install -y default-jdk` first |
| **Server won't start** | Check EULA is accepted (`eula=true` in eula.txt) |
| **Out of memory** | Reduce `JAVA_XMX` to 1024 or 512 |
| **Can't connect from outside** | Verify ngrok tunnel is running, check the address |
| **Lag with multiple players** | Reduce `VIEW_DISTANCE` to 4, reduce player count |
| **World not saving** | Always run `stop` command in-game, don't just kill process |

---

## 📝 Notes

- **Session Timeout:** Kaggle stops notebooks after ~9 hours. Set a reminder.
- **World Loss:** Always backup before closing. Kaggle storage is temporary.
- **Address Changes:** ngrok URL changes every session. Share new one each time.
- **Player Limit:** Start small (2-3 players) and add more if performance is good.
- **Performance:** Paper server is already optimized. Additional plugins not recommended on free tier.

---

## ✅ Checklist

- [ ] Created Kaggle Notebook
- [ ] Ran Cell 1 (environment check)
- [ ] Edited Cell 2 settings (edition, players, difficulty)
- [ ] Set EULA_ACCEPTED = True after reading EULA
- [ ] Ran Cell 3 (downloaded server)
- [ ] Ran Cell 5 (started server)
- [ ] Verified server booted (check logs)
- [ ] Ran Cell 6 (ngrok tunnel)
- [ ] Shared ngrok address with friends
- [ ] Backed up world before closing

---

## 📚 Resources

- [Minecraft EULA](https://minecraft.net/eula)
- [PaperMC Downloads](https://papermc.io/downloads)
- [ngrok](https://ngrok.com)
- [Kaggle Notebooks](https://www.kaggle.com/code)

---

*Better on Bedrock / Essentials+ add-ons from your existing setup won't work in this Kaggle environment. Those are for Aternos/other Bedrock hosts. For Kaggle, stick with vanilla Minecraft or Java Edition with minimal plugins.*
