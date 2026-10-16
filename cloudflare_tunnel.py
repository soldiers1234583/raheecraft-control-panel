#!/usr/bin/env python3
"""
RaheeCraft Cloudflare Tunnel
============================
Exposes the in-kernel RaheeCraft web panel (``127.0.0.1:8080``) through the
existing Cloudflare tunnel with a remotely-managed ("token") connector.

Why the connector runs *here*
-----------------------------
The panel listens on loopback inside the Kaggle kernel, so the connector has
to run in that same kernel: a connector anywhere else cannot reach
``localhost:8080``. A token connector takes every ingress rule from the
Cloudflare dashboard, so no hostname, port mapping or tunnel secret is stored
in this repo or passed on a command line.

Credential handling
-------------------
* The tunnel token is read from the environment only
  (``CLOUDFLARE_TUNNEL_TOKEN``, populated from Kaggle Secrets by the notebook).
  It is never passed as a command-line argument — argv is visible in ``ps`` —
  never printed, and scrubbed from connector output before it is buffered,
  logged or returned.
* This module never creates, changes or deletes a DNS record. The public
  hostname is configured once in the Cloudflare dashboard, on purpose.
* No ``sudo`` and no persistent service: the connector is downloaded to a
  writable directory and run as a normal background process, so it lives and
  dies with the ephemeral runtime.

Output draining
---------------
The connector's stdout is drained continuously into a bounded ring buffer and
a log file. Leaving a child's ``stdout=PIPE`` undrained fills the 64 KiB pipe
buffer and freezes the child — the exact failure this project already hit with
the Paper JVM.

Status reporting is honest by construction: ``start()`` only reports success
after the connector itself prints that it registered a tunnel connection, and
an early exit is reported with the reason Cloudflare gave.
"""

import os
import platform
import shlex
import shutil
import signal
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional

# Environment variable holding the tunnel token (Kaggle Secret name).
TOKEN_VAR = "CLOUDFLARE_TUNNEL_TOKEN"
# cloudflared's own variable name for a remotely-managed connector token. The
# child always gets this one too; without it `cloudflared tunnel run` fails with
# "requires the ID or name of the tunnel".
CONNECTOR_TOKEN_VAR = "TUNNEL_TOKEN"
# Optional overrides.
TUNNEL_ID_VAR = "CLOUDFLARE_TUNNEL_ID"
HOSTNAME_VAR = "CLOUDFLARE_PANEL_HOSTNAME"
BINARY_VAR = "CLOUDFLARED_BIN"
# Optional command prefix the connector is launched through. Needed where the
# host cannot give a Go binary a DNS config: on Android/Termux ``/etc`` is a
# read-only symlink to ``/system/etc`` with no ``resolv.conf``, so the Go
# resolver falls back to 127.0.0.1:53 and every lookup fails. Running the
# connector under a wrapper that supplies the file fixes it without root:
#   CLOUDFLARED_LAUNCHER="proot -b /data/data/com.termux/files/usr/etc/"\
#   "resolv.conf:/system/etc/resolv.conf -r /"
# The token still travels through the environment, never on argv.
LAUNCHER_VAR = "CLOUDFLARED_LAUNCHER"

# The tunnel this panel is meant to use (informational: the token, not the id,
# is what authenticates a remotely-managed connector).
DEFAULT_TUNNEL_ID = "96a8853c-45cf-4b9a-a92e-7b412262d8a6"
# The hostname the dashboard must route to this connector.
DEFAULT_HOSTNAME = "panel.soldiers1234583.dpdns.org"

# Official release host (verified reachable from a Kaggle runtime).
RELEASE_URL = ("https://github.com/cloudflare/cloudflared/releases/latest/"
               "download/cloudflared-linux-{arch}")
MAX_DOWNLOAD_BYTES = 160 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 300

# Bounded console buffer (lines) — never grows without limit.
CONSOLE_MAX_LINES = 500

# Connector output markers.
_READY_MARKERS = ("registered tunnel connection", "connection registered")
_FATAL_MARKERS = (
    "invalid tunnel secret",
    "failed to unmarshal credentials",
    "unauthorized",
    "credentials are not valid",
    "failed to connect to origin",   # hostname/DNS not configured yet
    "no such host",
)


class CloudflareError(Exception):
    """Raised when the connector cannot be prepared or started."""


class CloudflareTunnel:
    """A token-managed cloudflared connector for the local panel.

    Example (notebook):
        tunnel = CloudflareTunnel(local_port=8080)
        if tunnel.has_token():
            tunnel.start()
            result = tunnel.wait_for_connection(timeout=90)
            print(result["reason"])
    """

    def __init__(self,
                 token: Optional[str] = None,
                 tunnel_id: Optional[str] = None,
                 hostname: Optional[str] = None,
                 local_port: int = 8080,
                 binary_path: Optional[str] = None,
                 dry_run: bool = False):
        self.dry_run = dry_run
        self.local_port = int(local_port)
        self.tunnel_id = (tunnel_id
                          or os.environ.get(TUNNEL_ID_VAR, "").strip()
                          or DEFAULT_TUNNEL_ID)
        self.hostname = (hostname
                         or os.environ.get(HOSTNAME_VAR, "").strip()
                         or DEFAULT_HOSTNAME)
        self._explicit_token = token
        self._binary_path = binary_path
        self.process: Optional[subprocess.Popen] = None
        self.connected = False
        self.connections: Dict[int, str] = {}
        self.last_error: Optional[str] = None
        self._console = deque(maxlen=CONSOLE_MAX_LINES)
        self._console_lock = threading.Lock()
        self._drain_thread: Optional[threading.Thread] = None
        self._stop_requested = False

    # --- token -------------------------------------------------------------

    def _get_token(self) -> Optional[str]:
        """The tunnel token, from the argument or the environment. Never logged."""
        if self._explicit_token:
            return self._explicit_token.strip()
        return (os.environ.get(TOKEN_VAR, "") or "").strip() or None

    def has_token(self) -> bool:
        return bool(self._get_token())

    # --- launcher ----------------------------------------------------------

    def launcher_prefix(self, strict: bool = False) -> list:
        """Command prefix the connector runs under (empty on a normal host).

        Set ``CLOUDFLARED_LAUNCHER`` to a command line such as
        ``proot -b <resolv.conf>:/system/etc/resolv.conf -r /`` where the Go
        runtime cannot read ``/etc/resolv.conf`` itself. Parsed with
        ``shlex`` so quoted arguments survive; ``strict=True`` (used by
        ``start()``) raises instead of quietly running the connector without
        the wrapper it was told to use.
        """
        raw = (os.environ.get(LAUNCHER_VAR, "") or "").strip()
        if not raw:
            return []
        try:
            return shlex.split(raw)
        except ValueError as exc:
            message = f"invalid {LAUNCHER_VAR}: {exc}"
            self.last_error = message
            if strict:
                raise CloudflareError(message) from exc
            return []

    def _redact(self, text: str) -> str:
        """Remove the tunnel token from connector output before it is kept.

        cloudflared itself never echoes the token, but the buffer, the log file
        and the status payload are all readable, so the token is scrubbed
        defensively.
        """
        if not text:
            return text
        for value in (self._get_token(), os.environ.get(TOKEN_VAR, "").strip(),
                      os.environ.get(CONNECTOR_TOKEN_VAR, "").strip()):
            if value and len(value) > 8:
                text = text.replace(value, "***")
        return text

    # --- binary ------------------------------------------------------------

    def _install_dir(self) -> Path:
        """Where a downloaded connector is kept (session-scoped).

        Resolved on each call so ``CLOUDFLARED_DIR`` set by a caller or a test
        is honoured rather than frozen at import time.
        """
        path = Path(os.environ.get("CLOUDFLARED_DIR",
                                   str(Path.home() / ".rahee-bin")))
        path.mkdir(parents=True, exist_ok=True)
        return path

    def log_path(self) -> Path:
        return self._install_dir() / "cloudflared.log"

    def find_binary(self) -> Optional[str]:
        """Locate a usable cloudflared, in order of preference."""
        candidates = [
            self._binary_path,
            os.environ.get(BINARY_VAR, "").strip(),
            shutil.which("cloudflared"),
            str(self._install_dir() / "cloudflared"),
            "/usr/local/bin/cloudflared",
        ]
        for candidate in candidates:
            if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
                return candidate
        return None

    @staticmethod
    def _arch() -> str:
        machine = platform.machine().lower()
        if machine in ("x86_64", "amd64"):
            return "amd64"
        if machine in ("aarch64", "arm64"):
            return "arm64"
        raise CloudflareError(f"unsupported architecture for cloudflared: {machine}")

    def download_binary(self) -> Optional[str]:
        """Download the official connector binary (no sudo, no package manager).

        Verifies HTTP status, a size cap, and the ELF magic before installing,
        then renames atomically so an interrupted download can never leave a
        half-written executable in place.
        """
        existing = self.find_binary()
        if existing:
            return existing
        if self.dry_run:
            raise CloudflareError("dry_run: refusing to download cloudflared")

        url = RELEASE_URL.format(arch=self._arch())
        target = self._install_dir() / "cloudflared"
        tmp = target.with_suffix(".part")
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "rahee-panel"})
            with urllib.request.urlopen(request, timeout=60) as response:
                if response.status != 200:
                    raise CloudflareError(
                        f"cloudflared download failed: HTTP {response.status}")
                size = 0
                with open(tmp, "wb") as fh:
                    while True:
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise CloudflareError("cloudflared download exceeded the size cap")
                        fh.write(chunk)
                if size == 0:
                    raise CloudflareError("cloudflared download was empty")
            with open(tmp, "rb") as fh:
                if fh.read(4) != b"\x7fELF":
                    raise CloudflareError("downloaded file is not an ELF executable")
            os.chmod(tmp, stat.S_IRWXU)
            os.replace(tmp, target)
            return str(target)
        except CloudflareError:
            raise
        except Exception as exc:
            raise CloudflareError(f"could not download cloudflared: {exc}") from exc
        finally:
            if tmp.exists():
                try:
                    tmp.unlink()
                except OSError:
                    pass

    # --- output draining ---------------------------------------------------

    def _start_output_drain(self) -> None:
        """Drain the connector's stdout into a bounded buffer + log file."""
        if self.process is None or self.process.stdout is None:
            return
        stream = self.process.stdout
        log_path = self.log_path()

        def _pump() -> None:
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(log_path, "a", encoding="utf-8") as log:
                    for raw in stream:                       # drains the pipe
                        line = self._redact(raw.rstrip("\n"))
                        if not line.strip():
                            continue
                        with self._console_lock:
                            self._console.append(line)
                        try:
                            log.write(line + "\n")
                            log.flush()
                        except OSError:
                            pass
                        self._note_marker(line)
            except Exception as exc:                         # never kill the app
                self.last_error = self.last_error or f"log drain stopped: {exc}"
            finally:
                try:                                         # release the pipe
                    stream.close()
                except Exception:
                    pass

        self._drain_thread = threading.Thread(
            target=_pump, name="cloudflared-log", daemon=True)
        self._drain_thread.start()

    def _note_marker(self, line: str) -> None:
        """Track connection state from the connector's own output."""
        lowered = line.lower()
        if any(marker in lowered for marker in _READY_MARKERS):
            self.connected = True
            index = 0
            for token_part in lowered.split():
                if token_part.startswith("connindex="):
                    try:
                        index = int(token_part.split("=", 1)[1])
                    except ValueError:
                        index = 0
            self.connections[index] = line.strip()[:200]
        for marker in _FATAL_MARKERS:
            if marker in lowered:
                self.last_error = line.strip()[:300]
                break

    # --- lifecycle ---------------------------------------------------------

    def start(self) -> bool:
        """Start the connector with the token supplied through the environment."""
        if self.process is not None and self.process.poll() is None:
            self.last_error = "a connector is already running"
            return False

        token = self._get_token()
        if not token:
            self.last_error = (
                f"no tunnel token: add the secret {TOKEN_VAR} in Kaggle "
                f"(Add-ons -> Secrets) and attach it to this notebook; a token "
                f"connector cannot be authenticated with the tunnel id alone"
            )
            return False

        binary = self.find_binary()
        if binary is None:
            binary = self.download_binary()
        if binary is None:
            self.last_error = "cloudflared binary unavailable"
            return False

        if self.dry_run:
            self.last_error = "dry_run: refusing to start cloudflared"
            return False

        env = dict(os.environ)
        # Token through the environment only: argv is visible in `ps`.
        # CLOUDFLARE_TUNNEL_TOKEN is this project's name for the secret;
        # TUNNEL_TOKEN is the name cloudflared itself reads. Both are set, so a
        # token supplied either way authenticates the connector.
        env[TOKEN_VAR] = token
        env["TUNNEL_TOKEN"] = token
        try:
            prefix = self.launcher_prefix(strict=True)
        except CloudflareError:
            return False
        command = prefix + [binary, "tunnel", "--no-autoupdate", "run"]
        try:
            self._stop_requested = False
            self.process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=env,
                text=True,
                bufsize=1,
                # Own process group: stopping the connector stops everything it
                # spawned. Otherwise a leftover child keeps the stdout pipe
                # open, the drain thread never sees EOF, and shutdown blocks.
                start_new_session=True,
            )
        except Exception as exc:
            self.last_error = f"could not start cloudflared: {exc}"
            self.process = None
            return False

        self._start_output_drain()
        return True

    def wait_for_connection(self, timeout: int = 90) -> Dict[str, Any]:
        """Wait until the connector reports a registered connection.

        Returns a dict with ``connected``, ``reason`` and ``connections``. A
        process that exits first is reported as a failure with the reason the
        connector printed — success is never assumed.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.connected:
                break
            if self.process is not None and self.process.poll() is not None:
                break
            time.sleep(1)
        self._collect_exit()

        if self.connected and (self.process is not None
                              and self.process.poll() is None):
            return {
                "connected": True,
                "reason": f"connector registered {len(self.connections)} connection(s)",
                "connections": len(self.connections),
            }
        if self.process is None:
            return {"connected": False, "reason": self.last_error or "not started",
                    "connections": 0}
        if self.process.poll() is not None:
            tail = self.tail(3)
            return {
                "connected": False,
                "reason": (self.last_error
                           or f"connector exited with code {self.process.returncode}"),
                "connections": len(self.connections),
                "tail": tail,
            }
        return {
            "connected": False,
            "reason": self.last_error or f"no connection registered within {timeout}s",
            "connections": len(self.connections),
        }

    def _collect_exit(self) -> None:
        """Reap the connector if it already exited, and keep its last words."""
        if self.process is not None and self.process.poll() is not None:
            if self.last_error is None:
                tail = self.tail(3)
                if tail:
                    self.last_error = tail[-1][:300]

    def verify_local_service(self, timeout: int = 5) -> bool:
        """Does the thing the tunnel points at actually answer on loopback?"""
        url = f"http://127.0.0.1:{self.local_port}/health"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                return response.status == 200
        except urllib.error.HTTPError as exc:
            return exc.code < 500
        except Exception:
            return False

    def verify_public_service(self, hostname: str = None, scheme: str = "https",
                              timeout: int = 20) -> Dict[str, Any]:
        """Does the panel actually answer on the *public* hostname?

        A connected connector is not the same as a working URL: the hostname
        must also exist in the Cloudflare dashboard and route to this
        connector. This checks the whole path (edge -> tunnel -> connector ->
        panel) instead of assuming it, and never invents a hostname.
        """
        name = (hostname or self.hostname or "").strip()
        if not name:
            return {"ok": False, "url": None, "detail": "no hostname configured"}
        url = f"{scheme}://{name}/health"
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "rahee-panel"})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(300).decode("utf-8", "replace")
                ok = response.status == 200 and "healthy" in body
                return {
                    "ok": ok,
                    "url": url,
                    "status": response.status,
                    "detail": (body.strip()[:160] if ok
                               else f"unexpected response body: {body.strip()[:80]}"),
                }
        except urllib.error.HTTPError as exc:
            return {"ok": False, "url": url, "status": exc.code,
                    "detail": f"HTTP {exc.code}"}
        except Exception as exc:
            return {"ok": False, "url": url,
                    "detail": f"{type(exc).__name__}: {exc}"}

    def tail(self, lines: int = 20) -> list:
        with self._console_lock:
            return list(self._console)[-lines:]

    def status(self) -> Dict[str, Any]:
        """Everything a caller needs to report honestly. Secrets are redacted."""
        running = self.process is not None and self.process.poll() is None
        return {
            "running": running,
            "connected": bool(self.connected and running),
            "pid": self.process.pid if self.process is not None else None,
            "exit_code": (self.process.returncode if self.process is not None
                          and self.process.poll() is not None else None),
            "tunnel_id": self.tunnel_id,
            "hostname": self.hostname,
            "local_service": f"http://127.0.0.1:{self.local_port}",
            "local_service_ok": self.verify_local_service(),
            "token_present": self.has_token(),
            "launcher": " ".join(self.launcher_prefix()),
            "connections": len(self.connections),
            "last_error": self.last_error,
            "log_file": str(self.log_path()),
            "tail": self.tail(10),
            "dry_run": self.dry_run,
        }

    def stop(self) -> bool:
        """Stop the connector, escalating SIGTERM -> SIGKILL. Returns success."""
        self._stop_requested = True
        process = self.process
        if process is None:
            return True
        if process.poll() is not None:
            self.process = None
            return True
        try:
            self._signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._signal_group(process, signal.SIGKILL)
                process.wait(timeout=5)
        except Exception as exc:
            self.last_error = f"could not stop cloudflared: {exc}"
            return False
        finally:
            self.connected = False
            # The stdout pipe is closed by the drain thread once it sees EOF;
            # closing it here could block on the read lock.
            self.process = None
        return True

    @staticmethod
    def _signal_group(process: subprocess.Popen, sig: int) -> None:
        """Signal the connector's whole process group (falls back to the pid)."""
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except Exception:
            process.send_signal(sig)


def test_cloudflare_tunnel() -> int:
    """Manual smoke test: report what is configured, change nothing."""
    tunnel = CloudflareTunnel()
    print("Cloudflare tunnel configuration")
    print(f"  tunnel id      : {tunnel.tunnel_id}")
    print(f"  hostname       : {tunnel.hostname}")
    print(f"  local service  : http://127.0.0.1:{tunnel.local_port}")
    print(f"  token present  : {tunnel.has_token()}")
    print(f"  local answer   : {tunnel.verify_local_service()}")
    print(f"  cloudflared    : {tunnel.find_binary() or '(not installed yet)'}")
    if not tunnel.has_token():
        print("\nNo token: add the Kaggle Secret "
              f"{TOKEN_VAR} (attach it to the notebook) before starting.")
        return 0
    if not tunnel.start():
        print(f"\nstart() refused: {tunnel.last_error}")
        return 1
    result = tunnel.wait_for_connection(timeout=90)
    print(f"\nconnection: {'connected' if result['connected'] else 'NOT connected'}"
          f" ({result['reason']})")
    for line in result.get("tail", []):
        print("  |", line)
    tunnel.stop()
    return 0 if result["connected"] else 1


if __name__ == "__main__":
    raise SystemExit(test_cloudflare_tunnel())
