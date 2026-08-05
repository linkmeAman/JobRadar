from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd

from job_radar import aman_os, dedupe


class AmanOsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_database_path = dedupe.DATABASE_PATH
        dedupe.DATABASE_PATH = Path(self.temporary_directory.name) / "jobs.db"
        dedupe.filter_new(pd.DataFrame([{
            "title": "Python Backend Engineer", "company": "Acme", "site": "cutshort",
            "job_url": "https://example.test/jobs/1", "description": "Python FastAPI role building reliable APIs and database integrations for a production platform.",
            "city": "Mumbai", "country": "India", "is_remote": True,
        }]))

    def tearDown(self) -> None:
        dedupe.DATABASE_PATH = self.original_database_path
        self.temporary_directory.cleanup()

    def test_successful_sync_is_not_resent_until_payload_changes(self) -> None:
        response = MagicMock()
        response.json.return_value = {"received": 1, "inserted": 1, "updated": 0}
        with patch.dict(os.environ, {"AMAN_OS_ENDPOINT": "https://aman.example/api/job-radar", "AMAN_OS_API_KEY": "key"}, clear=False), patch("job_radar.aman_os.requests.post", return_value=response) as post:
            first = aman_os.sync_pending({"enabled": True})
            second = aman_os.sync_pending({"enabled": True})
        self.assertEqual(first.synchronized, 1)
        self.assertEqual(second.attempted, 0)
        self.assertEqual(post.call_count, 1)

    def test_failed_sync_is_retained_for_retry(self) -> None:
        with patch.dict(os.environ, {"AMAN_OS_ENDPOINT": "https://aman.example/api/job-radar", "AMAN_OS_API_KEY": "key"}, clear=False), patch("job_radar.aman_os.requests.post", side_effect=aman_os.requests.RequestException("offline")):
            result = aman_os.sync_pending({"enabled": True})
        self.assertEqual(result.attempted, 1)
        self.assertIsNotNone(result.error)
        self.assertEqual(len(dedupe.aman_os_sync_candidates()), 1)

    def test_changed_pending_payload_is_synchronized_again(self) -> None:
        response = MagicMock()
        response.json.return_value = {"received": 1, "inserted": 1, "updated": 0}
        environment = {
            "AMAN_OS_ENDPOINT": "https://aman.example/api/job-radar",
            "AMAN_OS_API_KEY": "key",
        }
        with patch.dict(os.environ, environment, clear=False), patch(
            "job_radar.aman_os.requests.post", return_value=response
        ) as post:
            aman_os.sync_pending({"enabled": True})
            dedupe.filter_new(pd.DataFrame([{
                "title": "Python Backend Engineer", "company": "Acme", "site": "cutshort",
                "job_url": "https://example.test/jobs/1", "description": "Updated Python FastAPI role with detailed API, testing, database, and production reliability requirements.",
            }]))
            second = aman_os.sync_pending({"enabled": True})
        self.assertEqual(second.synchronized, 1)
        self.assertEqual(post.call_count, 2)
