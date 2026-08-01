"""Offline contract tests for external source adapters and scheduling."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

import pandas as pd

from job_radar import scrape_state
from job_radar.sources import cutshort, hacker_news, hirist, runner


FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(
        self,
        *,
        text: str = "",
        payload=None,
        status_code: int = 200,
    ):
        self.text = text
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class RoutingSession:
    def __init__(self, routes: dict[str, FakeResponse]):
        self.routes = routes

    def get(self, url: str, **_kwargs) -> FakeResponse:
        for marker, response in self.routes.items():
            if marker in url:
                return response
        raise AssertionError(f"unexpected URL: {url}")


def _make_adaptor(html: str):
    """Create a Scrapling Adaptor from HTML text for test mocking."""
    from scrapling.parser import Adaptor

    return Adaptor(html, auto_match=False)


class SourceAdapterTests(unittest.TestCase):
    def test_hacker_news_maps_company_post_and_remote_restriction(self) -> None:
        session = RoutingSession(
            {
                "/item/123.json": FakeResponse(
                    payload={"id": 123, "kids": [456]}
                ),
                "/item/456.json": FakeResponse(
                    payload={
                        "id": 456,
                        "by": "founder",
                        "time": 1785090600,
                        "text": (
                            "Acme | Backend Engineer | REMOTE (US only)"
                            "<p>Python, FastAPI, AWS and Docker."
                        ),
                    }
                ),
            }
        )
        jobs = hacker_news.scrape(
            {"thread_id": 123, "max_comments": 10}, session, 20
        )
        self.assertEqual(len(jobs), 1)
        row = jobs.iloc[0]
        self.assertEqual(row["company"], "Acme")
        self.assertTrue(row["is_remote"])
        self.assertEqual(row["remote_restriction"], "United States")
        self.assertEqual(row["site"], "hn_whos_hiring")

    def test_hacker_news_search_selects_exact_newest_thread(self) -> None:
        session = RoutingSession(
            {
                "hn.algolia.com": FakeResponse(
                    payload={
                        "hits": [
                            {
                                "objectID": "100",
                                "title": "Ask HN: Who is dating? (July 2026)",
                                "created_at": "2026-07-18T00:00:00Z",
                            },
                            {
                                "objectID": "200",
                                "title": "Ask HN: Who is hiring? (July 2026)",
                                "created_at": "2026-07-01T00:00:00Z",
                            },
                        ]
                    }
                ),
                "/item/200.json": FakeResponse(
                    payload={
                        "id": 200,
                        "title": "Ask HN: Who is hiring?",
                        "kids": [],
                    }
                ),
            }
        )
        thread = hacker_news._find_thread(session, 20, None)
        self.assertEqual(thread["id"], 200)

    def test_cutshort_parses_public_job_card(self) -> None:
        html = (FIXTURES / "cutshort_jobs.html").read_text(encoding="utf-8")
        adaptor = _make_adaptor(html)
        with patch.object(
            cutshort, "Fetcher"
        ) as mock_fetcher, patch.object(
            cutshort.scrape_state, "record_scraper_health_event"
        ):
            mock_fetcher.get.return_value = adaptor
            jobs = cutshort.scrape(
                {"pages": ["https://cutshort.io/jobs/api-jobs"]},
                MagicMock(),
                20,
            )
        self.assertEqual(len(jobs), 1)
        row = jobs.iloc[0]
        self.assertEqual(row["title"], "Backend Engineer")
        self.assertEqual(row["company"], "Acme")
        self.assertTrue(row["is_remote"])
        self.assertEqual(row["min_amount"], 800000)
        self.assertIn("Requires 2+ years", row["description"])

    def test_hirist_maps_structured_category_feed(self) -> None:
        html = (FIXTURES / "hirist_category.html").read_text(encoding="utf-8")
        payload = json.loads(
            (FIXTURES / "hirist_feed.json").read_text(encoding="utf-8")
        )
        category_adaptor = _make_adaptor(html)
        feed_adaptor = _make_adaptor(json.dumps(payload))
        with patch.object(
            hirist, "StealthyFetcher"
        ) as mock_stealthy, patch.object(
            hirist, "Fetcher"
        ) as mock_fetcher, patch.object(
            hirist.scrape_state, "record_scraper_health_event"
        ):
            mock_stealthy.fetch.return_value = category_adaptor
            mock_fetcher.get.return_value = feed_adaptor
            jobs = hirist.scrape(
                {
                    "categories": [
                        {
                            "page_url": (
                                "https://www.hirist.tech/c/backend-development-jobs"
                            )
                        }
                    ]
                },
                MagicMock(),
                20,
            )
        self.assertEqual(len(jobs), 1)
        row = jobs.iloc[0]
        self.assertEqual(row["company"], "Example Labs")
        self.assertEqual(row["min_amount"], 800000)
        self.assertIn("FastAPI", row["description"])
        self.assertTrue(row["job_url"].endswith("-1657001"))


class SourceRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_database_path = scrape_state.DATABASE_PATH
        scrape_state.DATABASE_PATH = (
            Path(self.temporary_directory.name) / "jobs.db"
        )
        self.config = {
            "enabled": True,
            "sources": {
                "cutshort": {
                    "enabled": True,
                    "interval_hours": 6,
                }
            },
        }

    def tearDown(self) -> None:
        scrape_state.DATABASE_PATH = self.original_database_path
        self.temporary_directory.cleanup()

    def test_successful_source_is_skipped_until_interval_expires(self) -> None:
        jobs = pd.DataFrame(
            [
                {
                    "title": "Backend Engineer",
                    "company": "Acme",
                    "site": "cutshort",
                }
            ]
        )
        with patch.dict(
            runner.ADAPTERS, {"cutshort": lambda *_args: jobs}, clear=False
        ):
            first = runner.run_all(self.config, persist_state=True)
            second = runner.run_all(self.config, persist_state=True)

        self.assertEqual(len(first.jobs), 1)
        self.assertEqual(first.provider_status["cutshort"]["status"], "success")
        self.assertTrue(second.jobs.empty)
        self.assertEqual(
            second.provider_status["cutshort"]["status"], "scheduled_skip"
        )

    def test_forced_dry_run_does_not_create_sqlite(self) -> None:
        with patch.dict(
            runner.ADAPTERS,
            {"cutshort": lambda *_args: pd.DataFrame()},
            clear=False,
        ):
            result = runner.run_all(
                self.config, persist_state=False, force=True
            )
        self.assertEqual(
            result.provider_status["cutshort"]["status"], "success"
        )
        self.assertFalse(scrape_state.DATABASE_PATH.exists())

    def test_one_source_failure_does_not_discard_another_source(self) -> None:
        config = {
            "enabled": True,
            "sources": {
                "hn_whos_hiring": {
                    "enabled": True,
                    "interval_hours": 6,
                },
                "cutshort": {
                    "enabled": True,
                    "interval_hours": 6,
                },
            },
        }
        jobs = pd.DataFrame(
            [
                {
                    "title": "Backend Engineer",
                    "company": "Acme",
                    "site": "cutshort",
                }
            ]
        )

        def fail(*_args):
            raise RuntimeError("source unavailable")

        with patch.dict(
            runner.ADAPTERS,
            {
                "hn_whos_hiring": fail,
                "cutshort": lambda *_args: jobs,
            },
            clear=False,
        ):
            result = runner.run_all(config, persist_state=True)

        self.assertEqual(len(result.jobs), 1)
        self.assertEqual(
            result.provider_status["hn_whos_hiring"]["status"],
            "failure",
        )
        self.assertEqual(
            result.provider_status["cutshort"]["status"], "success"
        )
