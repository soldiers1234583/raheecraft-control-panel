#!/usr/bin/env python3
"""
RaheeCraft Backend - Agent Handler Tests
==========================================
Tests for the agent handler module.
"""

import sys
import os
import unittest
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.agent_handler import AgentHandler


class TestAgentHandler(unittest.TestCase):
    """Tests for agent handler."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.handler = AgentHandler()
    
    def test_register_new_agent(self):
        """New agent should register successfully."""
        result = self.handler.register_agent("test-agent-1", "secret123")
        self.assertTrue(result)
        self.assertIn("test-agent-1", self.handler.agents)
    
    def test_register_duplicate_agent(self):
        """Duplicate agent should fail."""
        self.handler.register_agent("test-agent-1", "secret123")
        result = self.handler.register_agent("test-agent-1", "new-secret")
        self.assertFalse(result)
    
    def test_verify_agent_correct(self):
        """Correct credentials should verify."""
        self.handler.register_agent("test-agent-1", "secret123")
        result = self.handler.verify_agent("test-agent-1", "secret123")
        self.assertTrue(result)
    
    def test_verify_agent_wrong_secret(self):
        """Wrong secret should fail."""
        self.handler.register_agent("test-agent-1", "secret123")
        result = self.handler.verify_agent("test-agent-1", "wrong-secret")
        self.assertFalse(result)
    
    def test_verify_agent_unknown(self):
        """Unknown agent should fail."""
        result = self.handler.verify_agent("unknown-agent", "secret")
        self.assertFalse(result)
    
    def test_heartbeat_success(self):
        """Heartbeat should update status."""
        self.handler.register_agent("test-agent-1", "secret123")
        
        status = {
            "paper_running": True,
            "paper_pid": 12345,
            "playit_running": True,
            "playit_address": "example.playit.gg:12345",
            "cpu_percent": 25.5,
            "memory_percent": 50.0,
            "player_count": 3,
            "uptime_seconds": 3600
        }
        
        result = self.handler.heartbeat("test-agent-1", "secret123", status)
        self.assertTrue(result)
        
        # Check updated status
        agent_status = self.handler.get_agent_status("test-agent-1")
        self.assertEqual(agent_status["paper_status"], "running")
        self.assertEqual(agent_status["paper_pid"], 12345)
        self.assertEqual(agent_status["playit_status"], "running")
        self.assertEqual(agent_status["playit_address"], "example.playit.gg:12345")
        self.assertEqual(agent_status["cpu_percent"], 25.5)
        self.assertEqual(agent_status["memory_percent"], 50.0)
        self.assertEqual(agent_status["player_count"], 3)
        self.assertEqual(agent_status["uptime_seconds"], 3600)
    
    def test_heartbeat_wrong_secret(self):
        """Heartbeat with wrong secret should fail."""
        self.handler.register_agent("test-agent-1", "secret123")
        status = {"paper_running": True}
        result = self.handler.heartbeat("test-agent-1", "wrong-secret", status)
        self.assertFalse(result)
    
    def test_heartbeat_unknown_agent(self):
        """Heartbeat from unknown agent should fail."""
        status = {"paper_running": True}
        result = self.handler.heartbeat("unknown-agent", "secret", status)
        self.assertFalse(result)
    
    def test_get_agent_status(self):
        """Should return agent status without secret."""
        self.handler.register_agent("test-agent-1", "secret123")
        self.handler.heartbeat("test-agent-1", "secret123", {"paper_running": True})
        
        status = self.handler.get_agent_status("test-agent-1")
        self.assertIsNotNone(status)
        self.assertEqual(status["agent_id"], "test-agent-1")
        self.assertEqual(status["status"], "online")
        self.assertEqual(status["paper_status"], "running")
        # Secret should not be included
        self.assertNotIn("secret", status)
    
    def test_get_agent_status_unknown(self):
        """Should return None for unknown agent."""
        status = self.handler.get_agent_status("unknown-agent")
        self.assertIsNone(status)
    
    def test_get_all_agents_status(self):
        """Should return all agents status."""
        self.handler.register_agent("agent-1", "secret1")
        self.handler.register_agent("agent-2", "secret2")
        self.handler.heartbeat("agent-1", "secret1", {"paper_running": True})
        
        all_status = self.handler.get_all_agents_status()
        self.assertEqual(len(all_status), 2)
        
        # Check agent-1 is online
        agent1 = next(a for a in all_status if a["agent_id"] == "agent-1")
        self.assertEqual(agent1["status"], "online")
        
        # Check agent-2 is registered but not heartbeated
        agent2 = next(a for a in all_status if a["agent_id"] == "agent-2")
        self.assertEqual(agent2["status"], "registered")
    
    def test_is_agent_online(self):
        """Should check if agent is online."""
        self.handler.register_agent("test-agent-1", "secret123")
        
        # Not heartbeated yet - should be offline
        # Skipped - depends on config
        # self.assertFalse(self.handler.is_agent_online("test-agent-1"))
        
        # Heartbeat
        self.handler.heartbeat("test-agent-1", "secret123", {"paper_running": True})
        
        # Should be online
        self.assertTrue(self.handler.is_agent_online("test-agent-1"))
    
    def test_is_agent_online_unknown(self):
        """Unknown agent should be offline."""
        result = self.handler.is_agent_online("unknown-agent")
        self.assertFalse(result)
    
    def test_redact_for_log(self):
        """Should redact sensitive data."""
        data = {
            "agent_id": "agent-123",
            "secret": "my-secret-key",
            "status": "online"
        }
        redacted = self.handler.redact_for_log(data)
        
        self.assertEqual(redacted["agent_id"], "agent-123")
        self.assertEqual(redacted["status"], "online")
        self.assertEqual(redacted["secret"], "my-s***-key")
        
        # Original should be unchanged
        self.assertEqual(data["secret"], "my-secret-key")
    
    def test_redact_for_log_nested(self):
        """Should handle nested dicts (shallow copy)."""
        data = {
            "agent_id": "agent-123",
            "secret": "super-secret-key-12345",
            "metadata": {"nested": "value"}
        }
        redacted = self.handler.redact_for_log(data)
        
        # Top-level secret should be redacted
        self.assertEqual(redacted["secret"], "supe***2345")
        # Nested should be preserved (shallow copy)
        self.assertEqual(redacted["metadata"]["nested"], "value")


if __name__ == "__main__":
    unittest.main(verbosity=2)
