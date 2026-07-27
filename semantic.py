"""Optional OpenAI embedding score for deterministic borderline matches."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from typing import Any

import pandas as pd
import requests


EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"


def validate_settings(settings: Mapping[str, Any]) -> None:
    """Fail early when the optional stage is enabled without credentials."""
    if settings.get("enabled", False) and not os.environ.get(
        "OPENAI_API_KEY", ""
    ).strip():
        raise RuntimeError(
            "OPENAI_API_KEY must be set when semantic scoring is enabled"
        )


def _cosine(left: list[float], right: list[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def _job_text(row: pd.Series) -> str:
    fields = (
        row.get("title"),
        row.get("company"),
        row.get("description"),
    )
    return " | ".join(
        str(value) for value in fields if value is not None and not pd.isna(value)
    )[:4000]


def apply_semantic_scoring(
    evaluated: pd.DataFrame,
    profile: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> pd.DataFrame:
    """Add bounded semantic bonuses to non-excluded borderline jobs."""
    if evaluated.empty or not settings.get("enabled", False):
        return evaluated.copy()

    validate_settings(settings)
    api_key = os.environ["OPENAI_API_KEY"].strip()

    minimum = int(settings.get("borderline_min_score", 4))
    maximum = int(settings.get("borderline_max_score", 8))
    max_jobs = max(1, int(settings.get("max_jobs_per_run", 10)))
    candidates = evaluated[
        evaluated["exclusion_reason"].isna()
        & evaluated["match_score"].between(minimum, maximum)
    ].head(max_jobs)
    if candidates.empty:
        return evaluated.copy()

    profile_text = "Target roles: {roles}. Strong skills: {skills}.".format(
        roles=", ".join(str(value) for value in profile.get("roles", [])),
        skills=", ".join(str(value) for value in profile.get("skills", [])),
    )
    inputs = [profile_text, *[_job_text(row) for _, row in candidates.iterrows()]]
    response = requests.post(
        EMBEDDINGS_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": str(settings.get("model", "text-embedding-3-small")),
            "input": inputs,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    vectors = [
        item["embedding"]
        for item in sorted(payload["data"], key=lambda item: item["index"])
    ]
    if len(vectors) != len(inputs):
        raise RuntimeError("OpenAI returned an unexpected embedding count")

    result = evaluated.copy()
    result["semantic_similarity"] = pd.NA
    minimum_similarity = float(settings.get("minimum_similarity", 0.45))
    maximum_bonus = max(0, int(settings.get("maximum_bonus", 4)))
    resume_vector = vectors[0]
    for (index, _), vector in zip(candidates.iterrows(), vectors[1:]):
        similarity = _cosine(resume_vector, vector)
        result.at[index, "semantic_similarity"] = round(similarity, 4)
        if similarity < minimum_similarity:
            continue
        scaled = (similarity - minimum_similarity) / max(
            0.0001, 1.0 - minimum_similarity
        )
        bonus = max(1, min(maximum_bonus, round(scaled * maximum_bonus)))
        result.at[index, "match_score"] = int(result.at[index, "match_score"]) + bonus
        current = str(result.at[index, "match_reasons"]).strip()
        reason = f"semantic +{bonus}"
        result.at[index, "match_reasons"] = (
            f"{current}, {reason}" if current else reason
        )
    return result
