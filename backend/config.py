"""
RaheeCraft Control Panel Backend Configuration
===============================================
Loads configuration from environment variables.
Never hardcodes secrets.
"""

import os
import secrets
from typing import Optional


class Config:
    """Backend configuration."""
    
    # Server settings
    HOST: str = os.environ.get("HOST", "0.0.0.0")
    PORT: int = int(os.environ.get("PORT", "8080"))
    DEBUG: bool = os.environ.get("DEBUG", "false").lower() == "true"
    
    # Secret key for sessions (auto-generated if not set)
    SECRET_KEY: str = os.environ.get("SECRET_KEY", secrets.token_hex(32))
    
    # Agent authentication
    AGENT_TOKEN_HEADER: str = "X-Agent-Token"
    AGENT_ID_HEADER: str = "X-Agent-ID"
    
    # Session settings
    SESSION_COOKIE_NAME: str = "raheecraft_session"
    SESSION_COOKIE_SECURE: bool = os.environ.get("SESSION_SECURE", "false").lower() == "true"
    
    # Rate limiting (simple in-memory)
    RATE_LIMIT_REQUESTS: int = int(os.environ.get("RATE_LIMIT_REQUESTS", "100"))
    RATE_LIMIT_WINDOW_SECONDS: int = int(os.environ.get("RATE_LIMIT_WINDOW_SECONDS", "60"))
    
    # Agent registry (in-memory for Stage 1)
    AGENTS: dict = {}
    
    # User sessions (in-memory for Stage 1)
    SESSIONS: dict = {}
    
    # Audit log (in-memory for Stage 1)
    AUDIT_LOG: list = []
    
    @classmethod
    def reset(cls):
        """Reset in-memory stores (for testing)."""
        cls.AGENTS.clear()
        cls.SESSIONS.clear()
        cls.AUDIT_LOG.clear()
    
    @classmethod
    def add_agent(cls, agent_id: str, secret: str, metadata: dict = None):
        """Register an agent."""
        cls.AGENTS[agent_id] = {
            "secret": secret,
            "metadata": metadata or {},
            "last_seen": None,
            "status": "offline"
        }
    
    @classmethod
    def verify_agent(cls, agent_id: str, secret: str) -> bool:
        """Verify agent credentials."""
        agent = cls.AGENTS.get(agent_id)
        if not agent:
            return False
        return agent["secret"] == secret
    
    @classmethod
    def update_agent_status(cls, agent_id: str, status: str, metadata: dict = None):
        """Update agent status."""
        if agent_id in cls.AGENTS:
            cls.AGENTS[agent_id]["last_seen"] = metadata.get("timestamp") if metadata else None
            cls.AGENTS[agent_id]["status"] = status
            if metadata:
                cls.AGENTS[agent_id]["metadata"].update(metadata)
    
    @classmethod
    def create_session(cls, user_id: str, agent_id: str = None) -> str:
        """Create a new session."""
        session_id = secrets.token_hex(16)
        cls.SESSIONS[session_id] = {
            "user_id": user_id,
            "agent_id": agent_id,
            "created_at": __import__('time').time(),
            "last_activity": __import__('time').time()
        }
        return session_id
    
    @classmethod
    def validate_session(cls, session_id: str) -> Optional[dict]:
        """Validate a session."""
        session = cls.SESSIONS.get(session_id)
        if not session:
            return None
        return session
    
    @classmethod
    def log_audit(cls, action: str, user_id: str, details: dict, success: bool = True):
        """Log an audit event."""
        import time
        cls.AUDIT_LOG.append({
            "timestamp": time.time(),
            "action": action,
            "user_id": user_id,
            "details": details,
            "success": success
        })
        # Keep only last 1000 entries
        if len(cls.AUDIT_LOG) > 1000:
            cls.AUDIT_LOG = cls.AUDIT_LOG[-1000:]


config = Config()
