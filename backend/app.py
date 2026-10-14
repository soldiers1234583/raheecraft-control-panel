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
from bottle import SimpleTemplate

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
    """Rate limit decorator."""
    def decorator(f):
        def wrapper(*args, **kwargs):
            key = request.remote_addr or "unknown"
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


if __name__ == "__main__":
    main()
