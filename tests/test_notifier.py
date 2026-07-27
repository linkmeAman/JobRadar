"""Tests for Telegram retry behavior."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import pandas as pd
import requests

import notifier


class FakeResponse:
    def __init__(self, status_code: int, payload: dict, headers: dict | None = None):
        self.status_code = status_code
        self.payload = payload
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = requests.HTTPError(f"HTTP {self.status_code}")
            error.response = self
            raise error

    def json(self) -> dict:
        return self.payload


class NotifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {
                "TELEGRAM_BOT_TOKEN": "123456789:example_secret",
                "TELEGRAM_CHAT_ID": "987654321",
            },
            clear=False,
        )
        self.environment.start()
        self.job = pd.DataFrame(
            [
                {
                    "job_id": "job-1",
                    "title": "Backend Engineer",
                    "company": "Acme",
                    "is_remote": True,
                    "job_url": "https://example.test/job-1",
                }
            ]
        )

    def tearDown(self) -> None:
        self.environment.stop()

    def test_rate_limit_is_retried_using_telegram_delay(self) -> None:
        responses = [
            FakeResponse(429, {"ok": False, "parameters": {"retry_after": 2}}),
            FakeResponse(200, {"ok": True}),
        ]
        delivered: list[str] = []

        with patch("notifier.requests.post", side_effect=responses) as post, patch(
            "notifier.time.sleep"
        ) as sleep:
            sent = notifier.send_all(self.job, on_sent=delivered.append)

        self.assertEqual(sent, 1)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(2.0)
        self.assertEqual(delivered, ["job-1"])

    def test_forbidden_response_is_not_retried(self) -> None:
        with patch(
            "notifier.requests.post",
            return_value=FakeResponse(403, {"ok": False}),
        ) as post, self.assertRaisesRegex(RuntimeError, "HTTP 403"):
            notifier.send_all(self.job)

        self.assertEqual(post.call_count, 1)

    def test_job_message_includes_feedback_identifier(self) -> None:
        message = notifier.format_job_message(self.job.iloc[0])
        self.assertIn("🆔 job-1", message)
        self.assertIn("/applied job-1", message)

    def test_health_alert_uses_same_bot_api(self) -> None:
        with patch(
            "notifier.requests.post",
            return_value=FakeResponse(200, {"ok": True}),
        ) as post:
            notifier.send_health_alert("All providers failed")
        self.assertIn(
            "Job Radar health alert",
            post.call_args.kwargs["json"]["text"],
        )

    def test_fetch_updates_uses_persisted_offset(self) -> None:
        response = FakeResponse(
            200,
            {"ok": True, "result": [{"update_id": 42}]},
        )
        with patch(
            "notifier.requests.get", return_value=response
        ) as get:
            updates = notifier.fetch_updates(offset=42)

        self.assertEqual(updates, [{"update_id": 42}])
        self.assertEqual(get.call_args.kwargs["params"]["offset"], 42)
        self.assertEqual(get.call_args.kwargs["params"]["timeout"], 0)
