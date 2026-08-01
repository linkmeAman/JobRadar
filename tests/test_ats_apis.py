"""Offline unit tests for ATS JSON API adapters."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock

from job_radar.sources import ats_apis


class FakeResponse:
    def __init__(self, payload=None, status_code: int = 200):
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
        return FakeResponse(status_code=404)


class AtsApisTests(unittest.TestCase):
    def test_fetch_greenhouse_parses_jobs_and_filters_dates(self) -> None:
        session = RoutingSession(
            {
                "boards-api.greenhouse.io": FakeResponse(
                    payload={
                        "jobs": [
                            {
                                "id": 101,
                                "title": "Senior Python Backend Engineer",
                                "absolute_url": "https://boards.greenhouse.io/stripe/jobs/101",
                                "updated_at": "2026-07-30T10:00:00Z",
                                "location": {"name": "Remote, US"},
                                "content": "<p>FastAPI experience required.</p>",
                            },
                            {
                                "id": 102,
                                "title": "Old Engineer",
                                "absolute_url": "https://boards.greenhouse.io/stripe/jobs/102",
                                "updated_at": "2026-06-01T10:00:00Z",  # Older than 14 days
                                "location": {"name": "San Francisco, CA"},
                                "content": "<p>Old job.</p>",
                            },
                        ]
                    }
                )
            }
        )

        jobs = ats_apis.fetch_greenhouse(
            "stripe", session, timeout=10, max_age_days=14
        )
        self.assertEqual(len(jobs), 1)
        row = jobs[0]
        self.assertEqual(row["title"], "Senior Python Backend Engineer")
        self.assertEqual(row["company"], "Stripe")
        self.assertTrue(row["is_remote"])
        self.assertEqual(row["site"], "ats:greenhouse")

    def test_fetch_lever_parses_postings(self) -> None:
        session = RoutingSession(
            {
                "api.lever.co": FakeResponse(
                    payload=[
                        {
                            "id": "lever-1",
                            "text": "Golang Platform Engineer",
                            "hostedUrl": "https://jobs.lever.co/postman/lever-1",
                            "createdAt": 1785090600000,
                            "workplaceType": "remote",
                            "categories": {"location": "Remote"},
                            "descriptionPlain": "Build scalable platform APIs.",
                        }
                    ]
                )
            }
        )

        jobs = ats_apis.fetch_lever(
            "postman", session, timeout=10, max_age_days=300
        )
        self.assertEqual(len(jobs), 1)
        row = jobs[0]
        self.assertEqual(row["title"], "Golang Platform Engineer")
        self.assertEqual(row["company"], "Postman")
        self.assertTrue(row["is_remote"])
        self.assertEqual(row["site"], "ats:lever")

    def test_fetch_ashby_parses_jobs(self) -> None:
        session = RoutingSession(
            {
                "api.ashbyhq.com": FakeResponse(
                    payload={
                        "jobs": [
                            {
                                "id": "ashby-1",
                                "title": "API Backend Developer",
                                "jobUrl": "https://jobs.ashbyhq.com/linear/ashby-1",
                                "publishedAt": "2026-07-28T12:00:00Z",
                                "isRemote": True,
                                "locationName": "Remote",
                                "descriptionPlain": "API development using Python and Go.",
                            }
                        ]
                    }
                )
            }
        )

        jobs = ats_apis.fetch_ashby(
            "linear", session, timeout=10, max_age_days=14
        )
        self.assertEqual(len(jobs), 1)
        row = jobs[0]
        self.assertEqual(row["title"], "API Backend Developer")
        self.assertEqual(row["company"], "Linear")
        self.assertTrue(row["is_remote"])
        self.assertEqual(row["site"], "ats:ashby")

    def test_fetch_smartrecruiters_parses_postings(self) -> None:
        session = RoutingSession(
            {
                "api.smartrecruiters.com": FakeResponse(
                    payload={
                        "content": [
                            {
                                "id": "sr-1",
                                "name": "Software Engineer",
                                "releasedDate": "2026-07-29T08:00:00Z",
                                "location": {
                                    "city": "Bengaluru",
                                    "country": "India",
                                    "remote": True,
                                },
                            }
                        ]
                    }
                )
            }
        )

        jobs = ats_apis.fetch_smartrecruiters(
            "acme", session, timeout=10, max_age_days=14
        )
        self.assertEqual(len(jobs), 1)
        row = jobs[0]
        self.assertEqual(row["title"], "Software Engineer")
        self.assertEqual(row["city"], "Bengaluru")
        self.assertEqual(row["country"], "India")
        self.assertTrue(row["is_remote"])
        self.assertEqual(row["site"], "ats:smartrecruiters")

    def test_ats_fetcher_handles_404_gracefully(self) -> None:
        session = RoutingSession({})  # returns 404 for any URL
        jobs = ats_apis.fetch_greenhouse("nonexistent", session, timeout=10)
        self.assertEqual(jobs, [])

        jobs_lever = ats_apis.fetch_lever("nonexistent", session, timeout=10)
        self.assertEqual(jobs_lever, [])
