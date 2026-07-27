"""Tests for production configuration invariants."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml


class ConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = yaml.safe_load(
            Path("config.yaml").read_text(encoding="utf-8")
        )

    def test_thirty_minute_timer_expression(self) -> None:
        timer = Path("deploy/job-radar.timer").read_text(encoding="utf-8")
        self.assertIn("OnCalendar=*-*-* *:00/30:00", timer)

    def test_rotation_and_cooldown_settings(self) -> None:
        scraping = self.config["scraping"]
        self.assertEqual(scraping["searches_per_run"], 2)
        self.assertGreaterEqual(scraping["cooldown_minutes"], 30)
        self.assertGreaterEqual(
            scraping["max_cooldown_minutes"],
            scraping["cooldown_minutes"],
        )

    def test_indeed_filter_constraints(self) -> None:
        indeed_searches = [
            search
            for search in self.config["searches"]
            if "indeed" in search["site_name"]
        ]
        self.assertTrue(indeed_searches)
        for search in indeed_searches:
            self.assertIn("country_indeed", search)
            self.assertNotIn("hours_old", search)

    def test_rotating_searches_use_small_fresh_batches(self) -> None:
        rotating = [
            search
            for search in self.config["searches"]
            if not search.get("always_run")
        ]
        self.assertEqual(len(rotating), 4)
        for search in rotating:
            self.assertLessEqual(search["hours_old"], 6)
            self.assertLessEqual(search["results_wanted"], 20)

    def test_backend_hardening_defaults_are_safe(self) -> None:
        self.assertTrue(self.config["dynamic_searches"]["enabled"])
        self.assertEqual(self.config["matching"]["pending_expiry_days"], 7)
        self.assertGreaterEqual(
            self.config["monitoring"]["all_provider_failure_alert_runs"], 2
        )
        self.assertFalse(self.config["semantic"]["enabled"])
