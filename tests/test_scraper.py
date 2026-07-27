"""Tests for per-site scraping isolation."""

from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd


jobspy_stub = types.ModuleType("jobspy")
jobspy_stub.scrape_jobs = lambda **_params: pd.DataFrame()
sys.modules.setdefault("jobspy", jobspy_stub)

import scraper
import scrape_state


class ScraperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_database_path = scrape_state.DATABASE_PATH
        scrape_state.DATABASE_PATH = (
            Path(self.temporary_directory.name) / "jobs.db"
        )
        self.searches = [
            {
                "name": "first",
                "site_name": ["linkedin", "google"],
                "search_term": "backend engineer",
                "google_search_term": "backend engineer jobs India remote",
                "location": "India",
                "is_remote": True,
                "job_type": "fulltime",
                "hours_old": 24,
                "results_wanted": 10,
            },
            {
                "name": "second",
                "site_name": ["linkedin", "google"],
                "search_term": "python backend",
                "google_search_term": "python backend jobs India remote",
                "location": "India",
                "is_remote": True,
                "job_type": "fulltime",
                "hours_old": 24,
                "results_wanted": 10,
            },
        ]

    def tearDown(self) -> None:
        scrape_state.DATABASE_PATH = self.original_database_path
        self.temporary_directory.cleanup()

    def test_rate_limited_site_does_not_discard_other_site(self) -> None:
        calls: list[str] = []

        def fake_scrape_jobs(**params):
            site = params["site_name"][0]
            calls.append(site)
            if site == "linkedin":
                raise RuntimeError("HTTP 429 Too Many Requests")
            return pd.DataFrame(
                [{"title": "Backend Engineer", "company": "Acme", "site": site}]
            )

        with patch("scraper.scrape_jobs", side_effect=fake_scrape_jobs):
            jobs = scraper.run_all(self.searches)

        self.assertEqual(len(jobs), 2)
        self.assertEqual(set(jobs["site"]), {"google"})
        self.assertEqual(calls.count("linkedin"), 1)
        self.assertEqual(calls.count("google"), 2)

    def test_indeed_validation_still_requires_country(self) -> None:
        search = {
            "site_name": ["indeed"],
            "search_term": "backend",
            "is_remote": True,
            "job_type": "fulltime",
        }
        with self.assertRaisesRegex(ValueError, "country_indeed"):
            scraper._validate_search(search)

    def test_persisted_cooldown_skips_provider_on_next_run(self) -> None:
        calls: list[str] = []

        def fake_scrape_jobs(**params):
            site = params["site_name"][0]
            calls.append(site)
            if site == "linkedin":
                raise RuntimeError("HTTP 429 Too Many Requests")
            return pd.DataFrame()

        with patch("scraper.scrape_jobs", side_effect=fake_scrape_jobs):
            scraper.run_all(self.searches[:1])
            scraper.run_all(self.searches[:1])

        self.assertEqual(calls.count("linkedin"), 1)
        self.assertEqual(calls.count("google"), 2)

    def test_dry_run_returns_report_without_writing_state(self) -> None:
        with patch("scraper.scrape_jobs", return_value=pd.DataFrame()):
            outcome = scraper.run_all(
                self.searches[:1],
                persist_state=False,
                return_report=True,
            )
        self.assertIsInstance(outcome, scraper.ScrapeOutcome)
        self.assertEqual(outcome.provider_status["google"]["status"], "success")
        self.assertFalse(scrape_state.DATABASE_PATH.exists())
