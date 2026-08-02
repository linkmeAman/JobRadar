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
        self.assertIn("OnUnitActiveSec=30min", timer)
        self.assertIn("RandomizedDelaySec=300", timer)

    def test_docker_runtime_files_and_cadence(self) -> None:
        self.assertTrue(Path("Dockerfile").is_file())
        compose = Path("docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("scheduler:", compose)
        self.assertIn("ui:", compose)
        self.assertIn("JOB_RADAR_INTERVAL_SECONDS", compose)
        self.assertIn("127.0.0.1:8765:8765", compose)

    def test_rotation_and_cooldown_settings(self) -> None:
        scraping = self.config["scraping"]
        self.assertLessEqual(scraping["searches_per_run"], 4)
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
            self.assertLessEqual(search["hours_old"], 72)
            self.assertLessEqual(search["results_wanted"], 20)

    def test_backend_hardening_defaults_are_safe(self) -> None:
        self.assertTrue(self.config["dynamic_searches"]["enabled"])
        self.assertEqual(self.config["matching"]["pending_expiry_days"], 7)
        self.assertGreaterEqual(
            self.config["monitoring"]["all_provider_failure_alert_runs"], 2
        )
        self.assertFalse(self.config["semantic"]["enabled"])

    def test_external_sources_are_low_frequency_and_bounded(self) -> None:
        external = self.config["external_sources"]
        self.assertTrue(external["enabled"])
        for name, source in external["sources"].items():
            self.assertTrue(source["enabled"], name)
            self.assertGreaterEqual(source["interval_hours"], 6)
        self.assertLessEqual(
            external["sources"]["hn_whos_hiring"]["max_comments"], 200
        )
        self.assertLessEqual(
            external["sources"]["cutshort"]["max_results_per_page"], 20
        )

    def test_application_followup_defaults_are_daily(self) -> None:
        applications = self.config["applications"]
        self.assertTrue(applications["enabled"])
        self.assertEqual(applications["stale_after_days"], 7)
        self.assertGreaterEqual(
            applications["reminder_interval_hours"], 24
        )
