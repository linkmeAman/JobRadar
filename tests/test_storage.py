"""Tests for SQLite migration and reliability defaults."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from job_radar import storage


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary_directory.name) / "jobs.db"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_migration_creates_versioned_schema_and_safe_pragmas(self) -> None:
        connection = storage.connect(self.database)
        try:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 10000)
            self.assertEqual(
                connection.execute("SELECT MAX(version) FROM schema_version").fetchone()[0],
                storage.SCHEMA_VERSION,
            )
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(applications)")
            }
            self.assertIn("description_snapshot", columns)
            self.assertIn("next_follow_up_at", columns)
        finally:
            connection.close()
        self.assertEqual(storage.integrity_check(self.database), "ok")

    def test_legacy_tables_receive_new_columns(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute("CREATE TABLE seen_jobs (job_id TEXT PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE applications (job_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL, status TEXT NOT NULL, last_contact TEXT NOT NULL)"
        )
        connection.commit()
        connection.close()

        connection = storage.connect(self.database)
        try:
            seen = {row[1] for row in connection.execute("PRAGMA table_info(seen_jobs)")}
            applications = {
                row[1] for row in connection.execute("PRAGMA table_info(applications)")
            }
        finally:
            connection.close()
        self.assertIn("notification_payload", seen)
        self.assertIn("normalized_company", seen)
        self.assertIn("notes", applications)
