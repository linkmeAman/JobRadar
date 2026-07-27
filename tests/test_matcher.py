"""Tests for resume-driven job ranking."""

from __future__ import annotations

import unittest
from pathlib import Path

import pandas as pd

import matcher
import resume_profile


class MatcherTests(unittest.TestCase):
    def test_profile_extracts_backend_skills_and_roles(self) -> None:
        profile = resume_profile.build_profile(
            "Backend engineer with Python, Go, FastAPI, AWS, Docker and Linux.",
            source_path=Path("resume.pdf"),
            source_hash="test",
        )
        self.assertIn("python", profile["skills"])
        self.assertIn("go", profile["skills"])
        self.assertIn("golang backend engineer", profile["roles"])

    def test_profile_normalizes_split_aws_text(self) -> None:
        profile = resume_profile.build_profile(
            "Backend engineer with A WS, Python and Docker.",
            source_path=Path("resume.pdf"),
            source_hash="test",
        )
        self.assertIn("aws", profile["skills"])

    def test_backend_job_ranks_above_unrelated_job(self) -> None:
        profile = {"skills": ["python", "go", "fastapi", "aws", "docker"]}
        jobs = pd.DataFrame(
            [
                {
                    "title": "Python Backend Engineer",
                    "description": "FastAPI, AWS, Docker and Go services",
                    "is_remote": True,
                },
                {
                    "title": "Sales Associate",
                    "description": "Customer outreach and account management",
                    "is_remote": False,
                },
            ]
        )
        matched = matcher.filter_and_rank(jobs, profile, minimum_score=5)
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched.iloc[0]["title"], "Python Backend Engineer")
        self.assertIn("backend", matched.iloc[0]["match_reasons"])

    def test_excludes_mismatched_title_and_excessive_experience(self) -> None:
        profile = {"skills": ["python", "fastapi", "aws"]}
        jobs = pd.DataFrame(
            [
                {
                    "title": "Frontend Engineer",
                    "description": "Python and AWS",
                    "country": "India",
                },
                {
                    "title": "Backend Engineer",
                    "description": "Requires 8+ years of experience with Python",
                    "country": "India",
                },
                {
                    "title": "Backend Engineer",
                    "description": "3+ years of experience with Python and FastAPI",
                    "country": "India",
                },
            ]
        )
        matched = matcher.filter_and_rank(
            jobs,
            profile,
            minimum_score=6,
            excluded_title_terms=["frontend"],
            maximum_required_years=5,
            allowed_countries=["India"],
        )
        self.assertEqual(len(matched), 1)
        self.assertEqual(matched.iloc[0]["title"], "Backend Engineer")

    def test_excludes_non_remote_job_outside_allowed_country(self) -> None:
        profile = {"skills": ["python", "fastapi"]}
        jobs = pd.DataFrame(
            [
                {
                    "title": "Backend Engineer",
                    "description": "Python and FastAPI",
                    "country": "United States",
                    "is_remote": False,
                }
            ]
        )
        matched = matcher.filter_and_rank(
            jobs,
            profile,
            minimum_score=1,
            allowed_countries=["India"],
        )
        self.assertTrue(matched.empty)
