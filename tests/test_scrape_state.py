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

    def test_provider_degradation_alert_is_once_per_failure_streak(self) -> None:
        status = {"linkedin": {"status": "failure", "error": "429"}}
        for _ in range(2):
            run_id = scrape_state.start_run([{"name": "linkedin"}])
            scrape_state.record_failure("linkedin", "429")
            scrape_state.complete_run(
                run_id,
                started_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
                provider_status=status,
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
        self.assertEqual(scrape_state.degraded_providers(status, 3), [])

        run_id = scrape_state.start_run([{"name": "linkedin"}])
        scrape_state.record_failure("linkedin", "429")
        scrape_state.complete_run(
            run_id,
            started_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
            provider_status=status,
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
        degraded = scrape_state.degraded_providers(status, 3)
        self.assertEqual(degraded[0]["provider"], "linkedin")
        self.assertEqual(degraded[0]["failures"], 3)
        scrape_state.mark_provider_alert_sent(["linkedin"])
        self.assertEqual(scrape_state.degraded_providers(status, 3), [])

        success_id = scrape_state.start_run([{"name": "linkedin"}])
        scrape_state.record_success("linkedin", 4)
        scrape_state.complete_run(
            success_id,
            started_at=datetime(2026, 7, 27, tzinfo=timezone.utc),
            provider_status={"linkedin": {"status": "success"}},
            scraped_count=4,
            matched_count=0,
            new_count=0,
            pending_count=0,
            queued_count=0,
            deferred_count=0,
            expired_count=0,
            sent_count=0,
            all_providers_failed=False,
        )
        self.assertEqual(scrape_state.degraded_providers(status, 3), [])

    def test_consecutive_empty_runs_counter_and_circuit_breaker_cooldown(self) -> None:
        start = datetime(2026, 7, 27, tzinfo=timezone.utc)
        run_id = scrape_state.start_run([{"name": "google"}])
        scrape_state.complete_run(
            run_id,
            started_at=start,
            provider_status={"google": {"status": "success", "results": 15}},
            scraped_count=15,
            matched_count=0,
            new_count=0,
            pending_count=0,
            queued_count=0,
            deferred_count=0,
            expired_count=0,
            sent_count=0,
            all_providers_failed=False,
        )
        self.assertEqual(scrape_state.provider_historical_median("google"), 15.0)

        for _ in range(5):
            end = scrape_state.record_success(
                "google", 0, base_cooldown_minutes=120, max_cooldown_minutes=720, empty_run_circuit_breaker=6, now=start
            )
            self.assertIsNone(end)
            self.assertIsNone(scrape_state.blocked_until("google", now=start))

        tripped_end = scrape_state.record_success(
            "google", 0, base_cooldown_minutes=120, max_cooldown_minutes=720, empty_run_circuit_breaker=6, now=start
        )
        self.assertIsNotNone(tripped_end)
        self.assertEqual(tripped_end, start + timedelta(minutes=120))
        self.assertEqual(
            scrape_state.blocked_until("google", now=start + timedelta(minutes=30)),
            tripped_end,
        )

        scrape_state.record_success("google", 5, now=start + timedelta(minutes=130))
        self.assertIsNone(scrape_state.blocked_until("google", now=start + timedelta(minutes=131)))

    def test_sparse_provider_does_not_trigger_empty_run_circuit_breaker(self) -> None:
        start = datetime(2026, 7, 27, tzinfo=timezone.utc)
        self.assertEqual(scrape_state.provider_historical_median("hirist"), 0.0)

        for _ in range(10):
            end = scrape_state.record_success(
                "hirist", 0, base_cooldown_minutes=120, max_cooldown_minutes=720, empty_run_circuit_breaker=6, now=start
            )
            self.assertIsNone(end)

        self.assertIsNone(scrape_state.blocked_until("hirist", now=start))

    def test_alert_state_deduplication_and_recovery_transitions(self) -> None:
        start = datetime(2026, 7, 27, tzinfo=timezone.utc)
        run_id = scrape_state.start_run([{"name": "linkedin"}])
        scrape_state.complete_run(
            run_id,
            started_at=start,
            provider_status={"linkedin": {"status": "success", "results": 20}},
            scraped_count=20,
            matched_count=0,
            new_count=0,
            pending_count=0,
            queued_count=0,
            deferred_count=0,
            expired_count=0,
            sent_count=0,
            all_providers_failed=False,
        )

        status_degraded = {"linkedin": {"status": "failure", "error": "HTTP 429"}}
        for _ in range(3):
            r_id = scrape_state.start_run([{"name": "linkedin"}])
            scrape_state.record_failure("linkedin", "HTTP 429")
            scrape_state.complete_run(
                r_id,
                started_at=start,
                provider_status=status_degraded,
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

        newly_deg, newly_rec = scrape_state.evaluate_provider_alert_transitions(status_degraded, failure_threshold=3)
        self.assertEqual(len(newly_deg), 1)
        self.assertEqual(newly_deg[0]["provider"], "linkedin")
        self.assertEqual(newly_rec, [])

        scrape_state.mark_provider_alert_sent(["linkedin"])
        self.assertEqual(scrape_state.get_alert_state("linkedin"), "degraded")

        newly_deg2, newly_rec2 = scrape_state.evaluate_provider_alert_transitions(status_degraded, failure_threshold=3)
        self.assertEqual(newly_deg2, [])
        self.assertEqual(newly_rec2, [])

        status_healthy = {"linkedin": {"status": "success", "results": 25}}
        rec_id = scrape_state.start_run([{"name": "linkedin"}])
        scrape_state.record_success("linkedin", 25)
        scrape_state.complete_run(
            rec_id,
            started_at=start,
            provider_status=status_healthy,
            scraped_count=25,
            matched_count=0,
            new_count=0,
            pending_count=0,
            queued_count=0,
            deferred_count=0,
            expired_count=0,
            sent_count=0,
            all_providers_failed=False,
        )

        newly_deg3, newly_rec3 = scrape_state.evaluate_provider_alert_transitions(status_healthy, failure_threshold=3)
        self.assertEqual(newly_deg3, [])
        self.assertEqual(newly_rec3, ["linkedin"])

        scrape_state.mark_provider_recovered(["linkedin"])
        self.assertEqual(scrape_state.get_alert_state("linkedin"), "healthy")

        newly_deg4, newly_rec4 = scrape_state.evaluate_provider_alert_transitions(status_healthy, failure_threshold=3)
        self.assertEqual(newly_deg4, [])
        self.assertEqual(newly_rec4, [])

