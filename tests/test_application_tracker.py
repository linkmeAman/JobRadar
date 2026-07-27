"""Tests for application state, Telegram commands, and stale reminders."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from job_radar import application_tracker, dedupe


class ApplicationTrackerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_dedupe_path = dedupe.DATABASE_PATH
        self.original_tracker_path = application_tracker.DATABASE_PATH
        database = Path(self.temporary_directory.name) / "jobs.db"
        dedupe.DATABASE_PATH = database
        application_tracker.DATABASE_PATH = database
        jobs = pd.DataFrame(
            [
                {
                    "title": "Backend Engineer",
                    "company": "Acme",
                    "site": "google",
                    "job_url": "https://example.test/jobs/1",
                    "match_reasons": "backend, python, fastapi",
                }
            ]
        )
        dedupe.filter_new(jobs)
        self.job_id = str(dedupe.pending_notifications().iloc[0]["job_id"])

    def tearDown(self) -> None:
        dedupe.DATABASE_PATH = self.original_dedupe_path
        application_tracker.DATABASE_PATH = self.original_tracker_path
        self.temporary_directory.cleanup()

    def test_applied_is_idempotent_and_creates_feedback(self) -> None:
        first_time = datetime(2026, 7, 1, tzinfo=timezone.utc)
        later = first_time + timedelta(days=2)

        first = application_tracker.mark_applied(
            self.job_id[:12], now=first_time
        )
        second = application_tracker.mark_applied(
            self.job_id[:12], now=later
        )

        self.assertTrue(first.created)
        self.assertFalse(second.created)
        connection = sqlite3.connect(application_tracker.DATABASE_PATH)
        try:
            application = connection.execute(
                """
                SELECT applied_at, status, last_contact
                FROM applications WHERE job_id = ?
                """,
                (self.job_id,),
            ).fetchone()
            feedback = connection.execute(
                "SELECT label FROM job_feedback WHERE job_id = ?",
                (self.job_id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(
            application,
            (first_time.isoformat(), "applied", first_time.isoformat()),
        )
        self.assertEqual(feedback, ("applied",))

    def test_contact_and_status_control_stale_followups(self) -> None:
        applied_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
        check_time = applied_at + timedelta(days=8)
        application_tracker.mark_applied(self.job_id, now=applied_at)

        stale = application_tracker.stale_applications(7, now=check_time)
        self.assertEqual([item["job_id"] for item in stale], [self.job_id])

        application_tracker.mark_contacted(
            self.job_id[:12], now=check_time
        )
        self.assertEqual(
            application_tracker.stale_applications(7, now=check_time), []
        )

        application_tracker.set_status(
            self.job_id[:12], "interview", now=check_time
        )
        later = check_time + timedelta(days=8)
        self.assertEqual(
            len(application_tracker.stale_applications(7, now=later)), 1
        )
        application_tracker.set_status(
            self.job_id[:12], "rejected", now=later
        )
        self.assertEqual(
            application_tracker.stale_applications(
                7, now=later + timedelta(days=8)
            ),
            [],
        )

    def test_commands_use_authorized_chat_and_persist_offset(self) -> None:
        updates = [
            {
                "update_id": 40,
                "message": {
                    "chat": {"id": 111},
                    "text": f"/applied {self.job_id[:12]}",
                },
            },
            {
                "update_id": 41,
                "message": {
                    "chat": {"id": 222},
                    "text": f"/applied {self.job_id[:12]}",
                },
            },
        ]
        replies: list[str] = []
        with patch(
            "job_radar.application_tracker.notifier.get_credentials",
            return_value=("123:token", "111"),
        ), patch(
            "job_radar.application_tracker.notifier.fetch_updates",
            return_value=updates,
        ) as fetch, patch(
            "job_radar.application_tracker.notifier.send_text",
            side_effect=replies.append,
        ):
            processed = application_tracker.process_telegram_commands()

        self.assertEqual(processed, 1)
        self.assertEqual(len(replies), 1)
        self.assertIn("Application saved", replies[0])
        self.assertEqual(application_tracker.update_offset(), 42)
        fetch.assert_called_once_with(offset=None)

    def test_inline_feedback_callback_uses_authorized_chat(self) -> None:
        updates = [
            {
                "update_id": 50,
                "callback_query": {
                    "id": "callback-1",
                    "message": {"chat": {"id": 111}},
                    "data": f"jr:irrelevant:{self.job_id[:12]}",
                },
            },
            {
                "update_id": 51,
                "callback_query": {
                    "id": "callback-2",
                    "message": {"chat": {"id": 222}},
                    "data": f"jr:relevant:{self.job_id[:12]}",
                },
            },
        ]
        replies: list[tuple[str, str]] = []
        with patch(
            "job_radar.application_tracker.notifier.get_credentials",
            return_value=("123:token", "111"),
        ), patch(
            "job_radar.application_tracker.notifier.fetch_updates",
            return_value=updates,
        ), patch(
            "job_radar.application_tracker.notifier.answer_callback_query",
            side_effect=lambda callback_id, text: replies.append((callback_id, text)),
        ):
            processed = application_tracker.process_telegram_commands()

        self.assertEqual(processed, 1)
        self.assertEqual(replies[0][0], "callback-1")
        self.assertIn("irrelevant", replies[0][1])
        self.assertEqual(application_tracker.update_offset(), 52)
        connection = sqlite3.connect(application_tracker.DATABASE_PATH)
        try:
            label = connection.execute(
                "SELECT label FROM job_feedback WHERE job_id = ?",
                (self.job_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(label, "irrelevant")

    def test_stale_summary_is_sent_only_once_per_interval(self) -> None:
        applied_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
        check_time = applied_at + timedelta(days=8)
        application_tracker.mark_applied(self.job_id, now=applied_at)
        messages: list[str] = []
        settings = {
            "enabled": True,
            "stale_after_days": 7,
            "reminder_interval_hours": 24,
            "max_reminders_per_message": 10,
        }

        with patch(
            "job_radar.application_tracker.notifier.send_text",
            side_effect=messages.append,
        ):
            first = application_tracker.maybe_send_stale_reminder(
                settings, now=check_time
            )
            second = application_tracker.maybe_send_stale_reminder(
                settings, now=check_time + timedelta(hours=1)
            )

        self.assertEqual(first, 1)
        self.assertEqual(second, 0)
        self.assertEqual(len(messages), 1)
        self.assertIn("Backend Engineer", messages[0])
        self.assertIn(self.job_id[:12], messages[0])
