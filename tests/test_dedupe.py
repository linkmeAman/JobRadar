"""Tests for persisted job and notification state."""

from __future__ import annotations

import tempfile
import unittest
import sqlite3
import json
from pathlib import Path

import pandas as pd

from job_radar import dedupe


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

    def test_normalized_url_ignores_tracking_parameters(self) -> None:
        first = "https://Example.test/jobs/123/?utm_source=mail&ref=feed"
        second = "https://example.test/jobs/123"
        self.assertEqual(
            dedupe.normalize_job_url(first),
            dedupe.normalize_job_url(second),
        )

    def test_legacy_identity_migrates_without_resending(self) -> None:
        job = {
            "title": "Backend Engineer",
            "company": "Acme",
            "site": "linkedin",
            "job_url": "https://example.test/jobs/123?utm_source=old",
            "match_score": 10,
        }
        legacy_id = dedupe._job_id(
            job["title"], job["company"], job["site"]
        )
        connection = sqlite3.connect(dedupe.DATABASE_PATH)
        try:
            connection.execute(
                """
                CREATE TABLE seen_jobs (
                    job_id TEXT PRIMARY KEY, title TEXT, company TEXT, site TEXT,
                    first_seen_at TIMESTAMP, notification_payload TEXT,
                    notified_at TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                INSERT INTO seen_jobs VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, CURRENT_TIMESTAMP)
                """,
                (
                    legacy_id,
                    job["title"],
                    job["company"],
                    job["site"],
                    json.dumps({"job_url": job["job_url"]}),
                ),
            )
            connection.execute(
                """
                CREATE TABLE applications (
                    job_id TEXT PRIMARY KEY,
                    applied_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_contact TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO applications
                    (job_id, applied_at, status, last_contact)
                VALUES (?, CURRENT_TIMESTAMP, 'applied', CURRENT_TIMESTAMP)
                """,
                (legacy_id,),
            )
            connection.commit()
        finally:
            connection.close()

        current = pd.DataFrame(
            [{**job, "job_url": "https://example.test/jobs/123"}]
        )
        self.assertTrue(dedupe.filter_new(current).empty)

        connection = sqlite3.connect(dedupe.DATABASE_PATH)
        try:
            migrated = connection.execute(
                "SELECT job_id FROM seen_jobs"
            ).fetchone()[0]
            migrated_application = connection.execute(
                "SELECT job_id FROM applications"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(migrated, dedupe.job_id_for(current.iloc[0]))
        self.assertEqual(migrated_application, migrated)
        self.assertNotEqual(migrated, legacy_id)

    def test_same_legacy_fields_with_different_urls_are_distinct(self) -> None:
        jobs = pd.DataFrame(
            [
                {
                    "title": "Backend Engineer",
                    "company": "Acme",
                    "site": "linkedin",
                    "job_url": "https://example.test/jobs/one",
                },
                {
                    "title": "Backend Engineer",
                    "company": "Acme",
                    "site": "linkedin",
                    "job_url": "https://example.test/jobs/two",
                },
            ]
        )
        self.assertEqual(len(dedupe.filter_new(jobs)), 2)
        self.assertTrue(dedupe.filter_new(jobs).empty)

    def test_missing_url_falls_back_to_fields_after_url_identity(self) -> None:
        with_url = pd.DataFrame(
            [
                {
                    "title": "Backend Engineer",
                    "company": "Acme",
                    "site": "linkedin",
                    "job_url": "https://example.test/jobs/one",
                }
            ]
        )
        dedupe.filter_new(with_url)
        without_url = with_url.drop(columns="job_url")
        self.assertTrue(dedupe.seen_status(without_url).iloc[0])
        self.assertTrue(dedupe.filter_new(without_url).empty)

    def test_pending_job_expiry_stops_retries(self) -> None:
        jobs = pd.DataFrame(
            [
                {
                    "title": "Old Role",
                    "company": "Acme",
                    "site": "google",
                    "job_url": "https://example.test/old",
                }
            ]
        )
        dedupe.filter_new(jobs)
        connection = sqlite3.connect(dedupe.DATABASE_PATH)
        try:
            connection.execute(
                """
                UPDATE seen_jobs
                SET first_seen_at = datetime('now', '-8 days')
                """
            )
            connection.commit()
        finally:
            connection.close()
        self.assertEqual(dedupe.expire_pending(7), 1)
        self.assertTrue(dedupe.pending_notifications().empty)

    def test_feedback_builds_conservative_scoring_adjustments(self) -> None:
        jobs = pd.DataFrame(
            [
                {
                    "title": "Python Backend Engineer",
                    "company": "Acme",
                    "site": "google",
                    "job_url": "https://example.test/relevant",
                    "match_reasons": "backend, python, fastapi",
                },
                {
                    "title": "Backend Engineer",
                    "company": "SpamCo",
                    "site": "google",
                    "job_url": "https://example.test/irrelevant",
                    "match_reasons": "backend, python",
                },
            ]
        )
        dedupe.filter_new(jobs)
        pending = dedupe.pending_notifications()
        relevant = pending[pending["company"] == "Acme"].iloc[0]["job_id"]
        irrelevant = pending[pending["company"] == "SpamCo"].iloc[0]["job_id"]
        dedupe.record_feedback(relevant[:12], "applied")
        dedupe.record_feedback(irrelevant[:12], "irrelevant")

        adjustments = dedupe.feedback_adjustments()
        self.assertIn("python", adjustments["preferred_skills"])
        self.assertIn("fastapi", adjustments["preferred_skills"])
        self.assertIn("spamco", adjustments["penalized_companies"])
