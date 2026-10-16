#!/usr/bin/env python3
"""
RaheeCraft — Canonical process and readiness detection
======================================================
One authoritative implementation of "is Paper actually running?", shared by
the action executor, the status reporter, and the Stage 4 file operations.

Why this module exists
----------------------
The previous implementation used `pgrep -f paper.jar`. That matches *any*
process whose command line merely mentions the substring "paper.jar" —
including an editor, a `grep`, the test runner, or even the shell that launched
the check. That produced false "server is running" readings, which silently
blocked world backups, version switches, and restores.

Detection here is deliberately strict. A process counts as *this* server only
when all of the following hold:
  1. It is not this process or one of its ancestors.
  2. Its executable is the `java` binary.
  3. It was launched with an actual `paper.jar` argument (argv element, not a
     substring anywhere in the command line).
  4. That jar argument resolves to the exact `paper.jar` inside the configured
     server directory.

Readiness is a separate question from liveness: a JVM can be up for a while
before Minecraft is accepting connections. `paper_startup_state()` reads Paper's
own log for the documented `Done (…)` line (ready) and for fatal startup
markers (crashed), so callers never confuse "java is alive" with "players can
join".
"""

import os
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

__all__ = [
    "BOOT_MARKER",
    "READY_MARKERS",
    "FATAL_MARKERS",
    "find_paper_pid",
    "find_playit_pid",
    "is_paper_running",
    "paper_startup_state",
    "iter_process_cmdlines",
    "ancestor_pids",
]

# Marker Paper writes once per boot. Used to scope "is it ready" to the
# current startup rather than to any earlier session in the same log file.
BOOT_MARKER = "Starting minecraft server version"

# Markers Paper writes when the server has finished booting and will accept
# connections. Kept here so every caller agrees on what "ready" means.
READY_MARKERS = ("Done (", 'For help, type "help"')

# Markers that indicate the server will never become ready in this process.
FATAL_MARKERS = (
    "Failed to start the minecraft server",
    "Encountered an unexpected exception",
    "UnsupportedClassVersionError",
    "Error: Could not find or load main class",
    "Unable to access jarfile",
    "You need to agree to the EULA",
    "Failed to load eula.txt",
)

# How much of the tail of the log to scan for readiness/failure markers.
_LOG_TAIL_BYTES = 256 * 1024


def _read_cmdline(pid: int) -> Optional[List[str]]:
    """Read argv for `pid` from /proc, or None if unavailable."""
    try:
        raw = (Path("/proc") / str(pid) / "cmdline").read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    parts = [p.decode("utf-8", "replace") for p in raw.split(b"\x00")]
    argv = [p for p in parts if p]
    return argv or None


def iter_process_cmdlines() -> Iterator[Tuple[int, List[str]]]:
    """Yield (pid, argv) for every process whose argv we can read.

    Prefers Linux /proc (Kaggle, most containers, Termux). Falls back to
    parsing `ps` where /proc is unavailable so the agent still works off Linux.
    """
    proc_dir = Path("/proc")
    if proc_dir.is_dir():
        for entry in proc_dir.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            argv = _read_cmdline(pid)
            if argv:
                yield pid, argv
        return

    # Fallback: ps gives one line per process; we cannot recover exact argv
    # quoting, so this is best-effort and only used when /proc is missing.
    import subprocess

    try:
        result = subprocess.run(
            ["ps", "-eo", "pid=,args="],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return
    if result.returncode != 0:
        return
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        head, _, rest = line.partition(" ")
        if not head.isdigit():
            continue
        yield int(head), rest.split()


def ancestor_pids() -> set:
    """PIDs of this process and all of its ancestors.

    Ancestors are excluded from detection so a shell that launched the check
    (and whose command line happens to mention a jar) can never be mistaken
    for the server.
    """
    pids = {os.getpid()}
    pid = os.getpid()
    # Walk /proc/<pid>/stat's ppid field up to init.
    for _ in range(64):
        try:
            stat = (Path("/proc") / str(pid) / "stat").read_text()
        except OSError:
            break
        # comm may contain spaces/parens; ppid is the field after the last ')'.
        try:
            after = stat[stat.rindex(")") + 2:].split()
            ppid = int(after[1])
        except (ValueError, IndexError):
            break
        if ppid <= 0 or ppid in pids:
            break
        pids.add(ppid)
        pid = ppid
    return pids


def _same_file(a: Path, b: Path) -> bool:
    """True when two paths denote the same file (falls back to lexical match)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        try:
            return a.resolve() == b.resolve()
        except OSError:
            return False


def _process_cwd(pid: int) -> Optional[Path]:
    """Working directory of `pid`, i.e. where a relative jar argument points."""
    try:
        return Path(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        return None


def _argv_targets_jar(argv: List[str], jar: Path,
                      cwd: Optional[Path] = None) -> bool:
    """Does this argv launch `jar` with java?

    Requires a `java`-like executable AND an argv element that names our jar.
    A jar argument may be absolute, or relative to the JVM's own working
    directory (Paper is started with cwd = server dir) — the process's cwd is
    preferred over an assumption, because guessing made an unrelated server's
    `-jar paper.jar` look like ours.
    """
    if not argv:
        return False
    exe = os.path.basename(argv[0]).lower()
    if exe not in ("java", "java.exe") and not exe.startswith("java"):
        return False

    for arg in argv[1:]:
        if not arg.lower().endswith(".jar"):
            continue
        candidate = Path(arg)
        if not candidate.is_absolute():
            candidate = (cwd or jar.parent) / candidate
        # The argument has to name a real file. Two paths that both do not
        # exist (a bare relative `paper.jar` plus a server directory with no
        # jar in it) compare equal, which used to report every other JVM as
        # this server — the false "running" reading this module exists to
        # prevent.
        if not candidate.exists():
            continue
        if _same_file(candidate, jar):
            return True
    return False


def find_paper_pid(server_dir: Any) -> Optional[int]:
    """PID of the JVM running `server_dir/paper.jar`, or None.

    Returns None whenever no process positively matches this server, so callers
    can trust a False result instead of guessing from a substring match.
    """
    jar = Path(server_dir) / "paper.jar"
    skip = ancestor_pids()
    for pid, argv in iter_process_cmdlines():
        if pid in skip:
            continue
        # Cheap pre-filter: only a process with a .jar argument can match.
        if not any(a and a.lower().endswith(".jar") for a in argv[1:]):
            continue
        if _argv_targets_jar(argv, jar, _process_cwd(pid)):
            return pid
    return None


def is_paper_running(server_dir: Any) -> bool:
    """True only when a matching Paper JVM is positively identified."""
    return find_paper_pid(server_dir) is not None


def find_playit_pid() -> Optional[int]:
    """PID of the Playit.gg connector process, or None.

    Matches on the executable name only — never on a bare substring — so a
    documentation file or a shell command mentioning "playit" is not mistaken
    for a live tunnel.
    """
    skip = ancestor_pids()
    for pid, argv in iter_process_cmdlines():
        if pid in skip or not argv:
            continue
        exe = os.path.basename(argv[0]).lower()
        if exe in ("playit", "playit-linux-amd64", "playitd") or exe.startswith("playit"):
            return pid
    return None


def _read_log_tail(log_path: Path) -> str:
    try:
        with open(log_path, "rb") as fh:
            try:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - _LOG_TAIL_BYTES))
            except OSError:
                fh.seek(0)
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def paper_startup_state(server_dir: Any,
                        log_path: Optional[Path] = None) -> Dict[str, Any]:
    """Classify Paper's startup from its own log.

    Returns one of:
      {"state": "ready",   "marker": <line>}         server accepts connections
      {"state": "failed",  "marker": <line>}         fatal startup error seen
      {"state": "loading", "marker": None}           alive, not ready yet
      {"state": "unknown", "marker": None}           no log to inspect

    This hides no uncertainty: an unreadable log is reported as `unknown`, not
    as `ready`.
    """
    if log_path is None:
        log_path = Path(server_dir) / "logs" / "latest.log"
    if not Path(log_path).exists():
        return {"state": "unknown", "marker": None}

    tail = _read_log_tail(Path(log_path))
    if not tail:
        return {"state": "unknown", "marker": None}

    # Only the most recent boot matters: `latest.log` is appended to across
    # restarts, so a "Done (…)" from a previous session must not be reported as
    # the current state. Trim to the last startup banner.
    last_boot = tail.rfind(BOOT_MARKER)
    if last_boot != -1:
        tail = tail[last_boot:]

    for line in reversed(tail.splitlines()):
        stripped = line.strip()
        if any(marker in stripped for marker in FATAL_MARKERS):
            return {"state": "failed", "marker": stripped}
        if any(marker in stripped for marker in READY_MARKERS):
            return {"state": "ready", "marker": stripped}
    return {"state": "loading", "marker": None}
