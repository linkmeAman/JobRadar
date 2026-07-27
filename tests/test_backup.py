"""Tests for recoverable SQLite backups."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from job_radar.backup import backup_database, restore_database


class BackupTests(unittest.TestCase):
    def test_backup_copies_sqlite_state_and_prunes_old_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "jobs.db"
            destination = root / "backups"
            connection = sqlite3.connect(source)
            connection.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, title TEXT)")
            connection.execute("INSERT INTO jobs (title) VALUES ('Backend Engineer')")
            connection.commit()
            connection.close()

            old = destination / "jobs-old.db"
            destination.mkdir()
            old.write_bytes(b"old")
            old.touch()
            old_timestamp = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
            import os

            os.utime(old, (old_timestamp, old_timestamp))

            target = backup_database(
                destination,
                retention_days=14,
                source=source,
                now=datetime(2026, 7, 27, tzinfo=timezone.utc),
            )
            self.assertTrue(target.exists())
            self.assertFalse(old.exists())
            copy = sqlite3.connect(target)
            self.assertEqual(
                copy.execute("SELECT title FROM jobs").fetchone()[0],
                "Backend Engineer",
            )
            copy.close()

    def test_restore_database_verifies_and_replaces_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.db"
            backup = root / "backup.db"
            target = root / "target.db"
            for path, title in ((source, "Original"), (target, "Old")):
                connection = sqlite3.connect(path)
                connection.execute("CREATE TABLE jobs (title TEXT)")
                connection.execute("INSERT INTO jobs VALUES (?)", (title,))
                connection.commit()
                connection.close()
            backup_database(root, source=source, now=datetime(2026, 7, 27, tzinfo=timezone.utc))
            created = next(root.glob("jobs-*.db"))
            created.replace(backup)

            restored = restore_database(backup, target)

            self.assertEqual(restored, target)
            connection = sqlite3.connect(target)
            self.assertEqual(connection.execute("SELECT title FROM jobs").fetchone()[0], "Original")
            connection.close()
