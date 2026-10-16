#!/usr/bin/env python3
"""
RaheeCraft — safe Paper JAR download helper
===========================================
Used by:
- the Kaggle notebook cell that downloads and verifies the Paper server JAR
- the automated tests, which point it at a local fake PaperMC API

Design notes
------------
- Version and API base come from the environment so the notebook and the
  backend stay in sync without hardcoding either value in the cell.
- Only the official PaperMC API is used; no mirrors, no third-party hosts.

API version
-----------
The PaperMC **v2** API (`api.papermc.io/v2`) has been retired and now answers
`HTTP 410 {"error":"sunset"}` for every request, so a v2 client can never
download anything. This module talks to the current **v3** API
(`fill.papermc.io/v3`), whose builds endpoint returns a newest-first list of
builds and, for each build, the artifact's own file name, byte size, SHA-256,
and download URL.
- The build metadata (build number, size, SHA-256) comes from the same official
  API response that names the artifact, so the checksum we verify is the one the
  publisher advertised — not one we invented.
- The download goes to a temporary file in the destination directory, is
  verified (HTTP status, announced size, byte limit, ZIP integrity, SHA-256),
  and only then is atomically moved into place with `os.replace`.
- An existing `paper.jar` is never replaced without `force=True`.
- Every exit path cleans up its temporary file.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

__all__ = [
    "paper_api_base",
    "minecraft_version",
    "server_dir",
    "paper_jar_path",
    "download_paper_jar",
    "resolve_paper_build",
    "max_paper_jar_bytes",
    "min_paper_jar_bytes",
]

# Sanity limits. A real Paper JAR is tens of megabytes; these bounds stop a
# hostile or misconfigured endpoint from filling the disk, and stop a stub
# response from being installed as `paper.jar`. Read live from the environment
# so an operator (or a test with a tiny fixture) can adjust them per run.
DEFAULT_MAX_PAPER_JAR_BYTES = 200 * 1024 * 1024
DEFAULT_MIN_PAPER_JAR_BYTES = 1024


def max_paper_jar_bytes() -> int:
    return int(os.environ.get("PAPER_JAR_MAX_BYTES", str(DEFAULT_MAX_PAPER_JAR_BYTES)))


def min_paper_jar_bytes() -> int:
    return int(os.environ.get("PAPER_JAR_MIN_BYTES", str(DEFAULT_MIN_PAPER_JAR_BYTES)))

# Build/version identifiers that come from configuration. Restricting them keeps
# a bad config value from being spliced into the request URL.
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")

_UA = "RaheeCraft-Stage4"


DEFAULT_PAPER_API_BASE = "https://fill.papermc.io/v3"

# Hosts allowed to serve the actual JAR. The v3 API hands out
# `fill-data.papermc.io` object URLs; we refuse anything else.
OFFICIAL_ARTIFACT_HOSTS = frozenset({"fill-data.papermc.io", "api.papermc.io"})


def paper_api_base() -> str:
    """Official PaperMC API base URL (read live so tests can override it).

    Defaults to the current v3 API; the retired v2 endpoint answers HTTP 410.
    """
    return os.environ.get("PAPER_API_BASE", DEFAULT_PAPER_API_BASE).rstrip("/")


def minecraft_version() -> str:
    """Configured Minecraft version for the Paper build."""
    return os.environ.get("MINECRAFT_VERSION", "1.21.4").strip() or "1.21.4"


def server_dir() -> Path:
    """Minecraft server directory (where paper.jar lives)."""
    raw = os.environ.get("SERVER_DIR", "/kaggle/working/minecraft_server")
    return Path(raw).expanduser().resolve()


def paper_jar_path() -> Path:
    return server_dir() / "paper.jar"


def _json_get(url: str, timeout: int = 20):
    """GET `url` and decode the JSON body, closing any error response."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                raise RuntimeError(f"unexpected HTTP status {status} from the Paper API")
            payload = resp.read()
    except urllib.error.HTTPError as e:
        # HTTPError is also a file-like response; close it or the socket stays
        # open until garbage collection.
        code = e.code
        e.close()
        raise RuntimeError(f"Paper API returned HTTP {code} for {url}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Paper API unreachable: {e.reason}") from e

    try:
        return json.loads(payload.decode("utf-8", errors="replace"))
    except (ValueError, AttributeError) as e:
        raise RuntimeError(f"Paper API returned invalid JSON: {e}") from e


def resolve_paper_build(version: str) -> dict:
    """Resolve the newest usable official build for `version`.

    The v3 builds endpoint returns a newest-first list. We prefer the newest
    STABLE build and fall back to the newest listed build for versions that have
    no stable release yet. The artifact URL, file name, size, and SHA-256 all
    come from that same official response.

    Returns {url, build, channel, filename, sha256, size}.
    Raises RuntimeError on bad input, HTTP failure, or malformed metadata.
    """
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise RuntimeError(f"refusing to request a malformed version: {version!r}")

    builds = _json_get(f"{paper_api_base()}/projects/paper/versions/{version}/builds")
    if not isinstance(builds, list) or not builds:
        raise RuntimeError(f"no builds found for Paper version {version}")

    stable = [b for b in builds
              if isinstance(b, dict) and str(b.get("channel", "")).upper() == "STABLE"]
    chosen = (stable or builds)[0]
    if not isinstance(chosen, dict):
        raise RuntimeError(f"malformed build metadata for Paper version {version}")

    build_num = chosen.get("id")
    if not isinstance(build_num, int) or build_num <= 0:
        raise RuntimeError(f"no valid build number found for Paper version {version}")

    downloads = chosen.get("downloads")
    artifact = None
    if isinstance(downloads, dict):
        candidate = downloads.get("server:default")
        if isinstance(candidate, dict):
            artifact = candidate
        else:
            artifact = next((v for v in downloads.values() if isinstance(v, dict)), None)
    if not isinstance(artifact, dict) or not artifact.get("url"):
        raise RuntimeError(f"the Paper API returned no download for build {build_num}")

    url = str(artifact["url"])
    api_host = urllib.parse.urlparse(paper_api_base()).hostname or ""
    artifact_host = (urllib.parse.urlparse(url).hostname or "").lower()
    if artifact_host not in OFFICIAL_ARTIFACT_HOSTS and artifact_host != api_host.lower():
        raise RuntimeError(f"refusing to download from a non-official host: {artifact_host}")

    # Trusted metadata the publisher advertises for this exact artifact.
    checksums = artifact.get("checksums") or {}
    expected_sha = checksums.get("sha256") if isinstance(checksums, dict) else None
    if not (isinstance(expected_sha, str) and re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha)):
        expected_sha = None
    expected_size = artifact.get("size")
    if not isinstance(expected_size, int) or expected_size <= 0:
        expected_size = None

    return {
        "url": url,
        "build": build_num,
        "channel": chosen.get("channel"),
        "filename": artifact.get("name"),
        "sha256": expected_sha,
        "size": expected_size,
    }


def _looks_like_jar(head: bytes) -> bool:
    """A JAR is a ZIP archive; require the local-file or empty-archive header."""
    return head[:2] == b"PK"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _verify_jar(tmp_path: Path, announced_size: Optional[int]) -> int:
    """Validate the downloaded artifact. Returns its byte size.

    Raises RuntimeError describing exactly what failed.
    """
    file_size = tmp_path.stat().st_size

    if announced_size is not None and file_size != announced_size:
        raise RuntimeError(
            f"truncated download: announced {announced_size} bytes, received {file_size}"
        )
    if file_size > max_paper_jar_bytes():
        raise RuntimeError(f"download exceeds the size limit ({max_paper_jar_bytes()} bytes)")
    if file_size < min_paper_jar_bytes():
        raise RuntimeError(
            f"downloaded file is too small to be a valid Paper JAR "
            f"({file_size} bytes, minimum {min_paper_jar_bytes()})")

    head = tmp_path.read_bytes()[:4]
    if not _looks_like_jar(head):
        raise RuntimeError("downloaded file does not look like a JAR/ZIP archive")

    try:
        with zipfile.ZipFile(tmp_path) as zf:
            bad = zf.testzip()
    except zipfile.BadZipFile as e:
        raise RuntimeError(f"downloaded JAR is not a valid ZIP archive: {e}") from e
    if bad:
        raise RuntimeError(f"downloaded JAR failed ZIP integrity check at {bad}")

    return file_size


def download_paper_jar(*, force: bool = False) -> dict:
    """Download the configured Paper build and place it as `paper.jar`.

    Never overwrites an existing `paper.jar` unless `force=True`. Returns a dict
    with `state` in {"success", "skipped", "failed"}; failures carry an `error`
    and leave no partial artifact behind.
    """
    version = minecraft_version()
    dest = paper_jar_path()
    directory = server_dir()
    directory.mkdir(parents=True, exist_ok=True)

    if dest.exists() and not force:
        return {
            "state": "skipped",
            "error": None,
            "result": {
                "message": "paper.jar already exists; delete or move it first to replace it",
                "existing": str(dest.relative_to(directory.parent)),
            },
        }

    try:
        info = resolve_paper_build(version)
    except Exception as e:
        return {"state": "failed", "error": str(e), "result": {}}

    tmp_path: Optional[Path] = None
    try:
        fd, tmp_name = tempfile.mkstemp(prefix=".paper-download-", suffix=".jar", dir=directory)
        os.close(fd)
        tmp_path = Path(tmp_name)

        req = urllib.request.Request(info["url"], headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=180) as resp:
            status = getattr(resp, "status", 200)
            if status != 200:
                raise RuntimeError(f"unexpected HTTP status {status} for the Paper artifact")
            declared = resp.headers.get("Content-Length")
            announced = int(declared) if declared and declared.isdigit() else None

            downloaded = 0
            with open(tmp_path, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > max_paper_jar_bytes():
                        raise RuntimeError(
                            f"download exceeded the size limit ({max_paper_jar_bytes()} bytes)")
                    f.write(chunk)

        # Prefer the size the API advertised for this artifact; fall back to the
        # HTTP header. Either way a mismatch means a truncated transfer.
        expected_size = info["size"] or announced
        file_size = _verify_jar(tmp_path, expected_size)

        sha = _sha256(tmp_path)
        if info["sha256"] and sha.lower() != info["sha256"].lower():
            raise RuntimeError(
                "checksum mismatch: downloaded JAR does not match the SHA-256 "
                "published by the PaperMC API"
            )

        # Atomic install: os.replace never leaves a window with no paper.jar.
        os.replace(str(tmp_path), str(dest))
        tmp_path = None

        return {
            "state": "success",
            "error": None,
            "result": {
                "version": version,
                "build": info["build"],
                "channel": info.get("channel"),
                "filename": info.get("filename"),
                "download_url": info["url"],
                "path": str(dest.relative_to(directory.parent)),
                "size_bytes": file_size,
                "sha256": sha,
                "checksum_verified": bool(info["sha256"]),
            },
        }
    except urllib.error.HTTPError as e:
        e.close()
        return {"state": "failed",
                "error": f"download failed: HTTP {e.code}", "result": {}}
    except urllib.error.URLError as e:
        return {"state": "failed",
                "error": f"download failed: {e.reason}", "result": {}}
    except Exception as e:
        return {"state": "failed", "error": str(e), "result": {}}
    finally:
        if tmp_path is not None and tmp_path.exists():
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
