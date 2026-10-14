#!/usr/bin/env python3
"""
RaheeCraft Control Panel - Backend
===================================
Bottle web application for controlling Kaggle Minecraft server.

Usage:
    export BACKEND_URL=https://your-backend-url
    export AGENT_SECRET=your-secret
    python backend/app.py
"""

import os
import sys
import json
import time
import logging
from pathlib import Path

from bottle import Bottle, request, response, template, redirect, abort
from bottle import SimpleTemplate, TEMPLATE_PATH

# Ensure templates resolve regardless of the working directory (Render runs
# from backend/, local runs often from the project root).
_BACKEND_DIR = Path(__file__).parent
if str(_BACKEND_DIR / "templates") not in TEMPLATE_PATH:
    TEMPLATE_PATH.insert(0, str(_BACKEND_DIR / "templates"))

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import config
from backend.auth import (
    verify_user_token, create_user_token, generate_session_token,
    hash_secret, verify_secret, redact_for_log
)
from backend.agent_handler import agent_handler
from backend.server_status import server_status

# Configure logging
logging.basicConfig(
    level=logging.INFO if not config.DEBUG else logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# Create Bottle app
app = Bottle()


# =============================================================================
# Middleware
# =============================================================================

@app.hook('before_request')
def before_request():
    """Set response headers."""
    response.content_type = 'application/json'
    response.headers['X-Content-Type-Options'] = 'nosniff'


@app.hook('after_request')
def after_request():
    """Add security headers."""
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Cache-Control'] = 'no-store'


def json_error_handler(res):
    """Return abort() payloads as JSON instead of bottle's HTML error page.

    Views call abort(code, {"error": ...}) — the dashboard parses that body.
    """
    response.content_type = 'application/json'
    body = res.body
    if isinstance(body, (dict, list)):
        return json.dumps(body)
    if isinstance(body, bytes):
        body = body.decode(errors='replace')
    try:
        parsed = json.loads(str(body))
        if isinstance(parsed, (dict, list)):
            return json.dumps(parsed)
    except (ValueError, TypeError):
        pass
    return json.dumps({"error": str(body), "code": res.status_code})


app.default_error_handler = json_error_handler


# =============================================================================
# Authentication Decorators
# =============================================================================

def require_user(f):
    """Decorator to require authenticated user."""
    def wrapper(*args, **kwargs):
        session_id = request.get_cookie(config.SESSION_COOKIE_NAME)
        if not session_id:
            abort(401, {"error": "Authentication required"})
        
        session = config.validate_session(session_id)
        if not session:
            abort(401, {"error": "Invalid or expired session"})
        
        request.user_id = session["user_id"]
        request.session_id = session_id
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper


def require_admin(f):
    """Decorator to require admin user."""
    def wrapper(*args, **kwargs):
        session_id = request.get_cookie(config.SESSION_COOKIE_NAME)
        if not session_id:
            abort(401, {"error": "Authentication required"})
        
        session = config.validate_session(session_id)
        if not session:
            abort(401, {"error": "Invalid or expired session"})
        
        if session.get("user_id") != "admin":
            abort(403, {"error": "Admin access required"})
        
        request.user_id = session["user_id"]
        request.session_id = session_id
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper


def require_agent(f):
    """Decorator to require authenticated agent."""
    def wrapper(*args, **kwargs):
        agent_id = request.headers.get(config.AGENT_ID_HEADER)
        agent_secret = request.headers.get(config.AGENT_TOKEN_HEADER)
        
        if not agent_id or not agent_secret:
            abort(401, {"error": "Agent authentication required"})
        
        if not agent_handler.verify_agent(agent_id, agent_secret):
            logger.warning(f"Failed agent auth attempt: {agent_id[:8]}...")
            abort(401, {"error": "Invalid agent credentials"})
        
        request.agent_id = agent_id
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper


# =============================================================================
# Rate Limiting (simple in-memory)
# =============================================================================

class RateLimiter:
    """Simple in-memory rate limiter."""
    
    def __init__(self):
        self.requests = {}  # key -> [timestamps]
    
    def is_allowed(self, key: str, max_requests: int, window: int) -> bool:
        """Check if request is allowed."""
        now = time.time()
        if key not in self.requests:
            self.requests[key] = []
        
        # Remove old entries
        self.requests[key] = [t for t in self.requests[key] if now - t < window]
        
        if len(self.requests[key]) >= max_requests:
            return False
        
        self.requests[key].append(now)
        return True


rate_limiter = RateLimiter()


def rate_limit(max_requests=None, window=None):
    """Rate limit decorator.

    Buckets are namespaced per endpoint + client: without this, a burst of
    requests to any rate-limited route would consume the stricter power
    action budget (5/min) and cause cross-endpoint 429s.
    """
    def decorator(f):
        def wrapper(*args, **kwargs):
            key = f"{f.__name__}:{request.remote_addr or 'unknown'}"
            mr = max_requests or config.RATE_LIMIT_REQUESTS
            wn = window or config.RATE_LIMIT_WINDOW_SECONDS
            
            if not rate_limiter.is_allowed(key, mr, wn):
                abort(429, {"error": "Rate limit exceeded"})
            
            return f(*args, **kwargs)
        wrapper.__name__ = f.__name__
        return wrapper
    return decorator


# =============================================================================
# Routes - Public
# =============================================================================

@app.route('/health')
def health():
    """Health check endpoint."""
    return {
        "status": "healthy",
        "timestamp": time.time(),
        "server_dir_exists": server_status.server_exists()
    }


@app.route('/api/agent/register', method='POST')
@rate_limit(max_requests=10, window=60)
def agent_register():
    """
    Agent registration endpoint.
    
    Agent must provide:
    - X-Agent-ID header
    - X-Agent-Token header (secret)
    - JSON body with optional metadata
    """
    agent_id = request.headers.get(config.AGENT_ID_HEADER)
    agent_secret = request.headers.get(config.AGENT_TOKEN_HEADER)
    
    if not agent_id or not agent_secret:
        logger.warning("Registration attempt without credentials")
        abort(400, {"error": "Agent ID and secret required"})
    
    # Validate agent_id format
    if not agent_id.startswith("agent-"):
        abort(400, {"error": "Invalid agent ID format"})
    
    # Check if already registered
    if agent_id in agent_handler.agents:
        logger.info(f"Agent re-registration: {agent_id[:8]}...")
        # Update secret if changed
        agent_handler.register_agent(agent_id, agent_secret)
        return {"status": "re-registered"}
    
    # Register new agent
    success = agent_handler.register_agent(agent_id, agent_secret)
    if success:
        logger.info(f"New agent registered: {agent_id[:8]}...")
        config.log_audit("agent_register", "system", {
            "agent_id": agent_id[:8]
        }, success=True)
        return {"status": "registered"}
    else:
        abort(500, {"error": "Registration failed"})


@app.route('/api/agent/heartbeat', method='POST')
@require_agent
@rate_limit(max_requests=60, window=60)
def agent_heartbeat():
    """
    Agent heartbeat endpoint.
    
    Agent sends its current status.
    """
    try:
        data = request.json or {}
        
        # Build status dict
        status = {
            "paper_running": data.get("paper_running", False),
            "paper_pid": data.get("paper_pid"),
            "playit_running": data.get("playit_running", False),
            "playit_address": data.get("playit_address"),
            "drive_last_backup": data.get("drive_last_backup"),
            "cpu_percent": data.get("cpu_percent"),
            "memory_percent": data.get("memory_percent"),
            "player_count": data.get("player_count"),
            "uptime_seconds": data.get("uptime_seconds", 0),
            "timestamp": time.time()
        }
        
        success = agent_handler.heartbeat(request.agent_id, request.headers.get(config.AGENT_TOKEN_HEADER), status)
        
        if success:
            logger.debug(f"Heartbeat from {request.agent_id[:8]}...")
            return {"status": "ok"}
        else:
            abort(401, {"error": "Authentication failed"})
    
    except Exception as e:
        logger.error(f"Heartbeat error: {e}")
        abort(500, {"error": "Internal error"})


@app.route('/api/auth/login', method='POST')
@rate_limit(max_requests=10, window=60)
def login():
    """User login endpoint."""
    try:
        data = request.json or {}
        username = data.get("username", "")
        password = data.get("password", "")
        
        if not username or not password:
            abort(400, {"error": "Username and password required"})
        
        # Check admin password (Stage 1 simple auth)
        admin_hash = config.ADMIN_PASSWORD_HASH
        if not admin_hash:
            # No admin set up - reject
            logger.warning(f"Login attempt with no admin configured: {username[:8]}...")
            abort(401, {"error": "Authentication not configured"})
        
        if not verify_secret(password, admin_hash):
            logger.warning(f"Failed login attempt: {username[:8]}...")
            config.log_audit("login", username, {"success": False}, success=False)
            abort(401, {"error": "Invalid credentials"})
        
        # Create session
        session_id = config.create_session(username)
        
        # Set cookie
        response.set_cookie(
            config.SESSION_COOKIE_NAME,
            session_id,
            path="/",
            secure=config.SESSION_COOKIE_SECURE,
            httponly=True,
            max_age=3600  # 1 hour
        )
        
        logger.info(f"User logged in: {username}")
        config.log_audit("login", username, {"success": True}, success=True)
        
        return {
            "status": "ok",
            "session_id": session_id,
            "username": username
        }
    
    except Exception as e:
        logger.error(f"Login error: {e}")
        abort(500, {"error": "Internal error"})


@app.route('/api/auth/logout', method='POST')
@require_user
def logout():
    """User logout endpoint."""
    session_id = request.session_id
    if session_id in config.SESSIONS:
        del config.SESSIONS[session_id]
    
    response.delete_cookie(config.SESSION_COOKIE_NAME)
    return {"status": "logged_out"}


# =============================================================================
# Routes - Authenticated Users
# =============================================================================

@app.route('/api/status')
@require_user
def get_status():
    """Get server and agent status."""
    agent_id = request.session_id  # For now, use session as agent reference
    
    # Get agent status (if any agent is registered)
    agents_status = agent_handler.get_all_agents_status()
    
    # Get server status
    server_info = server_status.get_server_summary()
    
    # Determine overall status
    agent_online = any(a["status"] == "online" for a in agents_status)
    
    return {
        "server": {
            "exists": server_info["server_exists"],
            "paper_jar": server_info["paper_jar_exists"],
            "eula_accepted": server_info["eula_accepted"],
            "paper_running": server_info["paper_running"],
            "paper_pid": server_info["paper_pid"],
            "version": server_info["minecraft_version"],
            "paper_version": server_info["paper_version"],
            "world_exists": server_info["world_exists"],
        },
        "agent": {
            "online": agent_online,
            "agents": agents_status
        },
        "playit": server_info.get("playit", {"running": False}),
        "backup": server_info.get("last_backup"),
        "timestamp": server_info["timestamp"]
    }


@app.route('/api/logs')
@require_user
def get_logs():
    """Get recent server log lines."""
    log_path = server_status.get_log_path()
    
    if not log_path.exists():
        return {"lines": [], "error": "No log file found"}
    
    try:
        # Read last 100 lines
        with open(log_path, 'r') as f:
            lines = f.readlines()
        
        recent_lines = [line.rstrip() for line in lines[-100:]]
        
        return {
            "lines": recent_lines,
            "total_lines": len(lines),
            "file": str(log_path)
        }
    except Exception as e:
        logger.error(f"Error reading logs: {e}")
        return {"lines": [], "error": str(e)}


@app.route('/api/playit/status')
@require_user
def get_playit_status():
    """Get Playit tunnel status."""
    status = server_status.get_playit_status()
    return status


@app.route('/api/backup/status')
@require_user
def get_backup_status():
    """Get Google Drive backup status."""
    backup_info = server_status.get_last_backup_info()
    return {
        "last_backup": backup_info,
        "enabled": config.ENABLE_DRIVE_BACKUP
    }


# =============================================================================
# Routes - Admin Only
# =============================================================================

@app.route('/api/admin/agents', method='GET')
@require_admin
def list_agents():
    """List all registered agents (admin only)."""
    agents = agent_handler.get_all_agents_status()
    return {"agents": agents}


@app.route('/api/admin/agents/<agent_id>', method='DELETE')
@require_admin
def delete_agent(agent_id):
    """Delete an agent (admin only)."""
    if agent_id in agent_handler.agents:
        del agent_handler.agents[agent_id]
        config.log_audit("agent_delete", request.user_id, {"agent_id": agent_id[:8]}, success=True)
        return {"status": "deleted"}
    abort(404, {"error": "Agent not found"})


@app.route('/api/admin/audit', method='GET')
@require_admin
def get_audit_log():
    """Get audit log (admin only)."""
    return {
        "entries": config.AUDIT_LOG[-100:],  # Last 100 entries
        "total": len(config.AUDIT_LOG)
    }


# =============================================================================
# Routes - Frontend
# =============================================================================

@app.route('/')
def index():
    """Serve the main dashboard."""
    # Check if user is logged in
    session_id = request.get_cookie(config.SESSION_COOKIE_NAME)
    logged_in = False
    username = None
    
    if session_id:
        session = config.validate_session(session_id)
        if session:
            logged_in = True
            username = session.get("user_id")
    
    # Get status for initial render
    try:
        status = get_status()
    except Exception:
        status = {"server": {}, "agent": {"online": False, "agents": []}}
    
    return template(
        'dashboard.html',
        base_path=os.path.dirname(__file__),
        logged_in=logged_in,
        username=username,
        status=status
    )


@app.route('/login')
def login_page():
    """Serve login page."""
    return template('login.html', base_path=os.path.dirname(__file__))


@app.route('/static/<filename:path>')
def static_files(filename):
    """Serve static files."""
    static_dir = os.path.join(os.path.dirname(__file__), 'static')
    return bottle.static_file(filename, root=static_dir)


# Import bottle here to avoid circular import
import bottle


# =============================================================================
# Main
# =============================================================================

def main():
    """Start the web application."""
    # Ensure backend directory exists for templates
    backend_dir = Path(__file__).parent
    templates_dir = backend_dir / "templates"
    templates_dir.mkdir(parents=True, exist_ok=True)
    
    # Create default admin if not set
    admin_password = os.environ.get("ADMIN_PASSWORD")
    if admin_password and not config.ADMIN_PASSWORD_HASH:
        config.ADMIN_PASSWORD_HASH = hash_secret(admin_password)
        logger.info("Admin password configured")
    
    # Log startup
    logger.info(f"Starting RaheeCraft Control Panel on {config.HOST}:{config.PORT}")
    logger.info(f"Debug mode: {config.DEBUG}")
    logger.info(f"Server dir: {config.SERVER_DIR}")
    
    # Run the app
    app.run(
        host=config.HOST,
        port=config.PORT,
        debug=config.DEBUG,
        reloader=False  # Disable reloader for production
    )

# =============================================================================
# Power Control Endpoints (Stage 2)
# =============================================================================

from backend.command_handler import command_handler, ActionResult, ActionState


@app.route('/api/admin/server/start', method='POST')
@require_admin
@rate_limit(max_requests=5, window=60)
def start_server():
    """Queue a start action for the Kaggle agent.

    Returns state=pending with an action_id — the request being queued is
    NOT reported as a successful start.
    """
    existing = [a for a in command_handler.list_actions(10)
                if a["action_type"] in ("start_server", "restart_server")
                and a["state"] in ("pending", "running")]

    if existing:
        return {
            "status": "busy",
            "message": "Server operation already in progress",
            "action_id": existing[0]["action_id"],
            "state": existing[0]["state"]
        }

    agent = _require_online_agent()
    try:
        action = command_handler.queue_for_agent(
            "start_server", {}, agent_id=agent["agent_id"])
    except RuntimeError as e:
        abort(429, {"error": str(e)})

    config.log_audit("server_start", request.user_id,
                     {"action_id": action["action_id"], "state": action["state"]},
                     success=False)
    return {"status": "queued", "message": "Start queued for agent — poll for result",
            **action}


@app.route('/api/admin/server/stop', method='POST')
@require_admin
@rate_limit(max_requests=5, window=60)
def stop_server():
    """Queue a graceful stop (Minecraft `stop` → world saves) for the agent."""
    existing = [a for a in command_handler.list_actions(10)
                if a["action_type"] in ("stop_server", "restart_server")
                and a["state"] in ("pending", "running")]

    if existing:
        return {
            "status": "busy",
            "message": "Server operation already in progress",
            "action_id": existing[0]["action_id"],
            "state": existing[0]["state"]
        }

    agent = _require_online_agent()
    try:
        action = command_handler.queue_for_agent(
            "stop_server", {}, agent_id=agent["agent_id"])
    except RuntimeError as e:
        abort(429, {"error": str(e)})

    config.log_audit("server_stop", request.user_id,
                     {"action_id": action["action_id"], "state": action["state"]},
                     success=False)
    return {"status": "queued", "message": "Stop queued for agent — poll for result",
            **action}


@app.route('/api/admin/server/restart', method='POST')
@require_admin
@rate_limit(max_requests=3, window=120)
def restart_server():
    """Queue a controlled restart: agent stops, verifies termination, starts."""
    existing = [a for a in command_handler.list_actions(10)
                if a["state"] in ("pending", "running")]

    if existing:
        return {
            "status": "busy",
            "message": "Server operation already in progress",
            "action_id": existing[0]["action_id"],
            "state": existing[0]["state"]
        }

    agent = _require_online_agent()
    try:
        action = command_handler.queue_for_agent(
            "restart_server", {}, agent_id=agent["agent_id"])
    except RuntimeError as e:
        abort(429, {"error": str(e)})

    config.log_audit("server_restart", request.user_id,
                     {"action_id": action["action_id"], "state": action["state"]},
                     success=False)
    return {"status": "queued", "message": "Restart queued for agent — poll for result",
            **action}


@app.route('/api/admin/server/actions', method='GET')
@require_admin
def list_server_actions():
    """List recent server actions."""
    limit = request.query.limit or 50
    try:
        limit = int(limit)
    except (ValueError, TypeError):
        limit = 50
    
    return {"actions": command_handler.list_actions(limit)}


@app.route('/api/admin/server/actions/<action_id>', method='GET')
@require_admin
def get_server_action(action_id):
    """Get a specific action."""
    action = command_handler.get_action(action_id)
    if not action:
        abort(404, {"error": "Action not found"})
    return action


@app.route('/api/admin/server/actions/<action_id>', method='DELETE')
@require_admin
def cancel_server_action(action_id):
    """Cancel a pending action."""
    if command_handler.cancel_action(action_id):
        config.log_audit("action_cancel", request.user_id,
                         {"action_id": action_id}, success=True)
        return {"status": "cancelled"}
    abort(404, {"error": "Action not found or cannot be cancelled"})


@app.route('/api/admin/server/console', method='POST')
@require_admin
@rate_limit(max_requests=30, window=60)
def send_console_command():
    """Send a console command to the server."""
    data = request.json or {}
    command = data.get("command", "").strip()
    
    if not command:
        abort(400, {"error": "Command required"})
    
    if len(command) > 500:
        abort(400, {"error": "Command too long (max 500 chars)"})

    # Strict allowlist validation happens backend-side (defense in depth);
    # the agent re-validates before writing to the Paper console channel.
    base_cmd = command.split()[0].lower() if command.split() else ""
    if base_cmd not in command_handler.ALLOWED_COMMANDS:
        abort(400, {"error": f"Command '{base_cmd}' not allowed"})

    agent = _require_online_agent()
    try:
        action = command_handler.queue_for_agent(
            "console_command", {"command": command},
            agent_id=agent["agent_id"])
    except RuntimeError as e:
        abort(429, {"error": str(e)})

    config.log_audit("console_command", request.user_id,
                     {"action_id": action["action_id"], "command": command[:50]},
                     success=False)
    return {"status": "queued", "message": "Command queued for agent",
            **action}


@app.route('/api/admin/console/logs', method='GET')
@require_user
def get_recent_logs():
    """Get recent log lines with optional filtering."""
    log_path = server_status.get_log_path()
    
    if not log_path.exists():
        return {"lines": [], "total": 0}
    
    try:
        lines = log_path.read_text().splitlines()
        
        filter_type = request.query.filter or ""
        search = request.query.search or ""
        
        filtered = lines
        
        if filter_type:
            filtered = [l for l in filtered if filter_type.lower() in l.lower()]
        
        if search:
            filtered = [l for l in filtered if search.lower() in l.lower()]
        
        max_lines = min(int(request.query.limit or 200), 500)
        recent = filtered[-max_lines:]
        
        return {
            "lines": recent,
            "total": len(lines),
            "filtered": len(filtered),
            "filter": filter_type,
            "search": search
        }
    except Exception as e:
        logger.error(f"Error reading logs: {e}")
        return {"lines": [], "total": 0, "error": str(e)}


# =============================================================================
# Stage 2 — Agent Action Relay & Log Streaming
# =============================================================================
# Paper runs on Kaggle. The browser queues an action here; the agent polls
# it via POST /api/agent/poll (outbound HTTPS only), executes locally, and
# reports back via POST /api/agent/actions/<id>/result. Log lines stream
# with each heartbeat and land in the in-memory log buffer.

from backend.log_buffer import log_buffer


def _require_online_agent():
    """Abort 503 if no registered agent is currently heartbeating."""
    online = [a for a in agent_handler.get_all_agents_status()
              if agent_handler.is_agent_online(a["agent_id"])]
    if not online:
        abort(503, {"error": "No agent online — start the Kaggle notebook agent",
                    "state": "agent_offline"})
    return online[0]


@app.route('/api/agent/poll', method='POST')
@require_agent
@rate_limit(max_requests=60, window=60)
def agent_poll():
    """Agent pulls pending power/console actions (with timeout sweep)."""
    command_handler.sweep_timeouts()
    actions = command_handler.pop_for_agent(request.agent_id, limit=3)
    return {"actions": actions, "server_time": time.time()}


@app.route('/api/agent/actions/<action_id>', method='POST')
@require_agent
@rate_limit(max_requests=60, window=60)
def agent_action_result(action_id):
    """Agent reports the terminal state of an action (idempotent)."""
    data = request.json or {}
    state = data.get("state")
    if state not in ("success", "failed", "timeout", "cancelled"):
        abort(400, {"error": "state must be success|failed|timeout|cancelled"})

    # Only the agent the action was addressed to may complete it
    action = command_handler.get_action(action_id)
    if not action:
        abort(404, {"error": "Action not found"})
    if action.get("agent_id") and action["agent_id"] != request.agent_id:
        abort(403, {"error": "Action belongs to another agent"})

    try:
        result = command_handler.complete_from_agent(
            action_id, state,
            error=data.get("error"),
            result=data.get("result")
        )
    except (ValueError, KeyError) as e:
        abort(400, {"error": str(e)})

    config.log_audit("agent_action_result", request.agent_id,
                     {"action_id": action_id, "state": state},
                     success=(state == "success"))
    # Stage 3: close the loop on player-management audit entries so the
    # history shows the real outcome, not just "queued".
    if str(action.get("action_type", "")).endswith(
            ("_player", "whitelist_add", "whitelist_remove",
             "whitelist_enable", "whitelist_disable",
             "op_grant", "op_revoke")):
        config.log_audit(
            f"outcome_{action['action_type']}", request.agent_id,
            {"action_id": action_id,
             "target": (action.get("params") or {}).get("username"),
             "state": state,
             "error": data.get("error") or None},
            success=(state == "success"))
    return result


@app.route('/api/agent/logs', method='POST')
@require_agent
@rate_limit(max_requests=60, window=60)
def agent_log_push():
    """Agent streams new console log lines (redacted before storage)."""
    data = request.json or {}
    lines = data.get("lines", [])
    if not isinstance(lines, list):
        abort(400, {"error": "lines must be a list"})
    if len(lines) > 500:
        abort(400, {"error": "Too many lines (max 500 per push)"})

    accepted = log_buffer.extend(lines, source="agent")
    return {"accepted": accepted}


@app.route('/api/admin/console/tail', method='GET')
@require_user
def console_tail():
    """Tail the streamed console log with reconnect-safe sequence numbers.

    Client passes ?since=<last_seq> to fetch only new lines.
    """
    try:
        since = int(request.query.since or 0)
    except (ValueError, TypeError):
        since = 0
    limit = min(int(request.query.limit or 200), 500) \
        if str(request.query.limit or "").isdigit() else 200

    return log_buffer.tail(
        since=since,
        limit=limit,
        level=request.query.level or None,
        search=request.query.search or None
    )


@app.route('/api/admin/monitoring', method='GET')
@require_user
def get_monitoring():
    """Real measurements for dashboard monitoring cards.

    Values that cannot be measured are returned as null with a reason
    rather than invented.
    """
    command_handler.sweep_timeouts()

    agents = agent_handler.get_all_agents_status()
    online_agents = [a for a in agents
                     if agent_handler.is_agent_online(a["agent_id"])]
    agent_status = online_agents[0] if online_agents else (
        agents[0] if agents else None)
    # Never let a missing agent crash the endpoint — measurements degrade
    # to null with a note instead.
    agent = agent_status or {}

    # Server process state comes from the agent heartbeat (Kaggle side).
    # "measured" means the data is FRESH (agent online) — a stale heartbeat
    # is reported with its age instead of being presented as current.
    data_fresh = bool(online_agents)
    paper_running = bool(agent and agent.get("paper_status") == "running")
    last_hb = agent.get("last_heartbeat")
    hb_age = (time.time() - last_hb) if last_hb else None
    if data_fresh:
        process_note = None
    elif agent_status:
        process_note = (f"stale heartbeat ({int(hb_age)}s ago)"
                        if hb_age is not None else "stale heartbeat")
    else:
        process_note = "no agent heartbeat data"

    # Paper memory/CPU: agent reports system-wide metrics; per-process
    # Java memory is not measurable remotely → null with reason.
    mem_note = None
    java_memory_mb = None
    if agent and agent.get("memory_percent") is not None:
        mem_note = "system memory used (host-wide, not per-JVM)"

    # Log activity
    tail = log_buffer.tail(since=0, limit=1)
    last_log = tail["lines"][-1] if tail["lines"] else None

    # In-flight actions
    inflight = [a for a in command_handler.list_actions(20)
                if a["state"] in ("pending", "running")]

    return {
        "process": {
            "state": "online" if paper_running else "offline",
            "pid": agent.get("paper_pid") if agent else None,
            "measured": data_fresh,
            "note": process_note,
        },
        "uptime_seconds": (agent.get("uptime_seconds") if agent else None)
                          if paper_running else 0,
        "players": {
            "online": agent.get("player_count") if agent and paper_running else None,
            "max": _max_players(),
            "note": None if paper_running else "server not running",
        },
        "memory": {
            "system_percent": agent.get("memory_percent"),
            "memory_percent": agent.get("memory_percent") if agent else None,
            "java_rss_mb": java_memory_mb,
            "note": (mem_note + " — " + process_note) if (mem_note and not data_fresh)
                    else (mem_note or (process_note if not data_fresh else None)),
        },
        "cpu": {
            "percent": agent.get("cpu_percent") if agent else None,
            "note": None if data_fresh else process_note,
        },
        "agent": {
            "online": bool(online_agents),
            "last_heartbeat": last_hb,
            "heartbeat_age_seconds": round(hb_age, 1) if hb_age is not None else None,
            "agent_id": agent_status["agent_id"] if agent_status else None,
        },
        "log_activity": {
            "last_line": last_log["text"] if last_log else None,
            "last_line_ts": last_log["ts"] if last_log else None,
            "last_agent_push": log_buffer.last_agent_push,
            "buffered": len(log_buffer.tail(since=0, limit=500)["lines"]),
        },
        "playit": (agent.get("playit_status") == "running",
                   agent.get("playit_address")),
        "playit_detail": {
            "running": bool(agent.get("playit_status") == "running"),
            "address": agent.get("playit_address") if agent else None,
        },
        "backup": {
            "last": agent.get("drive_last_backup"),
            "source": "agent-reported",
        },
        "inflight_actions": inflight,
        "timestamp": time.time(),
    }


def _max_players():
    """Read max Players from server.properties if present (local dev only)."""
    try:
        props = server_status.server_properties()
        if props and "max-players" in props:
            return int(props["max-players"])
    except Exception:
        pass
    return None


if __name__ == "__main__":
    main()


# =============================================================================
# Stage 3 — Players, Moderation, Whitelist, Operators
# =============================================================================
# All player state comes from the agent's heartbeat snapshot, which the agent
# builds by reading Paper's own files (usercache.json, whitelist.json,
# banned-players.json, ops.json) and log lines. Nothing is fabricated; a
# missing snapshot renders as "unavailable", not as empty success.
#
# Mutations are relayed like power actions: validated here, re-validated by
# the agent, confirmed against Paper's files, audited with actor + target.

from backend.validation import (
    valid_username, sanitize_reason, validate_player_request
)

# Actor attribution: sessions carry the username that authenticated (Stage 1
# single-admin auth). Deeper per-user attribution is not possible until the
# auth system grows real accounts — see README limitations.

STAGE3_MUTATIONS = {
    "kick": ("kick_player", 10, 60),
    "ban": ("ban_player", 10, 60),
    "unban": ("unban_player", 10, 60),
    "whitelist_add": ("whitelist_add", 10, 60),
    "whitelist_remove": ("whitelist_remove", 10, 60),
    "whitelist_enable": ("whitelist_enable", 5, 60),
    "whitelist_disable": ("whitelist_disable", 5, 60),
    "op_grant": ("op_grant", 5, 60),
    "op_revoke": ("op_revoke", 5, 60),
}


def _stage3_mutation(kind, require_reason=False, require_admin=True,
                     step_up=False):
    """Build a validated, audited, relay-queued mutation endpoint body."""
    action_type, max_req, window = STAGE3_MUTATIONS[kind]

    data = request.json or {}
    username, reason, err = validate_player_request(
        data, require_reason=require_reason)
    if err:
        abort(400, {"error": err})

    # Step-up auth: privilege changes require re-entering the admin
    # password in the request body (defense against session hijack/CSRF
    # on the most dangerous operations).
    if step_up:
        confirm = data.get("confirm_password")
        if not confirm or not verify_secret(
                confirm, config.ADMIN_PASSWORD_HASH or ""):
            config.log_audit(f"player_{kind}", request.user_id,
                             {"target": username, "reason": reason},
                             success=False)
            abort(403, {"error": "Privilege changes require admin password "
                                 "confirmation"})

    if require_admin:
        _require_online_agent()
    params = {"username": username}
    if reason:
        params["reason"] = reason

    # Conflict guard: same action already in flight for same target
    for a in command_handler.list_actions(20):
        if (a["action_type"] == action_type
                and a["state"] in ("pending", "running")
                and a.get("params", {}).get("username") == username):
            return {"status": "busy", "message": "Action already in progress",
                    "action_id": a["action_id"], "state": a["state"]}

    try:
        action = command_handler.queue_for_agent(action_type, params)
    except RuntimeError as e:
        abort(429, {"error": str(e)})

    config.log_audit(f"player_{kind}", request.user_id,
                     {"target": username, "reason": reason or None,
                      "action_id": action["action_id"]},
                     success=False)  # outcome recorded when agent reports
    return {"status": "queued", "message": f"{kind} queued for agent",
            "action_id": action["action_id"], "state": action["state"],
            "target": username}


@app.route('/api/admin/players', method='GET')
@require_user
def players_list():
    """Online players and full snapshot from the agent heartbeat."""
    agents = agent_handler.get_all_agents_status()
    online_agents = [a for a in agents
                     if agent_handler.is_agent_online(a["agent_id"])]
    agent = online_agents[0] if online_agents else None

    snap = (agent or {}).get("player_snapshot")
    snap_ts = (agent or {}).get("player_snapshot_ts")

    if agent is None:
        return {"available": False, "reason": "agent_offline",
                "online": [], "count": 0}
    if snap is None:
        return {"available": False, "reason": "no_snapshot_yet",
                "online": [], "count": 0,
                "note": "Agent has not reported player data yet — "
                        "wait for the next heartbeat."}

    return {
        "available": True,
        "online": snap.get("online", []),
        "count": len(snap.get("online", [])),
        "snapshot_ts": snap_ts,
        "agent_online": True,
    }


@app.route('/api/admin/players/bans', method='GET')
@require_user
def players_bans():
    """Ban list from banned-players.json via the agent."""
    agents = agent_handler.get_all_agents_status()
    agent = next((a for a in agents
                  if agent_handler.is_agent_online(a["agent_id"])), None)
    snap = (agent or {}).get("player_snapshot")
    if snap is None:
        return {"available": False,
                "reason": "agent_offline" if agent is None
                          else "no_snapshot_yet",
                "bans": []}
    return {"available": True, "bans": snap.get("bans", []),
            "snapshot_ts": snap.get("ts")}


@app.route('/api/admin/whitelist', method='GET')
@require_user
def whitelist_get():
    """Whitelist status + entries from whitelist.json via the agent."""
    agents = agent_handler.get_all_agents_status()
    agent = next((a for a in agents
                  if agent_handler.is_agent_online(a["agent_id"])), None)
    snap = (agent or {}).get("player_snapshot")
    if snap is None:
        return {"available": False,
                "reason": "agent_offline" if agent is None
                          else "no_snapshot_yet",
                "enabled": None, "entries": []}
    wl = snap.get("whitelist", {})
    return {"available": True, "enabled": wl.get("enabled"),
            "entries": wl.get("entries", []),
            "snapshot_ts": snap.get("ts")}


@app.route('/api/admin/operators', method='GET')
@require_user
def operators_get():
    """Operator list from ops.json + LuckPerms detection."""
    agents = agent_handler.get_all_agents_status()
    agent = next((a for a in agents
                  if agent_handler.is_agent_online(a["agent_id"])), None)
    snap = (agent or {}).get("player_snapshot")
    if snap is None:
        return {"available": False,
                "reason": "agent_offline" if agent is None
                          else "no_snapshot_yet",
                "operators": [],
                "luckperms": {"installed": False, "operational": False}}
    return {"available": True, "operators": snap.get("ops", []),
            "luckperms": snap.get("luckperms",
                                  {"installed": False, "operational": False}),
            "snapshot_ts": snap.get("ts")}


@app.route('/api/admin/players/history', method='GET')
@require_user
def players_history():
    """RaheeCraft-recorded moderation history (from the audit log).

    This is OUR history of dashboard actions, not the server's own —
    presented as such. Search: ?username= filters by target.
    """
    username = request.query.username or None
    if username and not valid_username(username):
        abort(400, {"error": "Invalid username"})
    entries = [
        e for e in config.AUDIT_LOG
        if str(e.get("action", "")).startswith("player_")
    ]
    if username:
        entries = [
            e for e in entries
            if (e.get("details") or {}).get("target") == username
        ]
    return {"entries": entries[-200:], "source": "raheecraft_audit"}


# --- Mutations (each queues a validated relay action) -----------------------

@app.route('/api/admin/players/kick', method='POST')
@require_admin
@rate_limit(max_requests=10, window=60)
def players_kick():
    """Kick an online player with optional reason."""
    return _stage3_mutation("kick")


@app.route('/api/admin/players/ban', method='POST')
@require_admin
@rate_limit(max_requests=10, window=60)
def players_ban():
    """Ban a player (online or offline-known) with optional reason."""
    return _stage3_mutation("ban")


@app.route('/api/admin/players/unban', method='POST')
@require_admin
@rate_limit(max_requests=10, window=60)
def players_unban():
    """Remove a player from the ban list."""
    return _stage3_mutation("unban")


@app.route('/api/admin/whitelist/add', method='POST')
@require_admin
@rate_limit(max_requests=10, window=60)
def whitelist_add():
    """Add a player to the whitelist by username."""
    return _stage3_mutation("whitelist_add")


@app.route('/api/admin/whitelist/remove', method='POST')
@require_admin
@rate_limit(max_requests=10, window=60)
def whitelist_remove():
    """Remove a player from the whitelist."""
    return _stage3_mutation("whitelist_remove")


@app.route('/api/admin/whitelist/enable', method='POST')
@require_admin
@rate_limit(max_requests=5, window=60)
def whitelist_enable():
    """Enable the whitelist (destructive to joiners — audited)."""
    return _stage3_mutation("whitelist_enable", require_admin=True)


@app.route('/api/admin/whitelist/disable', method='POST')
@require_admin
@rate_limit(max_requests=5, window=60)
def whitelist_disable():
    """Disable the whitelist."""
    return _stage3_mutation("whitelist_disable", require_admin=True)


@app.route('/api/admin/operators', method='POST')
@require_admin
@rate_limit(max_requests=5, window=60)
def operators_grant():
    """Grant operator — requires admin password re-confirmation (step-up).

    LuckPerms group management is NOT implemented here: if LuckPerms is
    not detected as operational, the UI must show the unavailable state
    rather than pretend a change happened.
    """
    return _stage3_mutation("op_grant", step_up=True)


@app.route('/api/admin/operators/<username>', method='DELETE')
@require_admin
@rate_limit(max_requests=5, window=60)
def operators_revoke(username):
    """Revoke operator — requires admin password re-confirmation (step-up)."""
    if not valid_username(username):
        abort(400, {"error": "Invalid username"})
    confirm = (request.json or {}).get("confirm_password")
    if not confirm or not verify_secret(confirm,
                                        config.ADMIN_PASSWORD_HASH or ""):
        config.log_audit("player_op_revoke", request.user_id,
                         {"target": username}, success=False)
        abort(403, {"error": "Privilege changes require admin password "
                             "confirmation"})
    _require_online_agent()
    try:
        action = command_handler.queue_for_agent(
            "op_revoke", {"username": username})
    except RuntimeError as e:
        abort(429, {"error": str(e)})
    config.log_audit("player_op_revoke", request.user_id,
                     {"target": username,
                      "action_id": action["action_id"]}, success=False)
    return {"status": "queued", "action_id": action["action_id"],
            "state": action["state"], "target": username}
