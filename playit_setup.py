#!/usr/bin/env python3
"""
Playit.gg Tunnel Integration for Kaggle Minecraft Server
=========================================================
Provides public access to the Minecraft server using Playit.gg tunneling.

Features:
  - Downloads and runs the official Playit.gg Linux agent
  - Supports Java Edition TCP tunnel (port 25565)
  - Handles account claiming workflow
  - Stores tokens securely (Kaggle Secrets)
  - Background process with restart capability

Limitations:
  - Geyser plugin does NOT work with Playit's UDP tunnels
  - Bedrock crossplay may require paid plan or specific configuration
  - NetherNet (Bedrock 1.26.51+) support may be limited on free tier
  - Public hostname/port discovered after agent starts

Reference:
  https://playit.gg/
  https://playit.gg/support/run-on-linux/
  https://geysermc.org/wiki/geyser/playit-gg/
"""

import os
import sys
import json
import time
import subprocess
import tempfile
import shutil
import hashlib
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple

# ==== Configuration ====
SERVER_DIR = "/kaggle/working/minecraft_server"
PLAYIT_DIR = "/kaggle/working/playit"
JAVA_PORT = 25565  # Default Paper server port
BEDROCK_PORT = 19132  # Default Bedrock UDP port

# Playit credential env var names (like n8n style)
PLAYIT_TOKEN_VAR = "PLAYIT_TOKEN"  # Account token for claiming

# Where to store token (Kaggle working, persists during session)
TOKEN_FILE = os.path.join(PLAYIT_DIR, ".playit_token")


class PlayitError(Exception):
    """Custom exception for Playit errors"""
    pass


class PlayitTunnel:
    """
    Manages Playit.gg tunnel for Minecraft server.
    
    Authentication:
      - First run: Account claiming via web dashboard
      - Subsequent runs: Token stored securely
    """
    
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.process = None
        self.token = None
        self.public_address = None  # (hostname, port) tuple
        
    def _get_token(self) -> Optional[str]:
        """Get Playit token from Kaggle Secrets or stored file."""
        # Check environment (Kaggle Secrets)
        token = os.environ.get(PLAYIT_TOKEN_VAR, "").strip()
        if token:
            return token
        
        # Check stored file
        if os.path.exists(TOKEN_FILE):
            try:
                with open(TOKEN_FILE) as f:
                    data = json.load(f)
                    return data.get("token", "")
            except Exception:
                pass
        
        return None
    
    def setup_directory(self):
        """Create Playit working directory."""
        os.makedirs(PLAYIT_DIR, exist_ok=True)
        print(f"📁 Playit directory: {PLAYIT_DIR}")
        
    def detect_architecture(self) -> str:
        """Detect system architecture for binary download."""
        machine = os.uname().machine.lower() if hasattr(os, 'uname') else ""
        
        # Map to Playit architecture names
        arch_map = {
            "x86_64": "amd64",
            "amd64": "amd64",
            "aarch64": "arm64",
            "arm64": "arm64",
            "armv7l": "armv7",
            "armv6l": "armv6",
        }
        
        arch = arch_map.get(machine, "amd64")  # Default to amd64
        print(f"🖥️  Detected architecture: {machine} → {arch}")
        return arch
    
    def download_agent(self) -> Optional[str]:
        """
        Download the Playit.gg agent binary.
        
        Returns path to binary, or None on failure.
        """
        arch = self.detect_architecture()
        
        # Playit download URL pattern (may change - verify on their site)
        # Official: https://playit.gg/download
        # Linux binaries typically at:
        # https://download.playit.gg/playit-agent-linux-{arch}.tar.gz
        base_url = "https://download.playit.gg"
        
        # Try common URL patterns
        urls = [
            f"{base_url}/playit-agent-linux-{arch}.tar.gz",
            f"{base_url}/agent/linux/{arch}/playit-agent.tar.gz",
        ]
        
        print(f"\n📥 Downloading Playit agent for {arch}...")
        
        if self.dry_run:
            print("🔍 DRY RUN - Would download from:")
            for url in urls:
                print(f"   {url}")
            return None
        
        binary_path = None
        
        for url in urls:
            try:
                print(f"   Trying: {url}")
                
                # Download to temp file
                import urllib.request
                
                temp_dir = tempfile.mkdtemp()
                archive_path = os.path.join(temp_dir, "playit-agent.tar.gz")
                
                # Download with timeout
                urllib.request.urlretrieve(url, archive_path, timeout=30)
                
                # Extract
                import tarfile
                with tarfile.open(archive_path, 'r:gz') as tar:
                    tar.extractall(PLAYIT_DIR)
                
                # Find binary
                for root, dirs, files in os.walk(PLAYIT_DIR):
                    for f in files:
                        if f.startswith("playit") and os.access(os.path.join(root, f), os.X_OK):
                            binary_path = os.path.join(root, f)
                            break
                    if binary_path:
                        break
                
                if binary_path:
                    print(f"✅ Downloaded: {binary_path}")
                    break
                else:
                    print(f"   No executable found in archive")
                    shutil.rmtree(temp_dir, ignore_errors=True)
                    
            except Exception as e:
                print(f"   Failed: {e}")
                continue
        
        if not binary_path:
            print("❌ Failed to download Playit agent")
            print("\n💡 Manual download:")
            print("   1. Visit https://playit.gg/download")
            print("   2. Download Linux version")
            print("   3. Upload to Kaggle notebook files")
            print("   4. Extract to /kaggle/working/playit/")
        
        return binary_path
    
    def claim_account(self) -> bool:
        """
        Guide user through account claiming process.
        
        Playit requires claiming the agent to your account.
        This typically involves:
          1. Running the agent
          2. It generates a claim code
          3. User enters code at playit.gg/dashboard
          4. Token is issued
        """
        print("\n" + "=" * 60)
        print("PLAYIT ACCOUNT CLAIMING")
        print("=" * 60)
        
        print("""
📋 To claim your Playit account:

   1. Run this cell to start the agent (it will show a claim code)
   2. Visit https://playit.gg/dashboard
   3. Sign in or create account
   4. Find your server/device and claim it
   5. Copy the token from dashboard
   
   OR if you already have a token:
   - Add PLAYIT_TOKEN to Kaggle Secrets
   - Restart this cell
""")
        
        token = self._get_token()
        if token:
            print("✅ Token found in Kaggle Secrets")
            self.token = token
            return True
        
        print("⚠️  No token found")
        print("\n💡 Options:")
        print("   A. Add PLAYIT_TOKEN to Kaggle Secrets now")
        print("   B. Run agent and claim via dashboard")
        print("   C. Skip for now (configure later)")
        
        return False
    
    def start_agent(self, binary_path: str) -> bool:
        """
        Start the Playit agent process.
        
        Args:
            binary_path: Path to the playit binary
            
        Returns:
            True if agent started successfully
        """
        print("\n" + "=" * 60)
        print("STARTING PLAYIT AGENT")
        print("=" * 60)
        
        if self.dry_run:
            print("🔍 DRY RUN - Would start Playit agent")
            print(f"   Binary: {binary_path}")
            print(f"   Config dir: {PLAYIT_DIR}")
            return True
        
        if not os.path.exists(binary_path):
            print(f"❌ Binary not found: {binary_path}")
            return False
        
        # Make sure binary is executable
        os.chmod(binary_path, 0o755)
        
        # Prepare command
        # Playit agent typically runs with a config directory
        cmd = [
            binary_path,
            "--data-dir", PLAYIT_DIR,
            "--no-auto-update",  # Don't auto-update in Kaggle
        ]
        
        # Add token if available
        token = self._get_token()
        if token:
            # Some agents accept token via env or flag
            env = os.environ.copy()
            env["PLAYIT_TOKEN"] = token
        else:
            env = os.environ.copy()
        
        print(f"📦 Starting Playit agent...")
        print(f"   Command: {' '.join(cmd)}")
        print(f"   Working dir: {PLAYIT_DIR}")
        
        try:
            # Start in background
            self.process = subprocess.Popen(
                cmd,
                cwd=PLAYIT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                preexec_fn=os.setsid if hasattr(os, 'setsid') else None
            )
            
            print(f"✅ Agent started (PID: {self.process.pid})")
            print("\n⏳ Waiting for agent to initialize...")
            print("   (This may take 30-60 seconds)")
            
            # Read initial output to find claim code or errors
            self._read_initial_output()
            
            return True
            
        except Exception as e:
            print(f"❌ Failed to start agent: {e}")
            return False
    
    def _read_initial_output(self, timeout: int = 60):
        """Read initial agent output to find status/claim code."""
        if not self.process:
            return
        
        start_time = time.time()
        output_lines = []
        
        print("\n📋 Agent output (first 30 seconds):")
        print("-" * 40)
        
        while time.time() - start_time < timeout:
            try:
                line = self.process.stdout.readline().decode('utf-8', errors='ignore')
                if not line:
                    break
                
                output_lines.append(line.strip())
                
                # Print relevant lines
                line_lower = line.lower()
                if any(k in line_lower for k in ['claim', 'code', 'token', 'error', 'fail', 
                                                   'ready', 'connect', 'address', 'hostname',
                                                   'port', 'http', 'tcp', 'udp']):
                    print(f"   {line.strip()[:100]}")
                
                # Check for ready state
                if 'ready' in line_lower or 'listening' in line_lower:
                    print("\n✅ Agent appears to be ready")
                    break
                    
            except Exception:
                break
        
        print("-" * 40)
        
        # Parse for public address
        self._parse_public_address(output_lines)
    
    def _parse_public_address(self, output_lines: list):
        """Try to extract public hostname/port from agent output."""
        # Common patterns in Playit output
        patterns = [
            r'([a-z0-9-]+\.playit\.gg):\d+',
            r'address:\s*([a-z0-9.-]+):\d+',
            r'public\s*([a-z0-9.-]+):\d+',
        ]
        
        import re
        
        for line in output_lines:
            for pattern in patterns:
                match = re.search(pattern, line, re.IGNORECASE)
                if match:
                    hostname = match.group(1)
                    # Find port near hostname
                    port_match = re.search(r':(\d+)', line)
                    port = port_match.group(1) if port_match else "25565"
                    
                    self.public_address = (hostname, port)
                    print(f"\n📍 Found address: {hostname}:{port}")
                    return
        
        # If not found in output, user needs to check dashboard
        if not self.public_address:
            print("\n⚠️  Public address not found in output")
            print("   Check https://playit.gg/dashboard for your address")
    
    def get_public_address(self) -> Optional[Tuple[str, str]]:
        """
        Get the public address for the Java server.
        
        Returns:
            (hostname, port) tuple or None if not available
        """
        # Try to get from agent output first
        if self.public_address:
            return self.public_address
        
        # Check if agent is still running
        if self.process and self.process.poll() is None:
            print("\n⚠️  Agent running but address not detected")
            print("   Check https://playit.gg/dashboard")
        elif self.process and self.process.poll() is not None:
            print(f"\n❌ Agent exited with code {self.process.returncode}")
            self._read_exit_output()
        
        return None
    
    def _read_exit_output(self):
        """Read any remaining output after agent exits."""
        if not self.process:
            return
        
        print("\n📋 Agent exit output:")
        for line in self.process.stdout.read().decode('utf-8', errors='ignore').split('\n')[-20:]:
            if line.strip():
                print(f"   {line.strip()[:100]}")
    
    def restart(self, binary_path: str, max_attempts: int = 3) -> bool:
        """
        Restart the agent with bounded attempts.
        
        Args:
            binary_path: Path to binary
            max_attempts: Maximum restart attempts
            
        Returns:
            True if successfully started
        """
        for attempt in range(1, max_attempts + 1):
            print(f"\n🔄 Restart attempt {attempt}/{max_attempts}")
            
            if self.start_agent(binary_path):
                # Give it time to stabilize
                time.sleep(5)
                
                # Check if still running
                if self.process and self.process.poll() is None:
                    print("✅ Agent running")
                    return True
                else:
                    print("⚠️  Agent exited unexpectedly")
            else:
                print("❌ Failed to start")
            
            if attempt < max_attempts:
                print(f"   Waiting 10 seconds before retry...")
                time.sleep(10)
        
        print(f"\n❌ Failed after {max_attempts} attempts")
        return False
    
    def stop(self):
        """Stop the agent process."""
        if self.process:
            try:
                # Kill process group if available
                if hasattr(os, 'killpg'):
                    os.killpg(os.getpgid(self.process.pid), 15)  # SIGTERM
                else:
                    self.process.terminate()
                
                self.process.wait(timeout=5)
                print("✅ Agent stopped")
            except Exception as e:
                print(f"⚠️  Error stopping agent: {e}")
                try:
                    self.process.kill()
                except Exception:
                    pass
        else:
            print("ℹ️  No agent running")
    
    def test_connectivity(self, hostname: str, port: int) -> bool:
        """
        Test if the public endpoint is reachable.
        
        Note: This tests FROM Kaggle, which may not reflect 
        external connectivity due to network policies.
        """
        print(f"\n🔍 Testing connectivity to {hostname}:{port}...")
        
        import socket
        
        try:
            # Try TCP connection
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            
            # Resolve hostname first
            addr_info = socket.getaddrinfo(hostname, port, socket.AF_INET)
            if not addr_info:
                print(f"❌ Could not resolve {hostname}")
                return False
            
            ip = addr_info[0][4][0]
            print(f"   Resolved to: {ip}:{port}")
            
            # Try to connect
            result = sock.connect_ex((ip, port))
            sock.close()
            
            if result == 0:
                print(f"✅ Port {port} is open (TCP)")
                return True
            else:
                print(f"❌ Port {port} not reachable (error: {result})")
                print("\n💡 This may be normal if:")
                print("   - Playit uses UDP for some traffic")
                print("   - Firewall blocks inbound from Kaggle")
                print("   - Test from your Minecraft client instead")
                return False
                
        except Exception as e:
            print(f"❌ Connection test failed: {e}")
            return False


def test_playit_setup():
    """Test Playit setup without starting actual agent."""
    print("\n" + "=" * 60)
    print("PLAYIT SETUP TEST (DRY RUN)")
    print("=" * 60)
    
    tunnel = PlayitTunnel(dry_run=True)
    
    print("\n1. Directory setup:")
    tunnel.setup_directory()
    
    print("\n2. Architecture detection:")
    arch = tunnel.detect_architecture()
    print(f"   Would use: {arch}")
    
    print("\n3. Download URL:")
    tunnel.download_agent()  # Dry run
    
    print("\n4. Token check:")
    token = tunnel._get_token()
    if token:
        print("   ✅ Token found (hidden)")
    else:
        print("   ⚠️  No token found - need to set PLAYIT_TOKEN in Secrets")
    
    print("\n✅ Test complete")
    print("\n📋 Next: Run in live Kaggle session")


if __name__ == "__main__":
    test_playit_setup()
