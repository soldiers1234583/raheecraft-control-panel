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
├── kaggle_api_setup.py  # Kaggle API helper (existing)
├── minecraft_kaggle_setup.ipynb  # Kaggle notebook (existing)
├── KAGGLE_MINECRAFT_GUIDE.md     # User guide (existing)
├── KAGGLE_API_README.md          # API docs (existing)
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
