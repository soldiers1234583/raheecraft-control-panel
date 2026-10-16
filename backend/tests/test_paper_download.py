#!/usr/bin/env python3
"""
RaheeCraft — Paper JAR download / verify / atomic-install tests

Covers `backend/paper_download.py`, which the Kaggle notebook calls to install
`paper.jar`. The tests run against a local fake PaperMC **v3** API server:
nothing reaches fill.papermc.io, and no real Minecraft world is modified.

The v3 API is what matters here — PaperMC retired v2 (`api.papermc.io/v2` now
answers `HTTP 410 {"error":"sunset"}`), so a v2-shaped client can never download
anything.
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

import backend.paths as PATHS
import backend.paper_download as DL


def _make_fake_paper_jar(path: Path, extra_bytes: int = 0) -> Path:
    """Create a minimal valid ZIP/JAR."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("paper.yml", "# fake paper.yml\n")
        zf.writestr("version.json", json.dumps({"version": "1.21.4"}))
        if extra_bytes:
            zf.writestr("padding.bin", b"\x00" * extra_bytes)
    return path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class _FakeV3ApiHandler(BaseHTTPRequestHandler):
    """Minimal stand-in for the PaperMC v3 API, rooted at "/".

    GET /projects/paper
        -> {"project": {...}, "versions": {"1.21": ["1.21.4"]}}
    GET /projects/paper/versions/<v>/builds
        -> newest-first list of builds, each carrying the artifact URL,
           byte size, SHA-256, and file name.
    GET /objects/<sha>/<name>
        -> the fake JAR bytes.
    """

    server: HTTPServer = None

    def do_GET(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]

        if parts[:2] == ["projects", "paper"] and len(parts) == 2:
            return self._json({"project": {"id": "paper", "name": "Paper"},
                               "versions": {"1.21": ["1.21.4"]}})
        if (len(parts) == 5 and parts[:2] == ["projects", "paper"]
                and parts[2] == "versions" and parts[4] == "builds"):
            return self._json(self._builds(parts[3]))
        if parts and parts[0] == "objects":
            return self._serve_jar()

        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"not found")

    def _json(self, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _builds(self, version):
        jar = Path(self.server.fake_jar_path)
        data = jar.read_bytes()
        port = self.server.server_address[1]
        artifact = {
            "name": jar.name,
            "checksums": {"sha256": _sha256(jar)},
            "size": len(data),
            "url": f"http://127.0.0.1:{port}/objects/{_sha256(jar)}/{jar.name}",
        }
        # Newest first, matching the real API.
        return [
            {"id": 2, "channel": "STABLE", "time": "2025-06-09T10:18:55.778Z",
             "downloads": {"server:default": artifact}},
            {"id": 1, "channel": "ALPHA", "time": "2024-12-05T16:48:57.969Z",
             "downloads": {"server:default": artifact}},
        ]

    def _serve_jar(self):
        data = Path(self.server.fake_jar_path).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/java-archive")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class _FakeBadApiHandler(BaseHTTPRequestHandler):
    """Always 500 — simulates an unreachable / failing API."""

    def do_GET(self):
        self.send_response(500)
        self.end_headers()
        self.wfile.write(b"unavailable")

    def log_message(self, *args):
        pass


class _FakeTruncatedApiHandler(BaseHTTPRequestHandler):
    """Announces a huge artifact but sends a tiny body."""

    def do_GET(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if parts and parts[0] == "objects":
            self.send_response(200)
            self.send_header("Content-Length", "999999999")
            self.end_headers()
            self.wfile.write(b"too small")
            return
        port = self.server.server_address[1]
        self.send_response(200)
        body = json.dumps([{
            "id": 777, "channel": "STABLE",
            "downloads": {"server:default": {
                "name": "paper-1.21.4-777.jar",
                "checksums": {"sha256": "0" * 64},
                "size": 999999999,
                "url": f"http://127.0.0.1:{port}/objects/{'0'*64}/paper-1.21.4-777.jar",
            }},
        }]).encode("utf-8")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _FakeForeignHostApiHandler(_FakeV3ApiHandler):
    """Serves valid metadata but points the artifact at a third-party host."""

    def _builds(self, version):
        builds = super()._builds(version)
        for b in builds:
            b["downloads"]["server:default"]["url"] = "http://evil.example.com/paper.jar"
        return builds


class _FakeWrongChecksumApiHandler(_FakeV3ApiHandler):
    """Valid, correctly-sized artifact whose advertised SHA-256 is wrong.

    Isolates the checksum branch from the size and ZIP-integrity branches.
    """

    def _builds(self, version):
        builds = super()._builds(version)
        for b in builds:
            b["downloads"]["server:default"]["checksums"]["sha256"] = "1" * 64
        return builds


def _start_fake_server(handler_class, fake_jar_path: Path = None):
    server = HTTPServer(("127.0.0.1", 0), handler_class)
    server.fake_jar_path = fake_jar_path
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


class TestPaperApiConfiguration(unittest.TestCase):
    def test_api_base_is_the_current_v3_endpoint(self):
        base = PATHS.paper_api_base()
        # PaperMC retired v2; a v2 base means the download can never succeed.
        self.assertNotIn("/v2", base, f"still pointing at the sunset v2 API: {base}")
        self.assertIn("papermc.io", base)
        self.assertTrue(base.startswith("https://"))

    def test_default_paper_version_matches_config(self):
        self.assertEqual(PATHS.default_paper_version(), "1.21.4")


class TestPaperDownloadLocalLogic(unittest.TestCase):
    """Safe download / verify / atomic-install behavior against a fake API."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="paper-dl-"))
        self.server_dir = self.root / "minecraft_server"
        self.server_dir.mkdir()
        self.jars_dir = self.root / "jars"
        self.jars_dir.mkdir()
        self.fake_jar = self.jars_dir / "paper-1.21.4-2.jar"
        _make_fake_paper_jar(self.fake_jar, extra_bytes=256)
        self.servers = []

    def tearDown(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _start(self, handler, jar=None):
        server, port = _start_fake_server(handler, jar if jar is not None else self.fake_jar)
        self.servers.append(server)
        return f"http://127.0.0.1:{port}"

    def _env(self, *, version="1.21.4", base):
        return {
            "MINECRAFT_VERSION": version,
            "SERVER_DIR": str(self.server_dir),
            "PAPER_API_BASE": base,
            # The fixture JAR is deliberately tiny.
            "PAPER_JAR_MIN_BYTES": "1",
        }

    def _run(self, base, **kwargs):
        with patch.dict(os.environ, self._env(base=base), clear=False):
            return DL.download_paper_jar(**kwargs)

    # --- happy path ---------------------------------------------------------

    def test_download_creates_jar_and_moves_atomically(self):
        base = self._start(_FakeV3ApiHandler)
        res = self._run(base)
        self.assertEqual(res["state"], "success", res.get("error"))
        dest = self.server_dir / "paper.jar"
        self.assertTrue(dest.exists())
        self.assertEqual(dest.read_bytes(), self.fake_jar.read_bytes())
        self.assertEqual(res["result"]["size_bytes"], self.fake_jar.stat().st_size)
        self.assertEqual(res["result"]["sha256"], _sha256(self.fake_jar))
        # The API advertised a checksum, so it must have been verified.
        self.assertTrue(res["result"]["checksum_verified"])
        self.assertEqual(res["result"]["build"], 2)
        self.assertEqual(res["result"]["channel"], "STABLE")
        # No staging files left behind.
        self.assertEqual(list(self.server_dir.glob(".paper-download-*")), [])

    def test_resolve_prefers_the_newest_stable_build(self):
        base = self._start(_FakeV3ApiHandler)
        with patch.dict(os.environ, self._env(base=base), clear=False):
            info = DL.resolve_paper_build("1.21.4")
        self.assertEqual(info["build"], 2)
        self.assertEqual(info["channel"], "STABLE")
        self.assertTrue(info["url"].startswith("http://127.0.0.1:"))
        self.assertIsNotNone(info["sha256"])

    # --- overwrite protection ----------------------------------------------

    def test_existing_jar_is_not_overwritten(self):
        existing = self.server_dir / "paper.jar"
        _make_fake_paper_jar(existing)
        before = existing.read_bytes()
        base = self._start(_FakeV3ApiHandler)
        res = self._run(base)
        self.assertEqual(res["state"], "skipped")
        self.assertEqual(existing.read_bytes(), before)

    def test_download_overwrites_when_forced(self):
        existing = self.server_dir / "paper.jar"
        _make_fake_paper_jar(existing)
        other = self.jars_dir / "other.jar"
        _make_fake_paper_jar(other, extra_bytes=123)
        base = self._start(_FakeV3ApiHandler, other)
        res = self._run(base, force=True)
        self.assertEqual(res["state"], "success", res.get("error"))
        self.assertEqual((self.server_dir / "paper.jar").read_bytes(), other.read_bytes())

    # --- failure handling ---------------------------------------------------

    def test_download_rejects_invalid_zip(self):
        bad = self.jars_dir / "bad.jar"
        bad.write_bytes(b"NOT_A_ZIP!!")
        base = self._start(_FakeV3ApiHandler, bad)
        res = self._run(base)
        self.assertEqual(res["state"], "failed")
        self.assertFalse((self.server_dir / "paper.jar").exists())
        self.assertEqual(list(self.server_dir.glob(".paper-download-*")), [])

    def test_download_rejects_when_api_unreachable(self):
        base = self._start(_FakeBadApiHandler)
        res = self._run(base)
        self.assertEqual(res["state"], "failed")
        self.assertIn("500", res["error"])
        self.assertFalse((self.server_dir / "paper.jar").exists())

    def test_download_rejects_truncated_body(self):
        """A tiny body with a huge announced size must be refused, not installed."""
        base = self._start(_FakeTruncatedApiHandler)
        res = self._run(base)
        self.assertEqual(res["state"], "failed")
        self.assertIn("announced", res["error"])
        self.assertFalse((self.server_dir / "paper.jar").exists())
        self.assertEqual(list(self.server_dir.glob(".paper-download-*")), [])

    def test_download_rejects_tampered_checksum(self):
        """A body that does not match the advertised SHA-256 must be refused."""
        base = self._start(_FakeWrongChecksumApiHandler)
        res = self._run(base)
        self.assertEqual(res["state"], "failed", res)
        self.assertIn("checksum mismatch", res["error"])
        self.assertFalse((self.server_dir / "paper.jar").exists())
        self.assertEqual(list(self.server_dir.glob(".paper-download-*")), [])

    def test_resolve_rejects_non_official_artifact_host(self):
        base = self._start(_FakeForeignHostApiHandler)
        with patch.dict(os.environ, self._env(base=base), clear=False):
            with self.assertRaises(RuntimeError) as cm:
                DL.resolve_paper_build("1.21.4")
        self.assertIn("non-official host", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
