"""
RaheeCraft Authentication
=========================
Authentication for users and agents.
Uses HMAC-based verification (no external crypto dependencies).
"""

import hashlib
import hmac
import re
import time
import secrets
import os
from typing import Optional, Tuple


class AuthError(Exception):
    """Authentication error."""
    pass


def hash_secret(secret: str, salt: str = None) -> str:
    """Hash a secret using HMAC-SHA256."""
    if salt is None:
        salt = secrets.token_hex(16)
    h = hmac.new(salt.encode(), secret.encode(), hashlib.sha256)
    return f"{salt}${h.hexdigest()}"


def verify_secret(secret: str, hashed: str) -> bool:
    """Verify a secret against its hash."""
    try:
        salt, hash_value = hashed.split("$", 1)
        expected = hmac.new(salt.encode(), secret.encode(), hashlib.sha256)
        return hmac.compare_digest(expected.hexdigest(), hash_value)
    except (ValueError, AttributeError):
        return False


def generate_session_token() -> str:
    """Generate a cryptographically secure session token."""
    return secrets.token_urlsafe(32)


def generate_agent_token() -> Tuple[str, str]:
    """Generate an agent ID and secret."""
    agent_id = f"agent-{secrets.token_hex(8)}"
    secret = secrets.token_hex(32)
    return agent_id, secret


def create_user_token(username: str, secret_key: str) -> str:
    """Create a user session token."""
    timestamp = int(time.time())
    data = f"{username}:{timestamp}"
    signature = hmac.new(secret_key.encode(), data.encode(), hashlib.sha256).hexdigest()
    token = f"{data}.{signature}"
    return token


def verify_user_token(token: str, secret_key: str, max_age: int = 3600) -> Optional[str]:
    """Verify a user session token. Returns username if valid."""
    try:
        parts = token.split(".")
        if len(parts) != 2:
            return None
        data, signature = parts
        expected_sig = hmac.new(secret_key.encode(), data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_sig):
            return None
        username, timestamp = data.split(":", 1)
        if int(time.time()) - int(timestamp) > max_age:
            return None
        return username
    except (ValueError, AttributeError):
        return None


def redact_for_log(value: str, show_chars: int = 4) -> str:
    """Redact a value for logging."""
    if not value:
        return "***"
    if len(value) <= show_chars * 2:
        return "***"
    return value[:show_chars] + "***" + value[-show_chars:]


# Patterns that look like secrets in free-form text (log lines, errors)
_SECRET_PATTERNS = [
    (re.compile(r'(?i)\b(password|passwd|secret|token|api[_-]?key|authorization)\b(\s*[=:]\s*)(\S+)'),
     r'\1\2***'),
    (re.compile(r'(?i)\b(kaggle[_-]?key|kaggle[_-]?token)\b(\s*[=:]\s*)(\S+)'),
     r'\1\2***'),
    (re.compile(r'KGAT_[A-Za-z0-9]+'), 'KGAT_***'),
    (re.compile(r'\b[0-9a-f]{32,}\b'), '***'),
    (re.compile(r'(?i)(Bearer\s+)([A-Za-z0-9._\-]+)'), r'\1***'),
]


def redact_text(text: str) -> str:
    """Redact secret-looking patterns in free-form text (log lines).

    Unlike redact_for_log (which truncates a single known secret value),
    this scrubs patterns such as `password=...`, `KGAT_...`, long hex
    strings, and bearer tokens from arbitrary lines.
    """
    if not text:
        return text
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def get_admin_password_hash() -> Optional[str]:
    """Get the admin password hash from environment."""
    return os.environ.get("ADMIN_PASSWORD_HASH")
