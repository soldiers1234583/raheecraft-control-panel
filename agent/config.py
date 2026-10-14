"""
RaheeCraft Control Agent Configuration
======================================
Loads configuration from environment variables with sensible defaults.
Never hardcodes secrets.
"""

import os
import secrets
from pathlib import Path
from typing import Optional


class Config:
    """Agent configuration loaded from environment."""
    
    # Agent identity
    AGENT_ID: str = os.environ.get("AGENT_ID", "")
    AGENT_SECRET: str = os.environ.get("AGENT_SECRET", "")
    
    # Backend connection
    BACKEND_URL: str = os.environ.get("BACKEND_URL", "https://your-backend.github.io")
    BACKEND_API_PATH: str = os.environ.get("BACKEND_API_PATH", "/api/agent")
    
    # Connection settings
    POLL_INTERVAL_SECONDS: int = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
    CONNECT_TIMEOUT: int = int(os.environ.get("CONNECT_TIMEOUT", "10"))
    MAX_RETRIES: int = int(os.environ.get("MAX_RETRIES", "3"))
    RETRY_BASE_DELAY: float = float(os.environ.get("RETRY_BASE_DELAY", "1.0"))
    
    # Server paths (Kaggle-specific)
    SERVER_DIR: str = os.environ.get("SERVER_DIR", "/kaggle/working/minecraft_server")
    WORLD_DIR: str = os.environ.get("WORLD_DIR", "")
    LOG_FILE: str = os.environ.get("LOG_FILE", "")
    
    # Feature flags
    ENABLE_PLAYIT: bool = os.environ.get("ENABLE_PLAYIT", "true").lower() == "true"
    ENABLE_DRIVE_BACKUP: bool = os.environ.get("ENABLE_DRIVE_BACKUP", "true").lower() == "true"
    
    @classmethod
    def validate(cls) -> list[str]:
        """Validate configuration, return list of errors."""
        errors = []
        
        if not cls.AGENT_ID:
            errors.append("AGENT_ID not set")
        if not cls.AGENT_SECRET:
            errors.append("AGENT_SECRET not set")
        if not cls.BACKEND_URL:
            errors.append("BACKEND_URL not set")
            
        return errors
    
    @classmethod
    def generate_agent_credentials(cls) -> tuple[str, str]:
        """Generate a new agent ID and secret."""
        agent_id = f"agent-{secrets.token_hex(8)}"
        agent_secret = secrets.token_hex(32)
        return agent_id, agent_secret
    
    @classmethod
    def get_log_path(cls) -> Path:
        """Get the server log file path."""
        if cls.LOG_FILE:
            return Path(cls.LOG_FILE)
        # Default: Paper logs/latest.log
        return Path(cls.SERVER_DIR) / "logs" / "latest.log"
    
    @classmethod
    def get_world_path(cls) -> Path:
        """Get the world directory path."""
        if cls.WORLD_DIR:
            return Path(cls.WORLD_DIR)
        return Path(cls.SERVER_DIR) / "world"
    
    @classmethod
    def redact_secret(cls, value: str) -> str:
        """Redact a secret for logging."""
        if not value:
            return "***"
        if len(value) <= 8:
            return "***"
        return value[:4] + "***" + value[-4:]


# Singleton instance
config = Config()
