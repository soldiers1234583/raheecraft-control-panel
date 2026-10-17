"""agent.auth — thin bridge to backend.auth for the agent package.

The agent package was written expecting `agent.auth` to exist (register_agent.py
imports `agent.auth.generate_agent_token`). The real auth implementation lives in
`backend.auth`. This module re-exports the one function register_agent needs so the
import works without duplicating any crypto code.
"""

from backend.auth import generate_agent_token  # noqa: F401
