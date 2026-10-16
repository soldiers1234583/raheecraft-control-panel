# RaheeCraft Control Panel

Premium Minecraft server management dashboard for your Kaggle-hosted Paper server.

## Overview

RaheeCraft is a secure control plane connecting a web dashboard to your Kaggle-side Minecraft server. It provides:

- **Server Status** - Real-time Paper server state
- **Live Console** - Stream server logs
- **Player Management** - View and manage players
- **File Manager** - Browse and edit server files
- **Google Drive Backups** - Persistent world storage
- **Playit.gg Tunnel** - Public connectivity
- **Authentication** - Secure login for admin access

## Architecture

```
┌─────────────────┐         ┌─────────────────┐
│  Web Dashboard  │         │  Kaggle Notebook │
│  (GitHub Pages) │         │  (Minecraft +    │
│                 │         │   Agent)         │
└────────┬────────┘         └────────┬────────┘
         │                            │
         │  HTTPS Polling / WSS       │
         │  (agent initiates)         │
         └─────────────────────────────┘
```

**Key constraint:** The Kaggle agent must initiate the connection (outbound only).

## Quick Start

### 1. Set up the backend

```bash
cd backend
pip install bottle  # Already installed in Termux

# Configure environment
cp .env.example .env
# Edit .env with your settings

# Start the server
python app.py
```

### 2. Set up the Kaggle agent

In your Kaggle notebook, add a cell to start the agent:

```python
import sys
sys.path.insert(0, '/kaggle/working/raheecraft/agent')

from agent.main import run_agent

# Configure
import os
os.environ['BACKEND_URL'] = 'https://your-backend-url'
os.environ['AGENT_ID'] = 'your-agent-id'
os.environ['AGENT_SECRET'] = 'your-agent-secret'
os.environ['SERVER_DIR'] = '/kaggle/working/minecraft_server'

# Run agent
run_agent()
```

### 3. Access the dashboard

Open `http://localhost:8080` (if running locally) or your deployed backend URL.

## Project Structure

```
raheecraft-control-panel/
├── agent/               # Kaggle-side control agent
│   ├── __init__.py
│   ├── config.py        # Agent configuration
│   ├── main.py          # Agent entry point (to be created)
│   └── status.py        # Status reporting (to be created)
├── backend/             # Web control panel backend
│   ├── __init__.py
│   ├── app.py           # Bottle web application
│   ├── config.py        # Backend configuration
│   ├── auth.py          # Authentication
│   ├── agent_handler.py # Agent lifecycle management
│   ├── server_status.py # Server state reading
│   ├── templates/       # HTML templates
│   │   ├── login.html
│   │   └── dashboard.html
│   ├── static/          # Static assets
│   │   ├── css/
│   │   │   └── main.css
│   │   └── js/
│   │       ├── auth.js
│   │       └── dashboard.js
│   └── tests/           # Unit tests
│       ├── __init__.py
│       ├── test_auth.py
│       └── test_agent_handler.py
├── drive_backup.py      # Google Drive backup (existing)
├── playit_setup.py      # Playit.gg tunnel (existing)
├── geyser_setup.py      # Geyser/Floodgate (existing)
├── kaggle_api_setup.py  # Kaggle API helper (local only; gitignored)
├── run_backend.py       # Panel entry point (loads .env, starts Bottle)
├── cloudflare_tunnel.py # Cloudflare connector: token via env, drained output,
│                        #   reports connected only when Cloudflare says so
├── run_panel_tunnel.py  # Runs that connector next to a local panel
├── minecraft_kaggle_setup.ipynb  # Kaggle notebook (existing)
├── KAGGLE_MINECRAFT_GUIDE.md     # User guide (existing)
├── KAGGLE_API_README.md          # API docs (local only; gitignored)
├── .env.example         # Environment template
├── .gitignore           # Git ignore rules
└── README.md            # This file
```

## Environment Variables

### Backend (.env)

| Variable | Description | Default |
|----------|-------------|---------|
| `HOST` | Server bind address | `0.0.0.0` |
| `PORT` | Server port | `8080` |
| `SECRET_KEY` | Session signing key | Auto-generated |
| `DEBUG` | Debug mode | `false` |
| `ADMIN_PASSWORD` | Admin password | - |
| `ADMIN_PASSWORD_HASH` | Admin password hash | - |
| `SERVER_DIR` | Minecraft server dir | `/kaggle/working/minecraft_server` |

### Agent (Kaggle notebook cell)

| Variable | Description |
|----------|-------------|
| `BACKEND_URL` | Your backend URL (HTTPS) |
| `AGENT_ID` | Agent ID (from backend) |
| `AGENT_SECRET` | Agent secret (from backend) |
| `SERVER_DIR` | Same as backend |

## Public Panel URL (Cloudflare Tunnel)

The panel listens on `127.0.0.1:8080`, so the tunnel connector has to run
*next to it* — in the same Kaggle kernel, or on the same machine as a local
panel. `cloudflare_tunnel.py` is that connector; `run_panel_tunnel.py` is the
local entry point.

```bash
# 1. the panel (reads .env; ADMIN_PASSWORD is required to log in)
python3 run_backend.py

# 2. the connector for it: token from CLOUDFLARE_TUNNEL_TOKEN or
#    ~/.cloudflare-tunnel-token (chmod 600), never on a command line
python3 run_panel_tunnel.py

# report configuration without starting anything
python3 run_panel_tunnel.py --check
```

The tunnel itself is remotely managed, so the public hostname lives in
Cloudflare, not in this repo: a **Public Hostname** rule (`panel.…` →
`http://localhost:8080`) plus a **proxied CNAME** to `<tunnel-id>.cfargotunnel.com`.
Create both in the dashboard, or once through the API for an account-owned
token (`cfat_…`) — see Step 16 of `KAGGLE_MINECRAFT_GUIDE.md`.

On **Android/Termux** the runner adds one thing automatically: `/etc` is a
read-only symlink to `/system/etc` with no `resolv.conf`, so `cloudflared` (Go)
falls back to `127.0.0.1:53` and every lookup fails. The runner detects that and
launches the connector through `proot` with a real `resolv.conf` mounted at
`/system/etc/resolv.conf` (no root), by setting `CLOUDFLARED_LAUNCHER`. On a
normal Linux host it sets nothing.

Exposure: `/health` and the login page are public; every `/api/*` route —
panel and agent alike — returns `401` without a session or agent credential.
Keep the admin password long, and consider a Cloudflare Access policy for
`panel.<domain>/api/agent/*`.

## Deployment

### Backend (GitHub)

The backend can be deployed to:
- **GitHub Pages** (static frontend only, no backend)
- **GitHub Actions** (for containerized deployment)
- **External hosting** (Heroku, Railway, VPS, etc.)

For Stage 1, run locally with `python backend/app.py`.

### Kaggle Agent

Add to your notebook:

```python
# In a Kaggle notebook cell (run after server starts)
import os
os.environ['BACKEND_URL'] = 'https://your-backend-url'
os.environ['AGENT_ID'] = 'from-backend-registration'
os.environ['AGENT_SECRET'] = 'from-backend-registration'
os.environ['SERVER_DIR'] = '/kaggle/working/minecraft_server'

import sys
sys.path.insert(0, '/kaggle/working/raheecraft/agent')
from agent.main import run_agent

run_agent()
```

## Security

- **No hardcoded secrets** - All credentials via environment
- **HMAC-based auth** - No external crypto dependencies
- **Agent credentials** - Random generation, never logged
- **Rate limiting** - Prevents abuse
- **Audit logging** - Tracks all admin actions
- **Secret redaction** - Credentials never appear in logs

## Limitations (Stage 1)

- User auth is simple password (upgrade to proper auth later)
- Agent registry is in-memory (loses state on restart)
- No WebSocket support yet (uses HTTPS polling)
- No actual server control commands (read-only in Stage 1)
- No persistent storage for agents/users

## Next Steps (Stage 2+)

- Persistent agent registry (database)
- WebSocket support for real-time updates
- Server control commands (start/stop/restart)
- File manager implementation
- Player management
- Plugin installation
- World restoration
- User registration with proper auth

## License

Private - All rights reserved.
