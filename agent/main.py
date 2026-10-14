#!/usr/bin/env python3
"""
RaheeCraft Control Agent
=========================
Kaggle-side agent that connects to the control panel backend.

The agent:
1. Registers with the backend
2. Sends periodic heartbeats with server status
3. Reconnects with exponential backoff on failure

Usage in Kaggle notebook:
    import os
    os.environ['BACKEND_URL'] = 'https://your-backend-url'
    os.environ['AGENT_ID'] = 'agent-xxxxxxxx'
    os.environ['AGENT_SECRET'] = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
    os.environ['SERVER_DIR'] = '/kaggle/working/minecraft_server'
    
    from agent.main import run_agent
    run_agent()
"""

import os
import sys
import time
import json
import logging
import traceback
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.config import Config, config
from agent.status import get_agent_status

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)


class ControlAgent:
    """Control agent that connects to backend."""
    
    def __init__(self):
        self.running = False
        self.session = None
    
    def register(self) -> bool:
        """
        Register agent with backend.
        
        Returns True if registration successful.
        """
        if not config.AGENT_ID or not config.AGENT_SECRET:
            logger.error("Agent ID or secret not configured")
            return False
        
        if not config.BACKEND_URL:
            logger.error("BACKEND_URL not configured")
            return False
        
        import httpx
        
        url = f"{config.BACKEND_URL}/api/agent/register"
        headers = {
            "X-Agent-ID": config.AGENT_ID,
            "X-Agent-Token": config.AGENT_SECRET,
            "Content-Type": "application/json"
        }
        
        # Add metadata
        metadata = {
            "platform": "kaggle",
            "python_version": sys.version,
            "server_dir": config.SERVER_DIR
        }
        
        try:
            response = httpx.post(
                url,
                headers=headers,
                json={"metadata": metadata},
                timeout=config.CONNECT_TIMEOUT
            )
            
            if response.status_code == 200:
                logger.info(f"Agent registered: {config.AGENT_ID[:8]}...")
                return True
            elif response.status_code == 409:
                logger.info(f"Agent already registered: {config.AGENT_ID[:8]}...")
                return True
            else:
                logger.error(f"Registration failed: {response.status_code} {response.text}")
                return False
                
        except httpx.ConnectError as e:
            logger.warning(f"Cannot connect to backend: {e}")
            return False
        except Exception as e:
            logger.error(f"Registration error: {e}")
            return False
    
    def send_heartbeat(self) -> bool:
        """
        Send heartbeat with server status to backend.
        
        Returns True if successful.
        """
        if not config.AGENT_ID or not config.AGENT_SECRET:
            return False
        
        if not config.BACKEND_URL:
            return False
        
        import httpx
        
        status = get_agent_status()
        
        url = f"{config.BACKEND_URL}/api/agent/heartbeat"
        headers = {
            "X-Agent-ID": config.AGENT_ID,
            "X-Agent-Token": config.AGENT_SECRET,
            "Content-Type": "application/json"
        }
        
        try:
            response = httpx.post(
                url,
                headers=headers,
                json=status,
                timeout=config.CONNECT_TIMEOUT
            )
            
            if response.status_code == 200:
                return True
            else:
                logger.warning(f"Heartbeat failed: {response.status_code}")
                return False
                
        except httpx.ConnectError:
            logger.debug("Backend not reachable")
            return False
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")
            return False
    
    def run(self):
        """
        Main agent loop.
        
        1. Register with backend
        2. Send periodic heartbeats
        3. Reconnect on failure with backoff
        """
        logger.info("=" * 50)
        logger.info("RaheeCraft Control Agent starting")
        logger.info(f"Agent ID: {config.AGENT_ID[:8] if config.AGENT_ID else 'Not set'}...")
        logger.info(f"Backend: {config.BACKEND_URL}")
        logger.info(f"Server dir: {config.SERVER_DIR}")
        logger.info("=" * 50)
        
        self.running = True
        retry_delay = config.RETRY_BASE_DELAY
        max_retries = config.MAX_RETRIES
        
        while self.running:
            # Try to register
            if not self.register():
                logger.warning(f"Registration failed, retrying in {retry_delay}s...")
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)  # Exponential backoff
                continue
            
            # Reset retry delay on successful registration
            retry_delay = config.RETRY_BASE_DELAY
            
            # Send heartbeats
            while self.running:
                success = self.send_heartbeat()
                
                if not success:
                    logger.warning("Heartbeat failed, will retry on next cycle")
                
                time.sleep(config.POLL_INTERVAL_SECONDS)
        
        logger.info("Agent stopped")


def run_agent():
    """Entry point for running the agent."""
    agent = ControlAgent()
    agent.run()


if __name__ == "__main__":
    run_agent()
