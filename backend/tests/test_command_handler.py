#!/usr/bin/env python3
"""
RaheeCraft Backend - Command Handler Tests
==========================================
Tests for the command handler module (Stage 2 power controls).
"""

import sys
import os
import unittest
from unittest.mock import patch, MagicMock

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.command_handler import (
    CommandHandler, ActionResult, ActionState
)


class TestCommandHandler(unittest.TestCase):
    """Tests for command handler."""
    
    def setUp(self):
        """Set up test fixtures."""
        self.handler = CommandHandler()
    
    def test_create_action(self):
        """Should create a new action."""
        action = self.handler._create_action("test_action")
        self.assertIsNotNone(action)
        self.assertEqual(action["action_type"], "test_action")
        self.assertEqual(action["state"], "pending")
        self.assertIn("action_id", action)
        self.assertIn("created_at", action)
    
    def test_get_action(self):
        """Should get action by ID."""
        action = self.handler._create_action("test_action")
        action_id = action["action_id"]
        
        retrieved = self.handler.get_action(action_id)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved["action_type"], "test_action")
    
    def test_get_action_not_found(self):
        """Should return None for unknown action."""
        result = self.handler.get_action("unknown-action-id")
        self.assertIsNone(result)
    
    def test_list_actions(self):
        """Should list actions in reverse chronological order."""
        self.handler._create_action("action_1")
        self.handler._create_action("action_2")
        self.handler._create_action("action_3")
        
        actions = self.handler.list_actions(2)
        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0]["action_type"], "action_3")
        self.assertEqual(actions[1]["action_type"], "action_2")
    
    def test_cancel_pending_action(self):
        """Should cancel a pending action."""
        action = self.handler._create_action("test_action")
        action_id = action["action_id"]
        
        result = self.handler.cancel_action(action_id)
        self.assertTrue(result)
        
        retrieved = self.handler.get_action(action_id)
        self.assertEqual(retrieved["state"], "cancelled")
    
    def test_cancel_running_action(self):
        """Should cancel a running action."""
        action = self.handler._create_action("test_action")
        action_id = action["action_id"]
        action["state"] = "running"
        
        result = self.handler.cancel_action(action_id)
        self.assertTrue(result)
        
        retrieved = self.handler.get_action(action_id)
        self.assertEqual(retrieved["state"], "cancelled")
    
    def test_cancel_completed_action(self):
        """Should not cancel completed action."""
        action = self.handler._create_action("test_action")
        action_id = action["action_id"]
        action["state"] = "success"
        
        result = self.handler.cancel_action(action_id)
        self.assertFalse(result)
    
    def test_cancel_unknown_action(self):
        """Should return False for unknown action."""
        result = self.handler.cancel_action("unknown-action-id")
        self.assertFalse(result)
    
    def test_allowed_commands(self):
        """Should have expected allowed commands."""
        self.assertIn("list", CommandHandler.ALLOWED_COMMANDS)
        self.assertIn("say", CommandHandler.ALLOWED_COMMANDS)
        self.assertIn("save-all", CommandHandler.ALLOWED_COMMANDS)
        self.assertIn("stop", CommandHandler.ALLOWED_COMMANDS)
        self.assertNotIn("rm", CommandHandler.ALLOWED_COMMANDS)
        self.assertNotIn("rm -rf", CommandHandler.ALLOWED_COMMANDS)
        self.assertNotIn("eval", CommandHandler.ALLOWED_COMMANDS)
    
    def test_check_paper_running_not_running(self):
        """Should return False when Paper not running."""
        running, pid = self.handler._check_paper_running()
        self.assertFalse(running)
        self.assertIsNone(pid)
    
    def test_start_server_jar_not_found(self):
        """Should fail when paper.jar not found."""
        action = self.handler.start_server()
        self.assertEqual(action["state"], "failed")
        self.assertIn("not found", action.get("error", "").lower())
    
    def test_stop_server_not_running(self):
        """Should succeed when server not running."""
        with patch.object(self.handler, "_check_paper_running", return_value=(False, None)):
            action = self.handler.stop_server()
            self.assertEqual(action["state"], "success")
            self.assertTrue(action["result"].get("already_stopped"))
    
    def test_send_console_command_not_allowed(self):
        """Should reject disallowed command."""
        action = self.handler.send_console_command("rm -rf /")
        self.assertEqual(action["state"], "failed")
        self.assertIn("not allowed", action.get("error", ""))
    
    def test_send_console_command_empty(self):
        """Should reject empty command."""
        action = self.handler.send_console_command("")
        self.assertEqual(action["state"], "failed")
        self.assertIn("not allowed", action.get("error", ""))
    
    def test_send_console_command_server_not_running(self):
        """Should reject command when server not running."""
        with patch.object(self.handler, "_check_paper_running", return_value=(False, None)):
            action = self.handler.send_console_command("list")
            self.assertEqual(action["state"], "failed")
            self.assertIn("not running", action.get("error", ""))
    
    def test_send_console_command_allowed(self):
        """Should accept allowed command when server running."""
        with patch.object(self.handler, "_check_paper_running", return_value=(True, 12345)):
            with patch("builtins.open", create=True) as mock_open:
                mock_file = MagicMock()
                mock_file.__enter__ = MagicMock(return_value=mock_file)
                mock_file.__exit__ = MagicMock(return_value=False)
                mock_open.return_value = mock_file
                
                action = self.handler.send_console_command("list")
                self.assertEqual(action["state"], "success")
    
    def test_action_result_format(self):
        """ActionResult should have correct format."""
        action = ActionResult("test-id", "test_action")
        d = action.to_dict()
        
        self.assertEqual(d["action_id"], "test-id")
        self.assertEqual(d["action_type"], "test_action")
        self.assertEqual(d["state"], "pending")
        self.assertIn("created_at", d)
        self.assertIn("started_at", d)
        self.assertIn("completed_at", d)
        self.assertIn("error", d)
        self.assertIn("result", d)


if __name__ == "__main__":
    unittest.main(verbosity=2)
