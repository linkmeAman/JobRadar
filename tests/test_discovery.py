"""Offline unit tests for Google Dork ATS discovery adapter."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from job_radar import scrape_state
from job_radar.sources import discovery


class FakeResponse:
    def __init__(self, payload=None, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_database_path = scrape_state.DATABASE_PATH
        scrape_state.DATABASE_PATH = (
            Path(self.temporary_directory.name) / "jobs.db"
        )

    def tearDown(self) -> None:
        scrape_state.DATABASE_PATH = self.original_database_path
        self.temporary_directory.cleanup()

    def test_extract_slug_from_url(self) -> None:
        self.assertEqual(
            discovery.extract_slug_from_url(
                "https://boards.greenhouse.io/stripe/jobs/4019284"
            ),
            ("greenhouse", "stripe"),
        )
        self.assertEqual(
            discovery.extract_slug_from_url(
                "https://jobs.lever.co/postman/848201-abc"
            ),
            ("lever", "postman"),
        )
        self.assertEqual(
            discovery.extract_slug_from_url(
                "https://jobs.ashbyhq.com/linear/91823-xyz"
            ),
            ("ashby", "linear"),
        )
        # Ignored paths
        self.assertIsNone(
            discovery.extract_slug_from_url(
                "https://boards.greenhouse.io/embed/job_board?for=stripe"
            )
        )

    def test_extract_role_terms(self) -> None:
        config = {
            "searches": [
                {"search_term": "Python backend engineer FastAPI"},
                {"google_search_term": "Golang backend AWS engineer jobs India remote"},
            ]
        }
        terms = discovery.extract_role_terms(config)
        self.assertIn("python backend engineer fastapi", terms)
        self.assertIn("golang backend aws engineer jobs india", terms)

    def test_discover_slugs_queries_google_cse_and_caches_in_sqlite(self) -> None:
        session = MagicMock()
        session.get.return_value = FakeResponse(
            payload={
                "items": [
                    {
                        "link": "https://boards.greenhouse.io/datadog/jobs/12345"
                    },
                    {
                        "link": "https://jobs.lever.co/figma/abc-123"
                    },
                ]
            }
        )

        with patch.dict(
            "os.environ",
            {"GOOGLE_CSE_KEY": "fake_key", "GOOGLE_CSE_ID": "fake_id"},
        ):
            discovered = discovery.discover_slugs(
                session, {"searches": [{"search_term": "python backend"}]}
            )

        self.assertIn(("greenhouse", "datadog"), discovered)
        self.assertIn(("lever", "figma"), discovered)

        # Check SQLite persistence
        cached = scrape_state.get_discovered_companies()
        cached_pairs = [(c["provider"], c["slug"]) for c in cached]
        self.assertIn(("greenhouse", "datadog"), cached_pairs)
        self.assertIn(("lever", "figma"), cached_pairs)

    def test_discover_slugs_skips_when_env_vars_missing(self) -> None:
        session = MagicMock()
        with patch.dict("os.environ", {}, clear=True):
            discovered = discovery.discover_slugs(session, {})
        self.assertEqual(discovered, [])
        session.get.assert_not_called()
