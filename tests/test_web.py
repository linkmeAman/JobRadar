"""Tests for the framework-free local web state layer."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from threading import Thread
from unittest.mock import patch
from urllib.request import Request, urlopen

import pandas as pd

from job_radar import dedupe
from job_radar.web import server


class ImmediateExecutor:
    def submit(self, function, *args, **kwargs):
        function(*args, **kwargs)


class WebStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_database_path = dedupe.DATABASE_PATH
        dedupe.DATABASE_PATH = Path(self.temporary_directory.name) / "jobs.db"
        dedupe.filter_new(
            pd.DataFrame(
                [
                    {
                        "title": "Backend Engineer",
                        "company": "Acme",
                        "site": "google",
                        "job_url": "https://example.test/1",
                        "match_score": 10,
                        "match_reasons": "backend, python",
                    }
                ]
            )
        )

    def tearDown(self) -> None:
        dedupe.DATABASE_PATH = self.original_database_path
        self.temporary_directory.cleanup()

    def test_summary_and_active_filter_share_sqlite_state(self) -> None:
        job = server.jobs()[0]
        self.assertEqual(server.summary()["pending"], 1)
        self.assertTrue(job["is_active"])

        dedupe.set_active(job["job_id"], False)

        self.assertEqual(server.summary()["active"], 0)
        self.assertEqual(server.summary()["pending"], 0)
        self.assertEqual(server.jobs(status="inactive")[0]["status"], "inactive")

    def test_trigger_history_records_successful_ui_run(self) -> None:
        with patch(
            "job_radar.web.server.main.run",
            return_value={"run_id": 77},
        ):
            trigger_id = server.queue_trigger(ImmediateExecutor())

        history = server.trigger_history()
        self.assertEqual(trigger_id, history[0]["trigger_id"])
        self.assertEqual(history[0]["status"], "completed")
        self.assertEqual(history[0]["run_id"], 77)

    def test_http_report_and_active_toggle_endpoints(self) -> None:
        web_server = server.JobRadarServer(("127.0.0.1", 0))
        thread = Thread(target=web_server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{web_server.server_address[1]}"
        try:
            with urlopen(f"{base_url}/api/summary") as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers["Content-Type"].split(";")[0], "application/json")
            job_id = server.jobs()[0]["job_id"]
            request = Request(
                f"{base_url}/api/jobs/{job_id}",
                data=b'{"active": false}',
                method="PATCH",
                headers={"Content-Type": "application/json"},
            )
            with urlopen(request) as response:
                self.assertEqual(response.status, 200)
            with urlopen(f"{base_url}/") as response:
                self.assertIn(b"Run scraper", response.read())
        finally:
            web_server.shutdown()
            web_server.server_close()
            thread.join(timeout=2)
