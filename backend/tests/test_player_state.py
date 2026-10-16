#!/usr/bin/env python3
"""
Player-state persistence and correct player counting.

Two independent failures are pinned here, both of which stopped players from
"returning to where they left" a Kaggle session:

  * The Drive backup zipped only the three world folders. Op list, whitelist,
    bans and the name cache live in the *server root*, so they were never
    uploaded and every new (ephemeral) session forgot them.
  * `_worlds_unchanged` compared an aware Drive timestamp (`...Z`) against a
    naive local file mtime, which raises TypeError. The periodic backup catches
    that error and moves on, so after the first backup the world was never
    uploaded again.

Also pinned: the online player count must come from the tracker's real
join/leave state, never from counting every "joined the game" line in a log
that spans the whole session.
"""

import os
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))

os.environ.setdefault("SERVER_DIR", "/kaggle/working/minecraft_server")

import drive_backup  # noqa: E402
from drive_backup import GoogleDriveBackup, SERVER_STATE_FILES  # noqa: E402
from agent.player_tracker import PlayerTracker  # noqa: E402
from agent import status as agent_status  # noqa: E402

UUID = "069a79f4-44e9-4726-a5be-fca90e38aaf5"
OLD_MTIME = 1_600_000_000      # 2020-09-13
NEW_MTIME = 1_800_000_000      # 2027-01-15
BACKUP_EPOCH = 1_700_000_000   # 2023-11-14


def _make_server(root: str) -> Path:
    """A throwaway server tree with worlds, player data and root state files."""
    root = Path(root)
    (root / "world" / "playerdata").mkdir(parents=True)
    (root / "world" / "stats").mkdir(parents=True)
    (root / "world" / "region").mkdir(parents=True)
    (root / "world_nether" / "region").mkdir(parents=True)
    (root / "world_the_end" / "region").mkdir(parents=True)

    (root / "world" / "level.dat").write_bytes(b"LEVEL")
    (root / "world" / "playerdata" / f"{UUID}.dat").write_bytes(b"INVENTORY")
    (root / "world" / "stats" / f"{UUID}.json").write_text("{}")
    (root / "world" / "region" / "r.0.0.mca").write_bytes(b"R")
    (root / "world_nether" / "region" / "r.0.0.mca").write_bytes(b"N")
    (root / "world_the_end" / "region" / "r.0.0.mca").write_bytes(b"E")

    # Root state files (banned-ips.json deliberately absent: it is optional).
    for name in ("ops.json", "whitelist.json", "banned-players.json",
                 "usercache.json"):
        (root / name).write_text("[]")
    return root


def _zip_members(root: Path, tmp: str) -> set:
    drive = GoogleDriveBackup(dry_run=True)
    worlds = drive._find_world_directories()
    zip_path = os.path.join(tmp, "backup.zip")
    drive._create_world_zip(worlds, zip_path)
    with zipfile.ZipFile(zip_path) as zf:
        return set(zf.namelist())


class _TmpServer(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = _make_server(self._tmp.name)
        patch("drive_backup.SERVER_DIR", str(self.root)).start()
        self.addCleanup(patch.stopall)


class TestBackupCapturesPlayerState(_TmpServer):

    def test_world_and_player_progress_are_captured(self):
        members = _zip_members(self.root, self._tmp.name)
        self.assertIn("world/level.dat", members)
        self.assertIn(f"world/playerdata/{UUID}.dat", members)
        self.assertIn(f"world/stats/{UUID}.json", members)

    def test_all_three_worlds_are_captured(self):
        members = _zip_members(self.root, self._tmp.name)
        for name in ("world/region/r.0.0.mca",
                     "world_nether/region/r.0.0.mca",
                     "world_the_end/region/r.0.0.mca"):
            self.assertIn(name, members)

    def test_root_player_state_is_captured(self):
        """Ops, whitelist and bans must survive into the next session."""
        members = _zip_members(self.root, self._tmp.name)
        for name in ("ops.json", "whitelist.json", "banned-players.json",
                     "usercache.json"):
            self.assertIn(name, members)

    def test_a_missing_optional_state_file_is_not_an_error(self):
        os.remove(str(self.root / "usercache.json"))
        members = _zip_members(self.root, self._tmp.name)
        self.assertNotIn("usercache.json", members)
        self.assertIn("ops.json", members)
        # Every member lands under the server dir, so restore cannot escape it.
        self.assertFalse(any(m.startswith("..") or m.startswith("/")
                             for m in members))

    def test_exactly_the_present_root_state_files_are_added(self):
        present = {n for n in SERVER_STATE_FILES if (self.root / n).exists()}
        members = _zip_members(self.root, self._tmp.name)
        root_members = {m for m in members if "/" not in m}
        self.assertEqual(root_members, present)


class TestChangeDetection(_TmpServer):

    def setUp(self):
        super().setUp()
        self.drive = GoogleDriveBackup(dry_run=True)
        self.worlds = self.drive._find_world_directories()
        self.backup = {"modifiedTime": datetime.fromtimestamp(
            BACKUP_EPOCH, tz=timezone.utc).isoformat().replace("+00:00", "Z")}

    def _set_mtimes(self, seconds):
        for dirpath, _dirs, files in os.walk(self.root):
            for f in files:
                os.utime(os.path.join(dirpath, f), (seconds, seconds))
        for name in SERVER_STATE_FILES:
            path = self.root / name
            if path.exists():
                os.utime(str(path), (seconds, seconds))

    def test_drive_z_timestamp_does_not_raise(self):
        """The regression: aware vs naive made this throw TypeError."""
        self._set_mtimes(OLD_MTIME)
        self.assertIs(self.drive._worlds_unchanged(self.worlds, self.backup),
                      True)

    def test_a_newer_world_file_is_a_change(self):
        self._set_mtimes(OLD_MTIME)
        os.utime(str(self.root / "world" / "level.dat"),
                 (NEW_MTIME, NEW_MTIME))
        self.assertFalse(self.drive._worlds_unchanged(self.worlds, self.backup))

    def test_a_newer_whitelist_is_a_change(self):
        """Root state files must trigger a backup, not be ignored."""
        self._set_mtimes(OLD_MTIME)
        os.utime(str(self.root / "whitelist.json"), (NEW_MTIME, NEW_MTIME))
        self.assertFalse(self.drive._worlds_unchanged(self.worlds, self.backup))

    def test_an_unparseable_timestamp_forces_a_backup(self):
        self._set_mtimes(OLD_MTIME)
        self.assertFalse(self.drive._worlds_unchanged(
            self.worlds, {"modifiedTime": "not-a-date"}))

    def test_no_backup_means_changed(self):
        self.assertFalse(self.drive._worlds_unchanged(self.worlds, None))


class TestOnlinePlayerCount(unittest.TestCase):
    """The count must be real online players, never a log-length derivation."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tracker = PlayerTracker(server_dir=self._tmp.name)
        patch("agent.player_tracker.player_tracker", self.tracker).start()
        self.addCleanup(patch.stopall)

    def _count(self):
        return agent_status.get_player_count()

    def test_no_observations_reports_unknown_not_zero(self):
        self.assertIsNone(self._count())

    def test_a_join_increases_the_count(self):
        self.tracker.observe_line(
            "[12:00:00] [Server thread/INFO]: Steve joined the game")
        self.assertEqual(self._count(), 1)

    def test_a_join_then_leave_is_zero_but_known(self):
        self.tracker.observe_line(
            "[12:00:00] [Server thread/INFO]: Steve joined the game")
        self.tracker.observe_line(
            "[12:05:00] [Server thread/INFO]: Steve left the game")
        self.assertEqual(self._count(), 0)

    def test_list_line_reconciles_the_count(self):
        self.tracker.observe_line(
            "[12:00:00] [Server thread/INFO]: There are 2 of a max of 20 "
            "players online: Steve, Alex")
        self.assertEqual(self._count(), 2)

    def test_historical_join_lines_are_not_counted(self):
        """The old bug: `content.count('joined the game')` over the whole log."""
        logs = self._tmp.name
        os.makedirs(os.path.join(logs, "logs"), exist_ok=True)
        with open(os.path.join(logs, "logs", "latest.log"), "w") as fh:
            for i in range(7):
                fh.write(f"[00:0{i}:00] [Server thread/INFO]: P{i} joined "
                         "the game\n")
        # The tracker has observed nothing this run, so the answer is unknown —
        # not the 7 stale joins sitting in the log file.
        self.assertIsNone(self._count())


if __name__ == "__main__":
    unittest.main(verbosity=2)
