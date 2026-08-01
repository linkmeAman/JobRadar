"""Tests for scraper_health table and adaptive degradation detection."""

from __future__ import annotations

import logging
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from job_radar import scrape_state


class ScraperHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_database_path = scrape_state.DATABASE_PATH
        scrape_state.DATABASE_PATH = (
            Path(self.temporary_directory.name) / "jobs.db"
        )

    def tearDown(self) -> None:
        scrape_state.DATABASE_PATH = self.original_database_path
        self.temporary_directory.cleanup()

    def test_record_scraper_health_event_inserts_row(self) -> None:
        now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        scrape_state.record_scraper_health_event(
            "cutshort", "zero_results:https://cutshort.io/jobs/api-jobs", now=now
        )
        from job_radar import storage

        connection = storage.connect(scrape_state.DATABASE_PATH, read_only=True)
        try:
            rows = connection.execute(
                "SELECT source, event, ts FROM scraper_health"
            ).fetchall()
        finally:
            connection.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "cutshort")
        self.assertIn("zero_results", rows[0][1])
        self.assertEqual(rows[0][2], now.isoformat())

    def test_check_adaptive_degradation_returns_false_below_threshold(self) -> None:
        now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        # Insert 2 events — below default threshold of 3
        scrape_state.record_scraper_health_event("cutshort", "event_1", now=now)
        scrape_state.record_scraper_health_event(
            "cutshort", "event_2", now=now + timedelta(minutes=1)
        )
        result = scrape_state.check_adaptive_degradation(
            "cutshort", threshold=3, now=now + timedelta(minutes=5)
        )
        self.assertFalse(result)

    def test_check_adaptive_degradation_returns_true_at_threshold(self) -> None:
        now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        # Insert 3 events — meets threshold of 3
        for i in range(3):
            scrape_state.record_scraper_health_event(
                "hirist", f"event_{i}", now=now + timedelta(minutes=i)
            )
        with self.assertLogs("job_radar.scrape_state", level=logging.WARNING) as ctx:
            result = scrape_state.check_adaptive_degradation(
                "hirist", threshold=3, now=now + timedelta(minutes=10)
            )
        self.assertTrue(result)
        self.assertTrue(
            any("adaptive degradation" in msg for msg in ctx.output)
        )

    def test_check_adaptive_degradation_ignores_old_events(self) -> None:
        now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        # Insert 3 events from > 30 minutes ago
        old = now - timedelta(minutes=45)
        for i in range(3):
            scrape_state.record_scraper_health_event(
                "cutshort", f"old_{i}", now=old + timedelta(minutes=i)
            )
        result = scrape_state.check_adaptive_degradation(
            "cutshort", threshold=3, now=now
        )
        self.assertFalse(result)

    def test_check_adaptive_degradation_ignores_other_sources(self) -> None:
        now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
        # Insert 3 events for hirist, check cutshort
        for i in range(3):
            scrape_state.record_scraper_health_event(
                "hirist", f"event_{i}", now=now + timedelta(minutes=i)
            )
        result = scrape_state.check_adaptive_degradation(
            "cutshort", threshold=3, now=now + timedelta(minutes=10)
        )
        self.assertFalse(result)

    def test_check_adaptive_degradation_returns_false_when_no_database(self) -> None:
        scrape_state.DATABASE_PATH = Path("/nonexistent/jobs.db")
        result = scrape_state.check_adaptive_degradation("cutshort")
        self.assertFalse(result)
