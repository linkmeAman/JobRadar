"""Tests for persisted job and notification state."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

import dedupe


class DedupeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_database_path = dedupe.DATABASE_PATH
        dedupe.DATABASE_PATH = Path(self.temporary_directory.name) / "jobs.db"

    def tearDown(self) -> None:
        dedupe.DATABASE_PATH = self.original_database_path
        self.temporary_directory.cleanup()

    def test_only_unnotified_jobs_are_pending(self) -> None:
        jobs = pd.DataFrame(
            [
                {
                    "title": "Backend Engineer",
                    "company": "Acme",
                    "site": "linkedin",
                    "job_url": "https://example.test/a",
                    "is_remote": True,
                },
                {
                    "title": "Go Developer",
                    "company": "Beta",
                    "site": "indeed",
                    "job_url": "https://example.test/b",
                    "is_remote": False,
                },
            ]
        )

        self.assertEqual(len(dedupe.filter_new(jobs)), 2)
        pending = dedupe.pending_notifications()
        self.assertEqual(len(pending), 2)

        dedupe.mark_notified(pending.iloc[0]["job_id"])
        retry = dedupe.pending_notifications()
        self.assertEqual(len(retry), 1)
        self.assertEqual(retry.iloc[0]["title"], "Go Developer")

        self.assertTrue(dedupe.filter_new(jobs).empty)

    def test_pending_jobs_are_ranked_and_limited(self) -> None:
        jobs = pd.DataFrame(
            [
                {
                    "title": "Role A",
                    "company": "Acme",
                    "site": "linkedin",
                    "job_url": "https://example.test/a",
                    "match_score": 7,
                    "match_reasons": "python",
                },
                {
                    "title": "Role B",
                    "company": "Beta",
                    "site": "google",
                    "job_url": "https://example.test/b",
                    "match_score": 12,
                    "match_reasons": "backend, fastapi",
                },
            ]
        )
        dedupe.filter_new(jobs)
        pending = dedupe.pending_notifications(limit=1)

        self.assertEqual(len(pending), 1)
        self.assertEqual(pending.iloc[0]["title"], "Role B")
        self.assertEqual(pending.iloc[0]["match_score"], 12)
