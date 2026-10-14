# RaheeCraft Control Panel - Deployment Guide

## Overview

This guide covers deploying the RaheeCraft control panel backend to **Render** and connecting the Kaggle-side agent.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    RENDER (Backend)                         │
│  https://your-app.onrender.com                              │
│  - Bottle web app (HTTPS)                                   │
│  - Agent registration endpoint                              │
│  - Agent heartbeat endpoint                                 │
│  - Dashboard frontend                                       │
└─────────────────────────────────────────────────────────────┘
                           ⬆
                           │ HTTPS (agent initiates)
                           │
┌─────────────────────────────────────────────────────────────┐
│                 KAGGLE NOTEBOOK (Agent)                     │
│  - Run agent/main.py cell                                   │
│  - Sends heartbeats every 30s                               │
│  - Reports Paper/Playit/Drive status                        │
└─────────────────────────────────────────────────────────────┘
```

## Step 1: Deploy to Render

### 1.1 Create a New Web Service

1. Go to https://dashboard.render.com
2. Click **New +** → **Web Service**
3. Connect your GitHub account
4. Select `soldiers1234583/raheecraft-control-panel`
5. Configure:

| Setting | Value |
|---------|-------|
| **Name** | `raheecraft-control-panel` |
| **Region** | Your closest region |
| **Branch** | `main` |
| **Root Directory** | `backend` |
| **Runtime** | `Python 3` |
| **Build Command** | `pip install -r requirements.txt` |
| **Start Command** | `python app.py` |
| **Instance Type** | `Free` |

### 1.2 Set Environment Variables

In the Render dashboard for your service, go to **Environment** and add:

| Key | Value | Notes |
|-----|-------|-------|
| `HOST` | `0.0.0.0` | Already in render.yaml |
| `PORT` | `8080` | Already in render.yaml |
| `DEBUG` | `false` | Already in render.yaml |
| `SESSION_SECURE` | `true` | Already in render.yaml |
| `SECRET_KEY` | `(generate)` | Generate with: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `ADMIN_PASSWORD` | `(choose)` | Your admin login password |
| `ADMIN_PASSWORD_HASH` | `(leave empty)` | Auto-generated from ADMIN_PASSWORD on first run |

**Click Save Changes**

### 1.3 Wait for Deployment

- Render will build and deploy your service
- This takes 2-5 minutes on first deploy
- You'll see a live URL like: `https://raheecraft-control-panel.onrender.com`

## Step 2: Get Agent Credentials

After deployment completes:

### 2.1 Test the Backend

```bash
curl -sI https://your-app-name.onrender.com/health
```

Expected: `HTTP/2 200`

### 2.2 Register the Agent

Create a file `register_agent.sh`:

```bash
#!/bin/bash
export BACKEND_URL="https://your-app-name.onrender.com"

cd ~/rahee-world
python agent/register_agent.py
```

Run it:
```bash
chmod +x register_agent.sh
./register_agent.sh
```

This will output:
```
✅ Agent registered successfully!

📋 COPY THESE CREDENTIALS TO YOUR KAGGLE NOTEBOOK:
   os.environ['AGENT_ID'] = 'agent-xxxxxxxx'
   os.environ['AGENT_SECRET'] = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'

   Backend URL: https://your-app-name.onrender.com
```

**Copy these credentials to a secure location** (not in chat, not in git).

## Step 3: Configure Kaggle Notebook

In your Kaggle notebook, add a new cell AFTER your existing setup cells:

```python
# ============================================================
# RAHEECRAFT AGENT CONFIGURATION
# ============================================================

import os

# Replace with your actual deployed URL
os.environ['BACKEND_URL'] = 'https://your-app-name.onrender.com'

# Replace with credentials from Step 2
os.environ['AGENT_ID'] = 'agent-your-agent-id-here'
os.environ['AGENT_SECRET'] = 'your-agent-secret-here'

# Server directory (already set in your notebook)
os.environ['SERVER_DIR'] = '/kaggle/working/minecraft_server'
```

Then add another cell to run the agent:

```python
# Run the RaheeCraft agent
import sys
sys.path.insert(0, '/kaggle/working/raheecraft/agent')
from agent.main import run_agent

print("Starting RaheeCraft agent...")
run_agent()
```

## Step 4: Verify Connection

### 4.1 Check Backend Health

```bash
curl -s https://your-app-name.onrender.com/health
```

Expected response:
```json
{"status": "healthy", "timestamp": 1234567890, "server_dir_exists": false}
```

### 4.2 Check Agent Status

Visit your backend dashboard: `https://your-app-name.onrender.com/`

Login with your admin credentials, then check:
- Agent status should show "Connected"
- Paper status should show actual state
- Last heartbeat timestamp should be recent

### 4.3 Check Logs

In Render dashboard → your service → **Logs**:
- Look for "Agent registered" messages
- Look for "Heartbeat from" messages
- Check for any errors

## Environment Variables Reference

### Backend (Render Environment)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `HOST` | No | `0.0.0.0` | Bind address |
| `PORT` | No | `8080` | Listen port |
| `DEBUG` | No | `false` | Debug mode |
| `SECRET_KEY` | Yes | Auto-generate | Session signing key |
| `ADMIN_PASSWORD` | Yes | - | Admin login password |
| `ADMIN_PASSWORD_HASH` | No | Auto-generate | Pre-computed hash |
| `SESSION_SECURE` | No | `true` | Secure cookies |
| `BACKEND_URL` | No | - | Self-reference URL |
| `SERVER_DIR` | No | `/kaggle/working/minecraft_server` | Server path |

### Agent (Kaggle Notebook Cell)

| Variable | Required | Description |
|----------|----------|-------------|
| `BACKEND_URL` | Yes | Your deployed backend URL |
| `AGENT_ID` | Yes | From registration |
| `AGENT_SECRET` | Yes | From registration |
| `SERVER_DIR` | No | Server directory path |
| `POLL_INTERVAL_SECONDS` | No | 30 (default) |
| `CONNECT_TIMEOUT` | No | 10 (default) |
| `MAX_RETRIES` | No | 3 (default) |

## Connection Verification Checklist

- [ ] Backend deployed to Render
- [ ] Environment variables configured in Render
- [ ] `curl https://your-app.onrender.com/health` returns 200
- [ ] Dashboard loads in browser
- [ ] Admin password works for login
- [ ] Agent credentials generated via `register_agent.py`
- [ ] Kaggle notebook cell configured with credentials
- [ ] Agent cell runs without errors
- [ ] Agent appears as "Connected" in dashboard
- [ ] Heartbeats appear in backend logs
- [ ] Server status shows actual Paper state

## Troubleshooting

### Backend won't start

- Check Render logs for errors
- Verify `requirements.txt` has `bottle` and `httpx`
- Check `app.py` has correct WSGI entry point

### Agent can't connect

- Verify `BACKEND_URL` is correct (include https://)
- Check agent credentials are correct
- Check Render service is healthy
- Check network connectivity from Kaggle to Render

### Agent shows as offline

- Check agent is actually running in notebook
- Check heartbeat interval (default 30s)
- Check backend logs for heartbeat errors
- Verify agent credentials haven't expired

### Dashboard shows no data

- Verify agent is connected
- Check server directory path is correct
- Verify Paper server is running on Kaggle

## Security Notes

- **Never commit** `.env` files or credentials to git
- **Keep `ADMIN_PASSWORD` secret** - use a strong password
- **Keep `SECRET_KEY` secret** - regenerate if compromised
- **Keep `AGENT_SECRET` secret** - treat like a password
- **Use HTTPS** - Render provides this automatically
- **Set `SESSION_SECURE=true`** in production

## Optional: Custom Domain

To use a custom domain instead of `.onrender.com`:

1. In Render dashboard → your service → **Settings**
2. Scroll to **Custom Domain**
3. Add your domain (e.g., `panel.rahee.dev`)
4. Configure DNS as instructed by Render
5. Update `BACKEND_URL` in Kaggle notebook

## Cost

- **Render Free Tier**: $0/month
  - 750 hours/month (enough for 24/7 if it's the only service)
  - Sleeps after 15 min of inactivity (Spin Up in ~30s)
  - 1GB RAM, 0.1 CPU
  
For always-on, need Paid plan ($7/month starter).

## Next Steps (Stage 2)

After Stage 1 is deployed and verified:

1. Add server control commands (start/stop/restart)
2. Implement file manager
3. Add player management with RCON
4. Add Geyser/Floodgate status
5. Add Google Drive backup controls
6. Add real-time updates via Server-Sent Events
