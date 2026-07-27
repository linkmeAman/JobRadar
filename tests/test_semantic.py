"""Tests for optional embedding-based borderline scoring."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import pandas as pd

import semantic


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "data": [
                {"index": 0, "embedding": [1.0, 0.0]},
                {"index": 1, "embedding": [1.0, 0.0]},
            ]
        }


class SemanticTests(unittest.TestCase):
    def test_borderline_job_receives_bounded_bonus(self) -> None:
        jobs = pd.DataFrame(
            [
                {
                    "title": "Backend Developer",
                    "company": "Acme",
                    "description": "Python APIs",
                    "match_score": 5,
                    "match_reasons": "python",
                    "exclusion_reason": None,
                }
            ]
        )
        settings = {
            "enabled": True,
            "borderline_min_score": 4,
            "borderline_max_score": 8,
            "minimum_similarity": 0.45,
            "maximum_bonus": 4,
        }
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test"}), patch(
            "semantic.requests.post", return_value=FakeResponse()
        ) as post:
            scored = semantic.apply_semantic_scoring(
                jobs,
                {"roles": ["backend engineer"], "skills": ["python"]},
                settings,
            )

        self.assertEqual(scored.iloc[0]["match_score"], 9)
        self.assertIn("semantic +4", scored.iloc[0]["match_reasons"])
        self.assertEqual(
            post.call_args.kwargs["json"]["model"], "text-embedding-3-small"
        )

    def test_disabled_scoring_needs_no_api_key(self) -> None:
        jobs = pd.DataFrame([{"match_score": 5}])
        with patch.dict(os.environ, {}, clear=True):
            scored = semantic.apply_semantic_scoring(
                jobs, {}, {"enabled": False}
            )
        self.assertEqual(scored.iloc[0]["match_score"], 5)

    def test_enabled_scoring_requires_api_key(self) -> None:
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(
            RuntimeError, "OPENAI_API_KEY"
        ):
            semantic.validate_settings({"enabled": True})
