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
- Java: **21 or newer** — Paper for Minecraft 1.21.x will not start on 17

Kaggle's image already ships **OpenJDK 21** (verified by a live run on Kaggle:
`openjdk version "21.0.12"`, no install needed). If Cell 1 reports a lower
version, install 21 explicitly — do **not** use `default-jdk`, which can install
an older JVM:

```bash
!apt-get update && apt-get install -y openjdk-21-jdk-headless
```

The server refuses to start on an older JVM and names the version it found, so a
Java mismatch is reported rather than showing up as an unexplained crash.

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

### Step 8: Run Cell 6 — Paper JAR Download and Verification

Downloads the official Paper build for the configured Minecraft version and
installs it as `paper.jar`.

- Uses the **official PaperMC API only** — no mirrors.
- Verifies HTTP status, announced size, a size cap, ZIP integrity, and the
  SHA-256 the API publishes for the build.
- Downloads to a temp file and atomically replaces `paper.jar`, so an
  interrupted run never leaves a broken server JAR.
- **Never replaces an existing `paper.jar`.** Set `PAPER_FORCE_DOWNLOAD=1` only
  when you deliberately want to replace it.

### Step 9: Run Cell 7 — Server Configuration

Generates:
- `eula.txt` — EULA acceptance
- `server.properties` — Server settings (`level-name`, `server-port`, players,
  view distance)

### Step 10: Run Cell 8 — Start the Control Agent

Runs the Kaggle-side agent in a background thread. **This is what makes the
dashboard work**: start/stop/restart, the live console, player management, and
file/world/backup operations all execute through the agent.

Requires these Kaggle Secrets: `BACKEND_URL`, `AGENT_ID`, `AGENT_SECRET`.

### Step 11: Run Cell 9 — Start the Server

Starts Paper through the same agent executor, so there is exactly one owner of
the server process and the console channel. It waits for Paper's own readiness
line (`Done (…)!`) before reporting that the server is up, and starts the
periodic Drive backup thread.

### Step 12: Run Cell 10 — Playit.gg Tunnel

Kaggle has no public IP, so friends connect through Playit.gg.

1. Sign up at https://playit.gg
2. Copy your account token from the dashboard
3. Add `PLAYIT_TOKEN` to Kaggle Secrets
4. Run Cell 10 — it starts the connector and prints the public address

⚠️ The tunnel's **local target must be `127.0.0.1:25565`** for the tunnel you
claim in the Playit dashboard. The agent does not — and cannot — set that
mapping for you, so check it once in the dashboard.

Playit hands out either a `*.playit.gg` name or one of its short names such as
`name.<region>.at.ply.gg`; both are recognized. The address the connector was
**actually given** is written to `/kaggle/working/playit/public_address.json` as
it appears, and the dashboard's **Playit.gg Tunnel** card reads it back through
the agent — so the panel shows the real join address, or "address unknown",
never a guessed one.

⚠️ Keep **"Show IPs instead of domains"** OFF in your Playit account settings.
With it on, Playit reports a bare IP that the panel deliberately does not trust
as a join address, and the card stays "address unknown".

### Step 13: Run Cell 11 — Connection Info & Health Check

Reports the configuration actually in effect (`server-port`, `level-name`,
players, view distance) and then the **real** status of the server and the
tunnel, separately:

- Paper: `STOPPED`, or a PID with `readiness` of `loading` / `ready` / `failed`
- Playit: connector running or not, plus the address it was actually given

A JVM that is alive but still loading is reported as `loading`, not "ready".

### Step 14: Run Cell 12 — Backup Status & Manual Operations

Shows backup history and provides manual backup/restore commands.

### Step 15: Run Cell 13 — Logs, Troubleshooting, Shutdown

Final cell with troubleshooting and the shutdown procedure.

### Step 16: Run Cell 14 — Start the Web Panel (and its Cloudflare tunnel)

Runs the dashboard/API inside the Kaggle session, then publishes it through
the existing Cloudflare tunnel. This is a **different tunnel from the
Minecraft tunnel** in Cell 10.

**The connector has to run next to the panel.** The panel listens on
`127.0.0.1:8080` *inside* the kernel, so nothing outside can reach it; Cell 14
starts `cloudflared` next to the panel, using the token-connector form
(`cloudflared tunnel run` with `TUNNEL_TOKEN` in the environment — never on the
command line, where `ps` would show it; it is also redacted from the connector
log). The connector gets the token as both `TUNNEL_TOKEN` (the name
`cloudflared` reads) and `CLOUDFLARE_TUNNEL_TOKEN` (this project's secret name),
so either name authenticates it; without the token `cloudflared tunnel run`
refuses the command line with *"requires the ID or name of the tunnel"*.

| What | Value |
|------|-------|
| Kaggle Secret | `CLOUDFLARE_TUNNEL_TOKEN` (required for the tunnel) |
| Optional secrets | `CLOUDFLARE_TUNNEL_ID`, `CLOUDFLARE_PANEL_HOSTNAME` (labels only) |
| Tunnel | `Rahee-craft-panel` (`96a8853c-45cf-4b9a-a92e-7b412262d8a6`) |
| Public hostname | `panel.soldiers1234583.dpdns.org` |
| Local service | `http://localhost:8080` |

The token is the only thing that authenticates a connector. **The tunnel id
alone is not a credential** — without the token, Cell 14 prints exactly that
and starts no connector.

Get the token from the Cloudflare dashboard: **Zero Trust → Networks →
Tunnels → the tunnel → *Install connector***. Copy it into Kaggle **Add-ons →
Secrets** as `CLOUDFLARE_TUNNEL_TOKEN` and **attach it to this notebook**; then
re-run Cell 14. Never paste the token into chat, source, or a notebook cell.

> ⚠️ Secrets are only readable by a notebook you open in the editor. A run
> produced by `kaggle kernels push` has no integrations attached, so
> `UserSecretsClient().get_secret(...)` fails with *"Connection error trying to
> communicate with service"* (verified live). Run the tunnel cells from the
> editor, not headlessly.

One-time setup in Cloudflare: the tunnel needs a **Public Hostname**
`panel.soldiers1234583.dpdns.org` → service `http://localhost:8080`, and the
zone needs a **proxied DNS record** pointing at the tunnel. Either way works:

1. **Dashboard** — Zero Trust → Networks → Tunnels → the tunnel → *Public
   Hostname*; Cloudflare writes the ingress rule and the DNS record for you.
2. **API** — for an account-owned token (`cfat_…`):
   `PUT /accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations` with
   `{"config": {"ingress": [{"hostname": "panel.…", "service":
   "http://localhost:8080"}, {"service": "http_status:404"}]}}`, then
   `POST /zones/{zone_id}/dns_records` with `{"type": "CNAME", "name":
   "panel", "content": "{tunnel_id}.cfargotunnel.com", "proxied": true}`.

Whichever route you take, the connector only serves the hostname once the rule
exists. `cloudflare_tunnel.py` itself still creates and changes **no** DNS: the
API calls above are a one-time operator step, not something the notebook or the
panel does at runtime.

**The same connector can run on your own machine** instead of in the kernel,
which is how the panel is published from this phone. `run_panel_tunnel.py`
starts the identical connector (token from `CLOUDFLARE_TUNNEL_TOKEN` or
`~/.cloudflare-tunnel-token`, never on the command line) for a panel listening
on `127.0.0.1:8080`, and keeps it up until Ctrl-C:

```bash
python3 run_backend.py          # the panel
python3 run_panel_tunnel.py     # the public hostname for it
python3 run_panel_tunnel.py --check   # report config, start nothing
```

On Android/Termux one extra detail applies: `/etc` is a read-only symlink to
`/system/etc` with no `resolv.conf`, so `cloudflared` (a Go binary) falls back
to `127.0.0.1:53` and every DNS lookup fails with *connection refused*. The
runner detects that and launches the connector through `proot` with a real
`resolv.conf` mounted at `/system/etc/resolv.conf` (no root needed), by setting
`CLOUDFLARED_LAUNCHER`. On a normal Linux host the runner sets nothing and
`cloudflared` runs directly.

Finally, keep `/api/agent/*` closed to the public: the panel and the agent
surface share one hostname, and `/api/agent/register` accepts self-registration
by design. Add a **Cloudflare Access** policy for `panel.<domain>/api/agent/*`
(or keep the panel tunnel separate from anything that serves the agent API).

Panel output goes to `logs/panel.log` inside the session rather than an unread
pipe, so a long-lived panel cannot fill a pipe buffer and freeze.

### Step 17: Run Cell 15 — Runtime Supervisor (leave it running)

**This is the step that keeps RaheeCraft alive.** Cell 15 blocks on purpose.
While it is executing, the kernel stays alive, so the control agent and Paper
keep running. It is the *last* cell you run and you leave it running.

It does not sleep in a tight loop: it wakes every `HEALTH_INTERVAL_SECONDS`
(10s by default), prints one honest health line every `HEALTH_PRINT_SECONDS`
(60s), and returns as soon as the server's desired state becomes `STOPPED`.

```
[supervisor] state=RUNNING desired=RUNNING pid=4211 uptime=812s restarts=0 playit=running players=2
```

Press the notebook's **Stop** button (or interrupt the cell) and the supervisor
cell ends but the **server keeps running** — it tells you so explicitly. Only
**Stop** in the dashboard ends Minecraft, which is also what returns this cell.

---

## 🧭 Runbook: Start, Verify, Stop

### Start RaheeCraft (correct order)

Run the notebook cells top to bottom and leave Cell 15 running:

1. Cell 1 — environment check (Java must be 21+)
2. Cell 2 — configuration, Cell 3–4 — Google Drive auth (optional)
3. Cell 5 — world restore (never overwrites an existing world)
4. Cell 6 — Paper JAR download + verification
5. Cell 7 — `eula.txt`, `server.properties`
6. Cell 8 — control agent
7. Cell 9 — start Paper (waits for readiness)
8. Cell 10 — Playit tunnel (local target `127.0.0.1:25565`)
9. Cell 11 — connection info + health check
10. Cell 14 — web panel, then **Cell 15 — runtime supervisor (leave running)**

Alternatively, press **Start server** in the dashboard at any time. Starting is
idempotent: a second Start while one is already running returns
`already_running` and spawns nothing.

### Confirm it is genuinely ready

`RUNNING` means Paper itself printed its `Done (…)` readiness line — not merely
that a `java` process exists. Three independent confirmations:

- Cell 11 prints `readiness : ready`
- The dashboard badge reads **RUNNING**, not STARTING
- `tail -f /kaggle/working/minecraft_server/logs/latest.log` shows `Done (`

`STARTING` means Java is up and Paper is still loading (world generation can
take minutes). `CRASHED` means it is **not** running — read the error shown in
the dashboard.

### Find the Playit Java address

Cell 10 prints the public endpoint once the connector has one
(`name.playit.gg:NNNNN`). The dashboard's monitoring panel reports the same
thing separately as “tunnel connected”, and Cell 11 re-checks it. If no address
is printed, the tunnel has not been granted one — the server is still local-only,
and no address should be invented or shared.

### Use Stop safely

Press **Stop server** in the dashboard. That single action:

1. records `desired_state = STOPPED` **before** anything is signalled, so the
   supervisor can never restart it behind your back;
2. sends Minecraft's own `stop` command and waits (up to `STOP_TIMEOUT_SECONDS`,
   180s) for the world to be saved and the process to exit;
3. escalates to `SIGTERM`, then `SIGKILL` only if the first two fail — and
   reports which method was actually used;
4. stops the Playit tunnel and the other server-scoped helpers;
5. returns Cell 15 with the real final state.

The dashboard shows the backend's actual state, so it will not display
`STOPPED` while Minecraft is still alive. If shutdown genuinely fails you get
an error message, not a false “stopped”.

**Stop during startup works too.** World generation can take minutes; a Stop
clicked while the server is still `STARTING` cancels the start (the start
reports `cancelled`), brings the JVM down, and does not wait for the start's
timeout. If a genuinely stuck operation holds the lifecycle gate, Stop waits a
bounded time (`STOP_TIMEOUT_SECONDS`) and then reports honestly that it could
not be applied — it never claims a server is stopped while it is not.

Start, Stop and Restart are also safe against rapid repeated clicks: a second
Start while one is in flight returns `already_running` and spawns nothing, and
the agent executes queued actions one at a time.

### How crash recovery works

Only *unexpected* exits of a server you asked to run are recovered:

- the exit code and a log tail are recorded and shown in the dashboard;
- an **unrecoverable** startup failure (bad jar, Java too old, EULA refused) is
  reported immediately and is **never** retried in a loop;
- otherwise the server is restarted with exponential backoff
  (`RESTART_BACKOFF_SECONDS`, doubling to `RESTART_BACKOFF_CAP_SECONDS`),
  up to `MAX_RESTART_ATTEMPTS` (3 by default);
- after that it stays `CRASHED` with a reason a human can act on;
- the attempt budget resets only after `RESTART_RESET_AFTER_SECONDS` (300s) of
  continuous healthy uptime, so a crash-loop cannot restart forever;
- a crash-restart leaves the Playit tunnel alone (Paper returns on the same
  port), while an intentional Stop stops it.

Related knobs: `START_TIMEOUT_SECONDS`, `START_SETTLE_SECONDS`,
`STOP_TIMEOUT_SECONDS`, `HEALTH_INTERVAL_SECONDS`, `CONSOLE_LOG_MAX_BYTES`.

### What happens when Kaggle terminates the runtime

Kaggle ends sessions on its own schedule (idle timeout, ~9h limit, quota, or
“Stop session”). **The Minecraft server cannot outlive its Kaggle runtime** —
there is no way around that, and this setup does not pretend otherwise.

A Kaggle runtime termination is **not** the same as a Minecraft crash:

- Kaggle termination: the kernel and every process in it are gone. Nothing can
  recover, and `desired_state` never becomes `STOPPED`, so the dashboard simply
  stops receiving heartbeats and the agent shows offline.
- Minecraft crash: the Kaggle kernel is still alive, the lifecycle manager
  notices the exit, reports it, and applies the bounded restart policy above.

Best effort before the runtime disappears: an `atexit` hook asks Minecraft to
`save-all` and `stop`, and the periodic Drive backup keeps snapshots. Neither is
subject to the `desired_state` policy, so Start/Stop behaviour is unaffected.

### Protect the world (and the Drive backups)

Nothing in this setup ever deletes, resets or replaces an existing world on its
own:

- Cell 5 restores from Drive **only** when there is no world on disk, and always
  takes a safety copy first. An existing world is preserved unless you flip
  `RESTORE_FROM_DRIVE = True`.
- `world_delete` (panel/file operations) requires confirmation and takes a
  pre-delete backup.
- A start is **refused** while a restore is in flight
  (`world/.rahee-restore-in-progress`), so a half-extracted world can never be
  opened.
- A start is also **refused if a world is expected but `world/level.dat` is
  missing** — Paper would generate an empty world and the periodic Drive backup
  would then upload that over the real one. The error names the cause and tells
  you to restore from Drive. If you *deliberately* want a brand-new world, set
  `ALLOW_NEW_WORLD=1`; a genuinely fresh session (nothing on disk at all) is
  allowed through without that flag.

Before ending a session, run a manual Drive backup (Cell 12): Kaggle can drop
the runtime at any moment and the ~30-minute automatic backup is a best effort,
not a guarantee.

### Credentials that must be configured securely

| Name | Where | Purpose |
|------|-------|---------|
| `ADMIN_PASSWORD` | backend env / Kaggle Secrets | Dashboard login |
| `SECRET_KEY` | backend env | Session signing — set it, don't let it regenerate |
| `AGENT_ID`, `AGENT_SECRET` | Kaggle Secrets + backend registry | Agent authentication |
| `BACKEND_URL` | Kaggle Secrets | Where the agent polls |
| `PLAYIT_TOKEN` | Kaggle Secrets | Playit.gg tunnel |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | Kaggle Secrets | Drive backup |

Never commit these, never print them, and never put them in notebook outputs.
Cells print only whether a value is present, never its contents.

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

- All world folders: `world`, `world_nether`, `world_the_end`. The main world
  carries the per-player state, so a restored world puts players back exactly
  where they left: `world/playerdata/<uuid>.dat` (inventory, position, XP,
  health, ender chest), `world/stats/`, `world/advancements/` and
  `world/level.dat` (seed/spawn).
- Player/service state kept in the server **root**, outside the worlds:
  `ops.json`, `whitelist.json`, `banned-players.json`, `banned-ips.json` and
  `usercache.json`. These make the server *remember* who is an operator and who
  is banned across sessions; without them a player keeps their items but loses
  op, and a banned player is unbanned, on every new session.
- Excludes logs, caches, temp files
- Creates timestamped ZIP archives
- Stored in Drive folder: **Minecraft-Server-Backups**
- Keeps last 5 backups (configurable via `MAX_BACKUPS_TO_KEEP`)

**Not included, on purpose:** `plugins/` and `server.properties`. The notebook
re-writes `server.properties` (Cell 7) and reinstalls plugins each session, so a
restored copy would just be overwritten; plugin *data* (e.g. a LuckPerms
permission database) is therefore not persisted — keep such state inside the
world or avoid plugins on the free tier.

**Timing:** backups run every `BACKUP_INTERVAL_MINUTES` (30 by default) and
only when something actually changed. A session that dies abruptly loses
whatever was played since the last backup, so run a manual backup (Cell 12)
before you stop for the day.

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
| **Java not found** | `!apt-get update && apt-get install -y openjdk-21-jdk-headless` (Kaggle already has 21) |
| **"Java 17 is too old" from the server** | 1.21.x Paper needs Java 21+ — install `openjdk-21-jdk-headless`, then start again |
| **Server won't start** | Check EULA is accepted (`eula=true` in eula.txt) |
| **Out of memory** | Reduce `JAVA_XMX` to 1024 or 512 |
| **Can't connect from outside** | Verify Playit.gg tunnel is running, check the address |
| **Panel says "Connected, address unknown"** | The connector has not been given an address yet, or **"Show IPs instead of domains"** is ON. Check `tail /kaggle/working/playit/playit.log`; the address is cached in `/kaggle/working/playit/public_address.json` |
| **Lag with multiple players** | Reduce `VIEW_DISTANCE` to 4, reduce player count |
| **World not saving** | Always run `stop` command in-game, don't just kill process |
| **Players spawn with nothing / lose items next session** | The world holds player data (`world/playerdata/`); it is uploaded on each backup. If the session died before the next `BACKUP_INTERVAL_MINUTES` upload, recent progress is gone — back up manually (Cell 12) before ending a session |
| **Ops / whitelist / bans reset every session** | Fixed: these live in the server root and are now included in the backup. If they still reset, the backup predates this fix — take a fresh backup (Cell 12) |

---

## 📝 Notes

- **Session Timeout:** Kaggle stops notebooks after ~9 hours. Set a reminder.
  This is a *runtime* termination, not a Minecraft crash, and it cannot be
  recovered from — see “What happens when Kaggle terminates the runtime”.
- **World Loss:** Always backup before closing. Kaggle storage is temporary.
- **Address Changes:** Playit.gg URL changes every session. Share new one each time.
- **Player Limit:** Start small (2-3 players) and add more if performance is good.
- **Performance:** Paper server is already optimized. Additional plugins not recommended on free tier.
- **Drive Backup Reliability:** Kaggle may terminate sessions without warning. Periodic backups help but are not guaranteed. Always do manual backup before closing.
- **OAuth Limitation:** Interactive OAuth requires a browser. First auth must be done in an interactive Kaggle session with browser access.

---

## ✅ Checklist

### Setup
- [ ] Uploaded the repo (including `agent/`, `backend/`, `drive_backup.py`,
      `playit_setup.py`) so the notebook can import them
- [ ] Ran Cell 1 (environment check — Java 21)
- [ ] Edited Cell 2 settings (players, view distance)
- [ ] Set EULA_ACCEPTED = True after reading the EULA
- [ ] Set ENABLE_DRIVE_BACKUP = True (optional)

### Google Drive Setup
- [ ] Created Google Cloud project
- [ ] Enabled Google Drive API
- [ ] Created OAuth 2.0 credentials (Desktop app)
- [ ] Added GOOGLE_CLIENT_ID to Kaggle Secrets
- [ ] Added GOOGLE_CLIENT_SECRET to Kaggle Secrets
- [ ] Ran Cell 3 (authentication)
- [ ] Verified Drive connection (Cell 4)

### Server Setup (in order)
- [ ] Ran Cell 5 (world restoration — existing world is never overwritten)
- [ ] Ran Cell 6 (Paper JAR downloaded and **verified**)
- [ ] Ran Cell 7 (server configuration)
- [ ] Ran Cell 8 (control agent running)
- [ ] Ran Cell 9 (server reports READY, not just "started")
- [ ] Checked `logs/latest.log` for `Done (…)`

### Network & Backup
- [ ] Ran Cell 10 (Playit.gg connector) and noted the printed public address
- [ ] Confirmed the Playit dashboard tunnel targets `127.0.0.1:25565`
- [ ] Ran Cell 11 and saw `readiness : ready` for Paper
- [ ] Shared the Playit address with players
- [ ] Verified automatic Drive backups working
- [ ] Created a manual backup before closing

### Keep-alive
- [ ] Ran Cell 15 (runtime supervisor) **last** and left it running
- [ ] Saw honest health lines every 60s (`state=` / `pid=` / `uptime=`)
- [ ] Confirmed Start again is idempotent (no duplicate Java process)

### Shutdown
- [ ] Pressed **Stop server** in the dashboard (Paper's own `stop`, saves world)
- [ ] Confirmed the result method (`console stop` / `SIGTERM`) rather than assuming
- [ ] Confirmed Cell 15 returned and Paper is `STOPPED` in `logs/latest.log`
- [ ] Ran a final Drive backup (Cell 12)
- [ ] Stopped the notebook kernel

### If it breaks
- [ ] `CRASHED` — read the error in the dashboard; it is *not* running
- [ ] Restart budget exhausted — fix the cause, then press Start (once)
- [ ] Nothing responds — the Kaggle runtime itself is gone; the server went with it

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
