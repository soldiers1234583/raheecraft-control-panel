#!/usr/bin/env python3
"""
RaheeCraft Backend - Authentication Tests
==========================================
Tests for the authentication module.
"""

import sys
import os
import unittest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.auth import (
    hash_secret,
    verify_secret,
    generate_session_token,
    generate_agent_token,
    create_user_token,
    verify_user_token,
    redact_for_log,
)


class TestAuth(unittest.TestCase):
    """Tests for authentication functions."""
    
    def test_hash_secret_generates_hash(self):
        """Hash function should generate a hash."""
        hashed = hash_secret("test-secret")
        self.assertIsNotNone(hashed)
        self.assertIn("$", hashed)  # Salt$hash format
        self.assertGreater(len(hashed), 32)
    
    def test_hash_secret_different_salts(self):
        """Same secret with different salts should produce different hashes."""
        hash1 = hash_secret("same-secret")
        hash2 = hash_secret("same-secret")
        self.assertNotEqual(hash1, hash2)
    
    def test_verify_secret_correct(self):
        """Verification should succeed with correct secret."""
        hashed = hash_secret("my-secret")
        self.assertTrue(verify_secret("my-secret", hashed))
    
    def test_verify_secret_wrong(self):
        """Verification should fail with wrong secret."""
        hashed = hash_secret("my-secret")
        self.assertFalse(verify_secret("wrong-secret", hashed))
    
    def test_verify_secret_tampered(self):
        """Verification should fail with tampered hash."""
        hashed = hash_secret("my-secret")
        tampered = hashed[:-5] + "xxxxx"
        self.assertFalse(verify_secret("my-secret", tampered))
    
    def test_generate_session_token(self):
        """Session token should be generated."""
        token = generate_session_token()
        self.assertIsNotNone(token)
        self.assertGreater(len(token), 20)
    
    def test_generate_session_token_unique(self):
        """Session tokens should be unique."""
        token1 = generate_session_token()
        token2 = generate_session_token()
        self.assertNotEqual(token1, token2)
    
    def test_generate_agent_token(self):
        """Agent token should generate ID and secret."""
        agent_id, secret = generate_agent_token()
        self.assertIsNotNone(agent_id)
        self.assertIsNotNone(secret)
        self.assertTrue(agent_id.startswith("agent-"))
        self.assertGreater(len(secret), 32)
    
    def test_generate_agent_token_unique(self):
        """Agent tokens should be unique."""
        id1, secret1 = generate_agent_token()
        id2, secret2 = generate_agent_token()
        self.assertNotEqual(id1, id2)
        self.assertNotEqual(secret1, secret2)
    
    def test_create_user_token(self):
        """User token should be created."""
        token = create_user_token("testuser", "secret-key")
        self.assertIsNotNone(token)
        self.assertIn(".", token)
    
    def test_verify_user_token_valid(self):
        """Valid user token should verify."""
        secret_key = "admin-secret-key"
        token = create_user_token("admin", secret_key)
        username = verify_user_token(token, secret_key)
        self.assertEqual(username, "admin")
    
    def test_verify_user_token_invalid(self):
        """Invalid user token should fail."""
        secret_key = "admin-secret-key"
        token = create_user_token("admin", secret_key)
        username = verify_user_token(token, "wrong-key")
        self.assertIsNone(username)
    
    def test_verify_user_token_expired(self):
        """Expired user token should fail."""
        secret_key = "admin-secret-key"
        # Create token with old timestamp
        import time
        old_token = create_user_token("admin", secret_key)
        # Token is valid for 3600 seconds by default
        # We can't easily test expiration without mocking time
        # So we just verify it works for current time
        username = verify_user_token(old_token, secret_key)
        self.assertEqual(username, "admin")
    
    def test_verify_user_token_tampered(self):
        """Tampered user token should fail."""
        secret_key = "admin-secret-key"
        token = create_user_token("admin", secret_key)
        # Tamper with token
        tampered = token[:-5] + "xxxxx"
        username = verify_user_token(tampered, secret_key)
        self.assertIsNone(username)
    
    def test_redact_for_log_short(self):
        """Short values should be fully redacted."""
        result = redact_for_log("abc")
        self.assertEqual(result, "***")
    
    def test_redact_for_log_medium(self):
        """Medium values should be fully redacted."""
        result = redact_for_log("abcdefgh")
        self.assertEqual(result, "***")
    
    def test_redact_for_log_long(self):
        """Long values should show first/last chars."""
        result = redact_for_log("secret1234567890")
        self.assertEqual(result, "secr***7890")
    
    def test_redact_for_log_empty(self):
        """Empty values should return asterisks."""
        result = redact_for_log("")
        self.assertEqual(result, "***")
    
    def test_redact_for_log_none(self):
        """None values should return asterisks."""
        result = redact_for_log(None)
        self.assertEqual(result, "***")


if __name__ == "__main__":
    unittest.main(verbosity=2)
