#!/usr/bin/env python3
"""
Run the Cloudflare connector for the RaheeCraft panel on *this* machine.

Why this exists
---------------
`cloudflare_tunnel.py` holds the connector itself: it reads the tunnel token
from the environment, drains the connector's output, and only reports success
after Cloudflare says the connection registered. The Kaggle notebook cell calls
it in the kernel.

This entry point is the same connector for a panel running here (a phone, a VPS,
the dev box), and it adds the one host-specific piece the module deliberately
does not know about: getting a `resolv.conf` to a Go binary.

On Android/Termux `/etc` is a read-only symlink to `/system/etc` with no
`resolv.conf` in it, so `cloudflared` (Go) falls back to 127.0.0.1:53 and every
DNS lookup fails with "connection refused". `proot` can present a real
`resolv.conf` to the guest without root, so when this script detects that
situation it launches the connector through `proot` by setting
`CLOUDFLARED_LAUNCHER`. On a normal Linux host nothing is set and the connector
runs directly.

The tunnel token comes from `CLOUDFLARE_TUNNEL_TOKEN` (exported, or loaded from
`.env`), or from a token file, and is only ever handed to the connector through
the environment — never as a command-line argument, which `ps` would show.

Exit codes: 0 on a clean stop, 1 if the connector refused to start, 2 if no
token was configured.
"""

import argparse
import os
import platform
import shutil
import signal
import sys
import time
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from cloudflare_tunnel import CloudflareTunnel, TOKEN_VAR          # noqa: E402
from run_backend import load_env_file                              # noqa: E402

# Where a token file lives when it is not exported. Kept outside the repository.
DEFAULT_TOKEN_FILE = Path.home() / ".cloudflare-tunnel-token"

# Nameservers used only if the host has no usable resolv.conf of its own.
FALLBACK_RESOLV = "nameserver 1.1.1.1\nnameserver 8.8.8.8\nnameserver 1.0.0.1\n"

# How long to wait for the connector to register, and how long to wait between
# restarts if it dies (a phone or a kernel can kill a long-lived process).
CONNECT_TIMEOUT_SECONDS = 90
RESTART_DELAYS = (5, 15, 30, 60)


def token_from_file(path: Path) -> str:
    """Read a token file (one value, whitespace trimmed). Never prints it."""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def resolve_token(explicit_file: str = None) -> tuple:
    """Return ``(token, source)`` from the environment or a token file."""
    token = (os.environ.get(TOKEN_VAR, "") or "").strip()
    if token:
        return token, f"environment {TOKEN_VAR}"
    candidates = [Path(explicit_file)] if explicit_file else []
    candidates.append(DEFAULT_TOKEN_FILE)
    for path in candidates:
        value = token_from_file(path)
        if value:
            return value, str(path)
    return "", ""


def resolv_conf_is_usable() -> bool:
    """Can a Go binary find nameservers the normal way here?"""
    path = Path("/etc/resolv.conf")
    try:
        return path.is_file() and "nameserver" in path.read_text(errors="ignore")
    except OSError:
        return False


def detect_launcher() -> tuple:
    """Return ``(launcher, reason)`` for this host, empty when none is needed.

    Only a host that hides ``/etc/resolv.conf`` needs the workaround, so a
    normal machine gets no wrapper at all rather than a fragile one.
    """
    if resolv_conf_is_usable():
        return "", "host /etc/resolv.conf is readable, running cloudflared directly"
    proot = shutil.which("proot")
    if proot is None:
        return "", ("no readable /etc/resolv.conf and no proot: cloudflared "
                    "cannot resolve DNS on this host")
    prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    prefix_conf = Path(prefix) / "etc/resolv.conf"
    if prefix_conf.is_file() and "nameserver" in prefix_conf.read_text(errors="ignore"):
        source, reason = prefix_conf, f"mounting {prefix_conf} as /etc/resolv.conf via proot"
    else:
        source = Path.home() / ".rahee-bin" / "resolv.conf"
        source.parent.mkdir(parents=True, exist_ok=True)
        if not source.is_file() or "nameserver" not in source.read_text(errors="ignore"):
            source.write_text(FALLBACK_RESOLV, encoding="utf-8")
        reason = f"no host resolv.conf: wrote {source} and mount it via proot"
    return f'{proot} -b "{source}:/system/etc/resolv.conf" -r /', reason


def report(tunnel: CloudflareTunnel) -> None:
    """Print the honest picture: connector, local service, public hostname."""
    status = tunnel.status()
    print(f"  connector     : pid {status['pid']}, running={status['running']}, "
          f"connected={status['connected']}, connections={status['connections']}")
    print(f"  tunnel id     : {status['tunnel_id']}")
    print(f"  hostname      : {status['hostname']}")
    print(f"  local service : {status['local_service']} -> "
          f"{'OK' if status['local_service_ok'] else 'NOT answering'}")
    if status["launcher"]:
        print(f"  launcher      : {status['launcher']}")
    if status["last_error"]:
        print(f"  last error    : {status['last_error']}")
    if not tunnel.verify_local_service():
        public = None
    else:
        public = _public_check(tunnel)
    print(f"  public check  : {public['url'] if public else '(skipped)'} -> "
          f"{'OK' if public and public['ok'] else 'not serving'}")
    if public and not public["ok"] and public.get("detail"):
        print(f"                  {public['detail']}")


def _public_check(tunnel: CloudflareTunnel, attempts: int = 4,
                  delay: int = 8) -> dict:
    """Ask the public hostname, retrying while the edge catches up.

    A connector that registered seconds ago is not routable everywhere yet, so
    the first request can still return Cloudflare's 530. Retrying for a short
    while turns that into an accurate answer instead of a false alarm, and a
    hostname that really is broken still fails after the last attempt.
    """
    result = tunnel.verify_public_service()
    for _ in range(max(0, attempts - 1)):
        if result["ok"]:
            break
        time.sleep(delay)
        result = tunnel.verify_public_service()
    return result


def _sleep_until(seconds: int, stop_flag: dict) -> None:
    """Sleep in one-second slices so Ctrl-C stays responsive."""
    for _ in range(seconds):
        if stop_flag["stop"]:
            return
        time.sleep(1)


def supervise(tunnel: CloudflareTunnel, stop_flag: dict) -> int:
    """Keep the connector up until asked to stop; report every transition."""
    attempt = 0
    while not stop_flag["stop"]:
        if not tunnel.start():
            print(f"start refused: {tunnel.last_error}")
            return 1
        result = tunnel.wait_for_connection(timeout=CONNECT_TIMEOUT_SECONDS)
        if result["connected"]:
            attempt = 0
            print(f"CONNECTED: {result['reason']}")
            report(tunnel)
            print("Running in the foreground; Ctrl-C (or SIGTERM) stops the connector.")
        else:
            print(f"NOT connected: {result['reason']}")
            for line in result.get("tail", []):
                print("  |", line)
            tunnel.stop()

        # Idle until the connector exits or the operator stops us. A connector
        # that died right after registering is noticed here, not silently kept
        # on screen as "connected".
        while not stop_flag["stop"]:
            if tunnel.process is None or tunnel.process.poll() is not None:
                break
            time.sleep(2)
        if stop_flag["stop"]:
            break

        print("Connector exited; restarting it.")
        tunnel.stop()
        delay = RESTART_DELAYS[min(attempt, len(RESTART_DELAYS) - 1)]
        attempt += 1
        print(f"Retrying in {delay}s (attempt {attempt}).")
        _sleep_until(delay, stop_flag)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--token-file", default=None,
                        help=f"file holding the tunnel token (default: {DEFAULT_TOKEN_FILE})")
    parser.add_argument("--port", type=int, default=8080,
                        help="local panel port (default: 8080)")
    parser.add_argument("--hostname", default=None,
                        help="public hostname to verify (default: the module's)")
    parser.add_argument("--no-launcher", action="store_true",
                        help="do not auto-detect the proot wrapper")
    parser.add_argument("--check", action="store_true",
                        help="report configuration and exit; start nothing")
    args = parser.parse_args(argv)

    # Keep the log useful when this runs detached (`nohup ... >> log`): Python
    # block-buffers stdout when it is not a terminal.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    loaded = load_env_file()
    token, source = resolve_token(args.token_file)
    launcher, launcher_reason = "", "disabled by --no-launcher"
    if not args.no_launcher:
        launcher, launcher_reason = detect_launcher()
    if launcher:
        os.environ["CLOUDFLARED_LAUNCHER"] = launcher
    else:
        os.environ.pop("CLOUDFLARED_LAUNCHER", None)

    tunnel = CloudflareTunnel(local_port=args.port, hostname=args.hostname,
                              token=token or None)
    print("RaheeCraft panel tunnel")
    print(f"  .env settings : {loaded} loaded")
    print(f"  token         : {'present from ' + source if token else 'MISSING'}")
    print(f"  binary        : {tunnel.find_binary() or '(not installed; would download)'}")
    print(f"  python        : {platform.python_version()} on {platform.system()}")
    print(f"  launcher      : {launcher or '(none)'}")
    print(f"  why           : {launcher_reason}")

    if args.check:
        print(f"  local service : http://127.0.0.1:{args.port} -> "
              f"{'OK' if tunnel.verify_local_service() else 'NOT answering'}")
        public = tunnel.verify_public_service()  # point-in-time: nothing is started yet
        print(f"  public check  : {public['url']} -> "
              f"{'OK' if public['ok'] else 'not serving yet'}")
        return 0 if token else 2

    if not token:
        print(f"\nNo tunnel token. Either export {TOKEN_VAR}, or write the token "
              f"to {DEFAULT_TOKEN_FILE} (chmod 600), then run this script again.")
        return 2

    if not tunnel.verify_local_service():
        print(f"\nWarning: nothing answers on http://127.0.0.1:{args.port}. Start "
              "the panel first (`python3 run_backend.py`); the connector starts "
              "anyway and serves the panel as soon as it is up.")

    stop_flag = {"stop": False}

    def _stop(signum, _frame):
        print(f"\nSignal {signum}: stopping the connector.")
        stop_flag["stop"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    code = supervise(tunnel, stop_flag)
    tunnel.stop()
    print("Connector stopped.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
