"""Integration-style tests for Job Radar orchestration."""

from __future__ import annotations

import sys
import types
import unittest
from contextlib import nullcontext
from unittest.mock import patch

import pandas as pd


jobspy_stub = types.ModuleType("jobspy")
jobspy_stub.scrape_jobs = lambda **_params: pd.DataFrame()
sys.modules.setdefault("jobspy", jobspy_stub)

from job_radar import main, scraper


def _config() -> dict:
    return {
        "resume": {"paths": ["resume.pdf"]},
        "scraping": {
            "searches_per_run": 2,
            "cooldown_minutes": 120,
            "max_cooldown_minutes": 720,
        },
        "matching": {
            "minimum_score": 6,
            "max_alerts_per_run": 10,
            "pending_expiry_days": 7,
            "maximum_required_years": 5,
            "allowed_countries": ["India"],
        },
        "dynamic_searches": {"enabled": True},
        "applications": {"enabled": True},
        "semantic": {"enabled": False},
        "searches": [{"name": "one"}, {"name": "two"}],
    }


class MainTests(unittest.TestCase):
    def test_normal_run_caps_pending_alerts_and_records_history(self) -> None:
        config = _config()
        scraped = pd.DataFrame([{"title": "Backend Engineer"}])
        evaluated = scraped.assign(
            match_score=10,
            match_reasons="backend",
            exclusion_reason=None,
            matched=True,
        )
        pending = pd.DataFrame(
            [
                {"job_id": f"job-{index}", "title": f"Role {index}"}
                for index in range(12)
            ]
        )
        outcome = scraper.ScrapeOutcome(
            scraped, {"google": {"status": "success", "results": 1}}
        )

        with patch("job_radar.main.load_config", return_value=config), patch(
            "job_radar.main.run_lock.single_instance", return_value=nullcontext()
        ), patch(
            "job_radar.main.notifier.validate_delivery_target"
        ), patch(
            "job_radar.main.application_tracker.run_automation",
            return_value=(1, 2),
        ) as application_automation, patch(
            "job_radar.main.resume_profile.load_or_refresh",
            return_value={"skills": ["python"], "roles": ["backend engineer"]},
        ), patch(
            "job_radar.main._selected_searches", return_value=config["searches"]
        ), patch(
            "job_radar.main.scrape_state.start_run", return_value=7
        ), patch(
            "job_radar.main.scraper.run_all", return_value=outcome
        ), patch(
            "job_radar.main.dedupe.feedback_adjustments", return_value={}
        ), patch(
            "job_radar.main.matcher.evaluate_jobs", return_value=evaluated
        ), patch(
            "job_radar.main.matcher.select_matches", return_value=evaluated
        ), patch(
            "job_radar.main.dedupe.filter_new", return_value=evaluated
        ), patch(
            "job_radar.main.dedupe.expire_pending", return_value=0
        ), patch(
            "job_radar.main.dedupe.pending_notifications", return_value=pending
        ), patch(
            "job_radar.main.notifier.send_all", return_value=10
        ) as send_all, patch(
            "job_radar.main.scrape_state.complete_run"
        ) as complete, patch(
            "job_radar.main._maybe_send_health_alert"
        ):
            main.main([])

        queued = send_all.call_args.args[0]
        self.assertEqual(len(queued), 10)
        self.assertEqual(queued.iloc[0]["job_id"], "job-0")
        self.assertEqual(complete.call_args.kwargs["sent_count"], 10)
        application_automation.assert_called_once_with(
            config["applications"]
        )

    def test_dry_run_does_not_validate_send_or_write_sqlite(self) -> None:
        config = _config()
        scraped = pd.DataFrame(
            [
                {
                    "title": "Backend Engineer",
                    "company": "Acme",
                    "site": "google",
                }
            ]
        )
        outcome = scraper.ScrapeOutcome(
            scraped, {"google": {"status": "success", "results": 1}}
        )
        evaluated = scraped.assign(
            required_experience=None,
            country_eligible=True,
            country_eligibility="country not listed",
            exclusion_reason=None,
            match_score=9,
            match_reasons="backend",
            matched=True,
        )

        with patch("job_radar.main.load_config", return_value=config), patch(
            "job_radar.main.run_lock.single_instance", return_value=nullcontext()
        ), patch(
            "job_radar.main.notifier.validate_delivery_target"
        ) as validate, patch(
            "job_radar.main.resume_profile.load_or_refresh",
            return_value={"skills": ["python"], "roles": ["backend engineer"]},
        ), patch(
            "job_radar.main._selected_searches", return_value=config["searches"]
        ), patch(
            "job_radar.main.scraper.run_all", return_value=outcome
        ) as run_all, patch(
            "job_radar.main.dedupe.feedback_adjustments", return_value={}
        ), patch(
            "job_radar.main.matcher.evaluate_jobs", return_value=evaluated
        ), patch(
            "job_radar.main.matcher.select_matches", return_value=evaluated
        ), patch(
            "job_radar.main.dedupe.seen_status",
            return_value=pd.Series([False]),
        ), patch(
            "job_radar.main.scrape_state.start_run"
        ) as start_run, patch(
            "job_radar.main.dedupe.filter_new"
        ) as filter_new, patch(
            "job_radar.main.notifier.send_all"
        ) as send_all, patch(
            "job_radar.main.application_tracker.run_automation"
        ) as application_automation:
            main.main(["--dry-run"])

        validate.assert_not_called()
        start_run.assert_not_called()
        filter_new.assert_not_called()
        send_all.assert_not_called()
        application_automation.assert_not_called()
        self.assertFalse(run_all.call_args.kwargs["persist_state"])

    def test_external_jobs_join_jobspy_results_before_matching(self) -> None:
        config = _config()
        config["external_sources"] = {
            "enabled": True,
            "sources": {
                "cutshort": {"enabled": True, "interval_hours": 6}
            },
        }
        jobspy_jobs = pd.DataFrame(
            [
                {
                    "title": "Backend Engineer",
                    "company": "JobSpy Co",
                    "site": "google",
                }
            ]
        )
        external_jobs = pd.DataFrame(
            [
                {
                    "title": "Python Engineer",
                    "company": "Cutshort Co",
                    "site": "cutshort",
                }
            ]
        )
        jobspy_outcome = scraper.ScrapeOutcome(
            jobspy_jobs, {"google": {"status": "success"}}
        )
        external_outcome = scraper.ScrapeOutcome(
            external_jobs, {"cutshort": {"status": "success"}}
        )

        def evaluate(jobs, *_args, **_kwargs):
            self.assertEqual(len(jobs), 2)
            return jobs.assign(
                required_experience=None,
                country_eligible=True,
                country_eligibility="country not listed",
                exclusion_reason=None,
                match_score=8,
                match_reasons="backend",
                matched=True,
            )

        with patch("job_radar.main.load_config", return_value=config), patch(
            "job_radar.main.run_lock.single_instance", return_value=nullcontext()
        ), patch(
            "job_radar.main.resume_profile.load_or_refresh",
            return_value={"skills": ["python"], "roles": ["backend engineer"]},
        ), patch(
            "job_radar.main._selected_searches", return_value=config["searches"]
        ), patch(
            "job_radar.main.scraper.run_all", return_value=jobspy_outcome
        ), patch(
            "job_radar.main.source_runner.run_all", return_value=external_outcome
        ), patch(
            "job_radar.main.dedupe.feedback_adjustments", return_value={}
        ), patch(
            "job_radar.main.matcher.evaluate_jobs", side_effect=evaluate
        ), patch(
            "job_radar.main.matcher.select_matches", return_value=pd.DataFrame()
        ), patch(
            "job_radar.main._print_explanations"
        ):
            main.main(["--dry-run"])
