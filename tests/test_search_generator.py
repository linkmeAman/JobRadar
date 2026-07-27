"""Tests for resume-driven query generation."""

from __future__ import annotations

import unittest

import search_generator


class SearchGeneratorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.configured = [
            {"name": "fallback", "site_name": ["google"]},
            {
                "name": "indeed",
                "always_run": True,
                "site_name": ["indeed"],
                "country_indeed": "India",
            },
        ]
        self.settings = {
            "enabled": True,
            "location": "India",
            "role_limit": 4,
        }

    def test_queries_follow_resume_roles_and_skills(self) -> None:
        profile = {
            "roles": ["python backend engineer", "platform engineer"],
            "skills": ["python", "fastapi", "aws", "docker"],
        }
        searches = search_generator.generate_searches(
            profile, self.settings, self.configured
        )
        rotating = [search for search in searches if not search.get("always_run")]
        self.assertEqual(len(rotating), 2)
        self.assertIn("fastapi", rotating[0]["search_term"])
        self.assertIn("aws docker", rotating[1]["search_term"])
        self.assertEqual(searches[-1]["name"], "indeed")
        self.assertIn("python OR fastapi", searches[-1]["search_term"])

    def test_disabled_generation_returns_configured_searches(self) -> None:
        searches = search_generator.generate_searches(
            {"roles": [], "skills": []},
            {"enabled": False},
            self.configured,
        )
        self.assertEqual(searches, self.configured)
