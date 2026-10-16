#!/usr/bin/env python3
"""
End-to-end test for the notebook's Paper download/verification cell.

The previous version of this file sliced raw JSON text out of the .ipynb and
tried to `compile()` it, built an environment dict it never applied, and used
names it never imported — so it could not run at all. This version parses the
notebook properly, pulls out the real cell source, and executes it in a
temporary server directory against a fake PaperMC API.

It never touches the real Minecraft world, the real server directory, or Google
Drive.
"""
import contextlib
import hashlib
import io
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

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOTEBOOK = PROJECT_ROOT / "minecraft_kaggle_setup.ipynb"
CELL_MARKER = "DOWNLOADING PAPER SERVER JAR"


class _FakePaperApiHandler(BaseHTTPRequestHandler):
    """Minimal stand-in for the PaperMC **v3** API, rooted at "/".

    `PAPER_API_BASE` is the API root, so paths arrive as
    /projects/paper/versions/<v>/builds and artifacts are served from
    /objects/<sha>/<name>. PaperMC retired the v2 API (HTTP 410), so the v3
    shape is what the notebook cell must actually speak.
    """

    server: HTTPServer = None

    def do_GET(self):
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        if parts[:2] == ["projects", "paper"] and len(parts) == 2:
            return self._json({"project": {"id": "paper"},
                               "versions": {"1.21": ["1.21.4"]}})
        if (len(parts) == 5 and parts[:2] == ["projects", "paper"]
                and parts[2] == "versions" and parts[4] == "builds"):
            return self._json(self._builds())
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

    def _builds(self):
        jar = self.server.fake_jar_path
        data = jar.read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        port = self.server.server_address[1]
        artifact = {
            "name": jar.name,
            "checksums": {"sha256": sha},
            "size": len(data),
            "url": f"http://127.0.0.1:{port}/objects/{sha}/{jar.name}",
        }
        return [{"id": 2, "channel": "STABLE", "downloads": {"server:default": artifact}}]

    def _serve_jar(self):
        data = self.server.fake_jar_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/java-archive")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def _start_fake_server(fake_jar_path: Path):
    server = HTTPServer(("127.0.0.1", 0), _FakePaperApiHandler)
    server.fake_jar_path = fake_jar_path
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def _paper_download_cell_source() -> str:
    """Return the real source of the notebook's Paper download cell."""
    nb = json.loads(NOTEBOOK.read_text())
    for cell in nb["cells"]:
        if cell.get("cell_type") != "code":
            continue
        source = "".join(cell.get("source", []))
        if CELL_MARKER in source:
            return source
    raise AssertionError(
        f"no code cell in {NOTEBOOK.name} contains the marker {CELL_MARKER!r}")


class TestNotebookCellDownload(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="notebook-cell-"))
        self.server_dir = self.root / "minecraft_server"
        self.server_dir.mkdir()
        self.fake_jar = self.root / "paper-1.21.4.jar"
        with zipfile.ZipFile(self.fake_jar, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("paper.yml", "# fake paper.yml\n")
            zf.writestr("version.json", json.dumps({"version": "1.21.4"}))

        self.cell = compile(_paper_download_cell_source(), "<notebook-cell>", "exec")
        self.server, self.port = _start_fake_server(self.fake_jar)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        shutil.rmtree(self.root, ignore_errors=True)

    def _env(self):
        return {
            "MINECRAFT_VERSION": "1.21.4",
            "SERVER_DIR": str(self.server_dir),
            "PAPER_API_BASE": f"http://127.0.0.1:{self.port}",
            # The fixture JAR is intentionally tiny.
            "PAPER_JAR_MIN_BYTES": "1",
        }

    def _run_cell(self) -> str:
        buffer = io.StringIO()
        with patch.dict(os.environ, self._env(), clear=False):
            with contextlib.redirect_stdout(buffer):
                exec(self.cell, {"__name__": "__notebook_cell__"})
        return buffer.getvalue()

    def test_notebook_cell_creates_jar(self):
        output = self._run_cell()
        dest = self.server_dir / "paper.jar"
        self.assertTrue(dest.exists(), output)
        self.assertEqual(dest.read_bytes(), self.fake_jar.read_bytes())
        self.assertIn("downloaded and verified", output)

    def test_notebook_cell_skips_when_jar_exists(self):
        existing = self.server_dir / "paper.jar"
        with zipfile.ZipFile(existing, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("paper.yml", "# existing, must not be replaced\n")
        before = existing.read_bytes()

        output = self._run_cell()
        self.assertTrue(existing.exists())
        self.assertEqual(existing.read_bytes(), before)
        self.assertIn("Keeping the existing paper.jar", output)


if __name__ == "__main__":
    unittest.main()
