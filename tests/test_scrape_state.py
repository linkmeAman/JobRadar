"""Tests for persistent provider cooldown and search rotation."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from job_radar import scrape_state


class ScrapeStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_database_path = scrape_state.DATABASE_PATH
        scrape_state.DATABASE_PATH = (
            Path(self.temporary_directory.name) / "jobs.db"
        )

    def tearDown(self) -> None:
        scrape_state.DATABASE_PATH = self.original_database_path
        self.temporary_directory.cleanup()

    def test_provider_cooldown_is_persistent_and_exponential(self) -> None:
        start = datetime(2026, 7, 27, tzinfo=timezone.utc)
        first_end = scrape_state.record_blocked(
            "linkedin", 120, 720, now=start
        )
        self.assertEqual(first_end, start + timedelta(minutes=120))
        self.assertEqual(
            scrape_state.blocked_until(
                "linkedin", now=start + timedelta(minutes=30)
            ),
            first_end,
        )

        second_end = scrape_state.record_blocked(
            "linkedin", 120, 720, now=first_end
        )
        self.assertEqual(second_end, first_end + timedelta(minutes=240))

        scrape_state.record_success("linkedin", 10, now=second_end)
        self.assertIsNone(
            scrape_state.blocked_until(
                "linkedin", now=second_end + timedelta(minutes=1)
            )
        )

    def test_searches_rotate_while_always_run_search_is_included(self) -> None:
        searches = [
            {"name": "one"},
            {"name": "two"},
            {"name": "three"},
            {"name": "four"},
            {"name": "indeed", "always_run": True},
        ]

        first = scrape_state.select_searches(searches, 2)
        second = scrape_state.select_searches(searches, 2)

        self.assertEqual(
            [search["name"] for search in first], ["one", "two", "indeed"]
        )
        self.assertEqual(
            [search["name"] for search in second],
            ["three", "four", "indeed"],
        )

    def test_dry_selection_does_not_create_or_advance_state(self) -> None:
        searches = [
            {"name": "one"},
            {"name": "two"},
            {"name": "three"},
        ]
        first = scrape_state.select_searches(searches, 1, advance=False)
        second = scrape_state.select_searches(searches, 1, advance=False)
        self.assertEqual(first[0]["name"], "one")
        self.assertEqual(second[0]["name"], "one")
        self.assertFalse(scrape_state.DATABASE_PATH.exists())

    def test_run_history_tracks_counts_and_failure_streak_alert(self) -> None:
        started = datetime(2026, 7, 27, tzinfo=timezone.utc)
        first = scrape_state.start_run([{"name": "one"}])
        scrape_state.complete_run(
            first,
            started_at=started,
            provider_status={"google": {"status": "failure"}},
            scraped_count=0,
            matched_count=0,
            new_count=0,
            pending_count=0,
            queued_count=0,
            deferred_count=0,
            expired_count=0,
            sent_count=0,
            all_providers_failed=True,
        )
        second = scrape_state.start_run([{"name": "two"}])
        scrape_state.complete_run(
            second,
            started_at=started,
            provider_status={"indeed": {"status": "cooldown"}},
            scraped_count=0,
            matched_count=0,
            new_count=0,
            pending_count=0,
            queued_count=0,
            deferred_count=0,
            expired_count=0,
            sent_count=0,
            all_providers_failed=True,
        )
        self.assertEqual(scrape_state.consecutive_all_failed_runs(), 2)
        self.assertFalse(scrape_state.current_failure_streak_alerted())
        scrape_state.mark_health_alert_sent(second)
        self.assertTrue(scrape_state.current_failure_streak_alerted())
