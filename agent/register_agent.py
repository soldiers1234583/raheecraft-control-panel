#!/usr/bin/env python3
"""
RaheeCraft Agent Registration Helper
=====================================
One-time script to register an agent with the backend and get credentials.

Usage:
    1. Deploy the backend to Render
    2. Set BACKEND_URL in this script
    3. Run: python agent/register_agent.py
    4. Copy the credentials to your Kaggle notebook environment
    
This script only runs ONCE to register a new agent.
After that, the agent uses the saved credentials.
"""

import os
import sys
import json

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import config, Config


def register_agent(backend_url: str) -> tuple[str, str]:
    """
    Register a new agent with the backend.
    
    Args:
        backend_url: The deployed backend URL (e.g., https://your-app.onrender.com)
    
    Returns:
        (agent_id, agent_secret) tuple
    """
    import httpx
    
    # Generate agent credentials
    from agent.auth import generate_agent_token
    agent_id, agent_secret = generate_agent_token()
    
    # Register with backend
    url = f"{backend_url}/api/agent/register"
    headers = {
        "X-Agent-ID": agent_id,
        "X-Agent-Token": agent_secret,
        "Content-Type": "application/json"
    }
    
    metadata = {
        "platform": "kaggle",
        "python_version": sys.version,
        "purpose": "minecraft-server-control"
    }
    
    print(f"Registering agent with {backend_url}...")
    print(f"  Agent ID: {agent_id}")
    print(f"  Backend: {backend_url}")
    
    try:
        response = httpx.post(
            url,
            headers=headers,
            json={"metadata": metadata},
            timeout=10.0
        )
        
        if response.status_code == 200:
            print(f"\n✅ Agent registered successfully!")
            print(f"\n📋 COPY THESE CREDENTIALS TO YOUR KAGGLE NOTEBOOK:")
            print(f"   os.environ['AGENT_ID'] = '{agent_id}'")
            print(f"   os.environ['AGENT_SECRET'] = '{agent_secret}'")
            print(f"\n   Backend URL: {backend_url}")
            return agent_id, agent_secret
        
        elif response.status_code == 409:
            print(f"\n⚠️  Agent already registered")
            print(f"   This agent ID already exists. Use different credentials.")
            return None, None
        
        else:
            print(f"\n❌ Registration failed: {response.status_code}")
            print(f"   Response: {response.text}")
            return None, None
            
    except httpx.ConnectError as e:
        print(f"\n❌ Cannot connect to backend: {e}")
        print(f"   Make sure the backend is deployed and accessible.")
        return None, None
    except Exception as e:
        print(f"\n❌ Error: {e}")
        return None, None


def main():
    """Main entry point."""
    print("=" * 60)
    print("RAHEECRAFT AGENT REGISTRATION")
    print("=" * 60)
    print()
    
    # Get backend URL from environment or prompt
    backend_url = os.environ.get("BACKEND_URL", "").strip()
    
    if not backend_url:
        print("Enter your deployed backend URL:")
        print("  (e.g., https://raheecraft-control-panel.onrender.com)")
        backend_url = input("  > ").strip()
    
    if not backend_url:
        print("\n❌ No backend URL provided")
        return
    
    if not backend_url.startswith("http"):
        backend_url = f"https://{backend_url}"
    
    # Remove trailing slash
    backend_url = backend_url.rstrip("/")
    
    print(f"\nBackend URL: {backend_url}")
    print()
    
    # Register
    agent_id, agent_secret = register_agent(backend_url)
    
    if agent_id and agent_secret:
        print("\n" + "=" * 60)
        print("NEXT STEPS:")
        print("=" * 60)
        print()
        print("1. Add these to your Kaggle notebook cell:")
        print(f"   os.environ['BACKEND_URL'] = '{backend_url}'")
        print(f"   os.environ['AGENT_ID'] = '{agent_id}'")
        print(f"   os.environ['AGENT_SECRET'] = '{agent_secret}'")
        print()
        print("2. Run the agent in another cell:")
        print("   import sys")
        print("   sys.path.insert(0, '/kaggle/working/raheecraft/agent')")
        print("   from agent.main import run_agent")
        print("   run_agent()")
        print()
        print("3. Visit your backend dashboard to see the agent status")
        print()
        print("⚠️  IMPORTANT: Keep AGENT_SECRET secret!")
        print("   Don't share it or commit it to version control.")
    else:
        print("\n❌ Registration failed. Check the error above.")


if __name__ == "__main__":
    main()
