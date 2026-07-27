"""Integration-style tests for the Job Radar orchestration flow."""

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

import pandas as pd


jobspy_stub = types.ModuleType("jobspy")
jobspy_stub.scrape_jobs = lambda **_params: pd.DataFrame()
sys.modules.setdefault("jobspy", jobspy_stub)

import main


class MainTests(unittest.TestCase):
    def test_main_caps_pending_alerts_after_matching_and_dedupe(self) -> None:
        config = {
            "resume": {"paths": ["resume.pdf"]},
            "scraping": {
                "searches_per_run": 2,
                "cooldown_minutes": 120,
                "max_cooldown_minutes": 720,
            },
            "matching": {
                "minimum_score": 6,
                "max_alerts_per_run": 10,
                "maximum_required_years": 5,
                "allowed_countries": ["India"],
            },
            "searches": [{"name": "one"}, {"name": "two"}],
        }
        scraped = pd.DataFrame([{"title": "Backend Engineer"}])
        matched = scraped.assign(match_score=10, match_reasons="backend")
        pending = pd.DataFrame(
            [
                {"job_id": f"job-{index}", "title": f"Role {index}"}
                for index in range(12)
            ]
        )

        with patch("main.load_config", return_value=config), patch(
            "main.notifier.validate_delivery_target"
        ), patch(
            "main.resume_profile.load_or_refresh",
            return_value={"skills": ["python"]},
        ), patch(
            "main.scrape_state.select_searches",
            return_value=config["searches"],
        ), patch(
            "main.scraper.run_all", return_value=scraped
        ), patch(
            "main.matcher.filter_and_rank", return_value=matched
        ), patch(
            "main.dedupe.filter_new", return_value=matched
        ), patch(
            "main.dedupe.pending_notifications", return_value=pending
        ), patch(
            "main.notifier.send_all", return_value=10
        ) as send_all:
            main.main()

        queued = send_all.call_args.args[0]
        self.assertEqual(len(queued), 10)
        self.assertEqual(queued.iloc[0]["job_id"], "job-0")
