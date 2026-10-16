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
import re
import sys
import json
import time
import subprocess
import tempfile
import threading
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

# The public host:port the connector was last given. Written here by whichever
# process runs the connector (the notebook cell) and read by the agent, which
# answers the panel's heartbeats from a different thread and its own
# PlayitTunnel instance. Without this file the panel can only ever show
# "address unknown".
ADDRESS_FILE = os.path.join(PLAYIT_DIR, "public_address.json")

# The connector's own stdout is appended here (see `_start_output_drain`). It is
# the fallback source for the address: it survives a re-run of the notebook cell
# and is the only place the address exists when the instance that started the
# connector is not the one now asking for it.
PLAYIT_LOG = os.path.join(PLAYIT_DIR, "playit.log")

# Hostnames Playit issues tunnel addresses under: `name.playit.gg` and the very
# common `name.<region>.at.ply.gg` / `name.tun.ply.gg`. Custom domains (offered
# on some plans) are deliberately not matched: any-host matching would report
# unrelated hostnames from the connector's output as the join address.
PLAYIT_ADDRESS_PATTERN = (r'([a-z0-9][a-z0-9.-]*\.'
                          r'(?:playit\.gg|ply\.gg)):(\d{1,5})')

# How much of the log tail to scan. The connector is low volume, so this covers
# a whole session in practice.
_LOG_SCAN_BYTES = 64 * 1024

# Optional explicit override of the public address, e.g. "rahee.gl.at.ply.gg:25565".
# Set it when the connector's output cannot be parsed (Playit's own dashboard
# always shows the address). Read from the environment / Kaggle Secrets; it is
# never written to disk and always wins over a parsed value.
PLAYIT_ADDRESS_VAR = "PLAYIT_PUBLIC_ADDRESS"

# A non-Playit host:port is only trusted when the line names it as the address.
# This keeps the connector's other output (a tunnel-server IP, the local
# 127.0.0.1 target) from ever being reported as the join address.
_ADDRESS_LABELS = ("address", "public", "hostname", "join")
_IPV4_RE = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$')
_HOST_PORT_RE = re.compile(r'([A-Za-z0-9][A-Za-z0-9.-]*):(\d{1,5})')


def _is_public_ipv4(host: str) -> bool:
    """True for a routable IPv4 address (never loopback or a private range)."""
    parts = host.split(".")
    if len(parts) != 4:
        return False
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return False
    if any(n > 255 for n in nums):
        return False
    a, b = nums[0], nums[1]
    if a in (0, 10, 127):
        return False
    if a == 172 and 16 <= b <= 31:
        return False
    if a == 192 and b == 168:
        return False
    if a == 169 and b == 254:
        return False
    return True


def valid_public_address(text: str) -> Optional[str]:
    """Return "host:port" when `text` is a usable public address, else None.

    Used to validate the explicit ``PLAYIT_PUBLIC_ADDRESS`` override so a typo
    (or a loopback/private address) can never be shown as the join address.
    """
    if not isinstance(text, str):
        return None
    m = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9.-]*):(\d{1,5})', text.strip())
    if not m:
        return None
    host, port = m.group(1), m.group(2)
    if _IPV4_RE.match(host):
        if not _is_public_ipv4(host):
            return None
    elif "." not in host:
        return None
    return f"{host}:{port}"


def clear_public_address() -> None:
    """Start a fresh address record for a new connector.

    Removes the previous address and rotates the connector log, so the log
    fallback cannot recover a stale address from the connector that just went
    away. The old log is kept as ``playit.log.prev`` for diagnostics.
    """
    try:
        os.remove(ADDRESS_FILE)
    except OSError:
        pass
    try:
        if os.path.exists(PLAYIT_LOG):
            os.replace(PLAYIT_LOG, PLAYIT_LOG + ".prev")
    except OSError:
        pass


def _write_public_address(hostname: str, port: str) -> None:
    """Persist the connector's real public address for the agent to read.

    Written atomically (temp file + rename) so a concurrent reader never sees a
    half-written file. Never called with a guessed address: callers pass what
    the connector actually printed.
    """
    try:
        os.makedirs(PLAYIT_DIR, exist_ok=True)
        payload = {"hostname": hostname, "port": port,
                   "address": f"{hostname}:{port}"}
        tmp = f"{ADDRESS_FILE}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, ADDRESS_FILE)
    except OSError:
        pass


def _read_recorded_address() -> Optional[str]:
    """The address written to ADDRESS_FILE, when one is there and well formed."""
    try:
        with open(ADDRESS_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    address = data.get("address")
    if isinstance(address, str) and address.count(":") == 1 and address:
        return address
    hostname, port = data.get("hostname"), data.get("port")
    if hostname and port:
        return f"{hostname}:{port}"
    return None


def _scan_log_for_address() -> Optional[str]:
    """The last real Playit address in the connector's drained output, or None.

    Used when the address file is missing — the notebook cell was re-run without
    starting a new connector, or another instance started it. Only Playit
    hostnames are accepted, so the connector's other output cannot turn into a
    wrong join address. The log is rotated by ``clear_public_address`` whenever a
    new connector starts, so a stale address from a previous connector is never
    returned.
    """
    try:
        size = os.path.getsize(PLAYIT_LOG)
        with open(PLAYIT_LOG, "rb") as fh:
            fh.seek(max(0, size - _LOG_SCAN_BYTES))
            text = fh.read().decode("utf-8", "replace")
    except OSError:
        return None
    matches = re.findall(PLAYIT_ADDRESS_PATTERN, text, re.IGNORECASE)
    if not matches:
        return None
    hostname, port = matches[-1]
    return f"{hostname}:{port}"


def read_public_address() -> Optional[str]:
    """The public address to show, as "host:port", or None.

    Precedence: the explicit `PLAYIT_PUBLIC_ADDRESS` override (the reliable fix
    when the connector's output cannot be parsed), then the file written the
    moment the address was seen, then the connector log (so a re-run of the cell
    or another starter instance still reports the same real address). Returns
    None — never a placeholder — when no real address is known, so the panel
    keeps saying "address unknown" instead of showing a wrong one.
    """
    override = valid_public_address(os.environ.get(PLAYIT_ADDRESS_VAR, ""))
    if override:
        return override
    return _read_recorded_address() or _scan_log_for_address()


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
        # Bounded buffer of connector output, filled by a drain thread. The
        # agent's stdout pipe must be drained continuously: an unread PIPE fills
        # its OS buffer and freezes the connector, and a blocking readline()
        # with no output hangs the notebook cell indefinitely.
        self._output_lines = []
        
    def _redact(self, line: str) -> str:
        """Remove the tunnel token from connector output before print/log.

        The connector's stdout is forwarded (printed and appended to
        playit.log); if it ever echoes the account token, the token must not
        escape into a notebook cell's output or a log file.
        """
        for value in (self.token, os.environ.get(PLAYIT_TOKEN_VAR, "").strip()):
            if value and len(value) >= 8 and value in line:
                line = line.replace(value, "***")
        return line

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

        # A new connector gets a new public address. Drop the previous one first
        # so the panel never shows last session's address.
        clear_public_address()

        # Prepare command
        # Playit agent typically runs with a config directory
        cmd = [
            binary_path,
            "--data-dir", PLAYIT_DIR,
            "--no-auto-update",  # Don't auto-update in Kaggle
        ]
        
        # Add token if available. It is passed through the environment, never
        # as a command-line argument, so it cannot be read from `ps`.
        token = self._get_token()
        if token:
            self.token = token
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

            # Start draining stdout into a bounded buffer + log file *before*
            # reading, so the connector can never block on a full pipe.
            self._start_output_drain()

            # Read initial output to find claim code or errors
            self._read_initial_output()
            
            return True
            
        except Exception as e:
            print(f"❌ Failed to start agent: {e}")
            return False
    
    def _start_output_drain(self):
        """Continuously drain the connector's stdout into a buffer + log file."""
        import threading

        if self.process is None or self.process.stdout is None:
            return

        try:
            log_path = Path(PLAYIT_LOG)
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            log_path = None

        stream = self.process.stdout
        lines = self._output_lines

        def _drain():
            try:
                for raw in iter(stream.readline, b""):
                    if not raw:
                        break
                    line = raw.decode("utf-8", errors="ignore").rstrip("\r\n")
                    line = self._redact(line)
                    lines.append(line)
                    # Persist the address the moment the connector prints it, not
                    # only inside the initial 60s window: a later assignment must
                    # still reach the panel.
                    address = self._extract_address(line)
                    if address and address != self.public_address:
                        self._remember_address(address)
                    if len(lines) > 4000:
                        del lines[:2000]
                    if log_path is not None:
                        try:
                            with open(log_path, "a") as fh:
                                fh.write(line + "\n")
                        except OSError:
                            pass
            except Exception:
                pass

        threading.Thread(target=_drain, name="playit-output", daemon=True).start()

    def _read_initial_output(self, timeout: int = 60):
        """Watch the connector's output (bounded) for status, claim code, errors."""
        if not self.process:
            return

        print("\n📋 Agent output (watching for up to %ds):" % timeout)
        print("-" * 40)

        deadline = time.time() + timeout
        seen = 0
        while time.time() < deadline:
            lines = list(self._output_lines)
            ready = False
            for line in lines[seen:]:
                line = self._redact(line)
                line_lower = line.lower()
                if any(k in line_lower for k in ['claim', 'code', 'token', 'error', 'fail',
                                                 'ready', 'connect', 'address', 'hostname',
                                                 'port', 'http', 'tcp', 'udp']):
                    print(f"   {line.strip()[:100]}")
                if 'ready' in line_lower or 'listening' in line_lower:
                    print("\n✅ Agent appears to be ready")
                    ready = True
                    break
            seen = len(lines)
            if ready:
                break
            if self.process.poll() is not None:
                break
            time.sleep(0.5)

        print("-" * 40)

        # Parse for public address
        self._parse_public_address(list(self._output_lines))

    # See PLAYIT_ADDRESS_PATTERN: Playit's own domains only, and the port is
    # part of the match — a bare hostname is not something a player can use.
    ADDRESS_PATTERN = PLAYIT_ADDRESS_PATTERN

    def _extract_address(self, line: str) -> Optional[Tuple[str, str]]:
        """The public host:port in one connector output line, or None.

        A port is REQUIRED. A missing port must never be filled in with a
        guess, because that sends players to an address nobody is listening on.
        """
        # A Playit-owned hostname is unambiguous wherever it appears.
        match = re.search(self.ADDRESS_PATTERN, line, re.IGNORECASE)
        if match:
            return match.group(1), match.group(2)

        # Otherwise only trust a host:port on a line that names it as the
        # address: Playit in "show IPs instead of domains" mode prints a bare
        # public IP, and custom domains are possible. Requiring the label keeps
        # the tunnel-server IP and the local 127.0.0.1 target out.
        lowered = line.lower()
        if not any(label in lowered for label in _ADDRESS_LABELS):
            return None
        for m in _HOST_PORT_RE.finditer(line):
            host, port = m.group(1), m.group(2)
            if _IPV4_RE.match(host):
                if _is_public_ipv4(host):
                    return host, port
                continue
            if "." in host:  # a dotted hostname: custom or Playit domain
                return host, port
        return None

    def _remember_address(self, address: Tuple[str, str]) -> None:
        """Record a real address in memory *and* in the shared file.

        The file is how the agent learns the address: the notebook starts the
        connector, but the agent answers the panel's heartbeats from a separate
        thread and its own PlayitTunnel instance, so an in-memory value here
        never reached the panel before.
        """
        self.public_address = address
        _write_public_address(address[0], address[1])

    def _parse_public_address(self, output_lines: list):
        """Find and persist the first real address the connector printed.

        A port is REQUIRED; if no real address is observed we report nothing
        and point at the dashboard instead of inventing one.
        """
        for line in output_lines:
            address = self._extract_address(line)
            if address:
                self._remember_address(address)
                print(f"\n📍 Found address: {address[0]}:{address[1]}")
                return

        # Not found in output - the user has to read the dashboard.
        if not self.public_address:
            print("\n⚠️  Public address not found in the connector output")
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

        # Then the shared file: another process (the notebook cell) may be the
        # one that started the connector and parsed the address.
        persisted = read_public_address()
        if persisted:
            hostname, _, port = persisted.partition(":")
            self.public_address = (hostname, port)
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
        """Show the tail of the connector output after it exits."""
        if not self.process:
            return

        print("\n📋 Agent exit output:")
        for line in self._output_lines[-20:]:
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
