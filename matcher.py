"""Rank scraped jobs against the current resume profile."""

from __future__ import annotations

import re
from typing import Any

import pandas as pd


_ROLE_WEIGHTS = (
    ("backend", 8),
    ("platform", 6),
    ("software engineer", 5),
    ("software developer", 5),
    ("api", 4),
    ("full stack", 2),
)

_EXPERIENCE_PATTERN = re.compile(
    r"\b(\d{1,2})\+?\s*(?:years?|yrs?)\s+(?:of\s+)?"
    r"(?:professional\s+|relevant\s+)?experience\b"
)


def _text(row: pd.Series, fields: tuple[str, ...]) -> str:
    values = [row.get(field) for field in fields]
    return " ".join(str(value) for value in values if pd.notna(value)).lower()


def _has_term(text: str, term: str) -> bool:
    if term == "go":
        return bool(re.search(r"\b(go|golang)\b", text))
    return term in text


def _required_years(text: str) -> int | None:
    requirements = [int(value) for value in _EXPERIENCE_PATTERN.findall(text)]
    return max(requirements) if requirements else None


def _exclusion_reason(
    row: pd.Series,
    excluded_title_terms: list[str],
    maximum_required_years: int,
    allowed_countries: list[str],
) -> str | None:
    title = _text(row, ("title",))
    for term in excluded_title_terms:
        if term.lower() in title:
            return f"excluded title: {term}"

    details = _text(row, ("description",))
    required_years = _required_years(details)
    if required_years is not None and required_years > maximum_required_years:
        return f"requires {required_years} years"

    is_remote = row.get("is_remote")
    remote = pd.notna(is_remote) and bool(is_remote)
    country = row.get("country")
    if pd.notna(country) and not remote:
        normalized_country = str(country).strip().lower()
        allowed = {value.lower() for value in allowed_countries}
        if allowed and normalized_country not in allowed:
            return f"country not allowed: {country}"
    return None


def _score(
    row: pd.Series,
    profile: dict[str, Any],
    seniority_penalty_terms: list[str],
) -> tuple[int, list[str]]:
    title = _text(row, ("title",))
    details = _text(row, ("title", "description"))
    score = 0
    reasons: list[str] = []

    for role, weight in _ROLE_WEIGHTS:
        if role in title:
            score += weight
            reasons.append(role)
            break

    for term in seniority_penalty_terms:
        if term.lower() in title:
            score -= 2
            reasons.append(f"{term} stretch")
            break

    matched_skills = [skill for skill in profile["skills"] if _has_term(details, skill)]
    title_skills = [skill for skill in matched_skills if _has_term(title, skill)]
    score += min(len(matched_skills), 5)
    score += min(len(title_skills) * 2, 4)
    reasons.extend(title_skills[:2] or matched_skills[:2])

    is_remote = row.get("is_remote")
    if pd.notna(is_remote) and bool(is_remote):
        score += 1
        reasons.append("remote")
    return score, reasons


def filter_and_rank(
    df: pd.DataFrame,
    profile: dict[str, Any],
    minimum_score: int,
    excluded_title_terms: list[str] | None = None,
    seniority_penalty_terms: list[str] | None = None,
    maximum_required_years: int = 5,
    allowed_countries: list[str] | None = None,
) -> pd.DataFrame:
    """Keep resume-matched jobs, ordered from strongest to weakest fit."""
    if df.empty:
        return df.copy()

    excluded_title_terms = excluded_title_terms or []
    seniority_penalty_terms = seniority_penalty_terms or []
    allowed_countries = allowed_countries or []

    ranked = df.copy()
    exclusion_reasons = [
        _exclusion_reason(
            row,
            excluded_title_terms,
            maximum_required_years,
            allowed_countries,
        )
        for _, row in ranked.iterrows()
    ]
    ranked["exclusion_reason"] = exclusion_reasons
    ranked = ranked[ranked["exclusion_reason"].isna()].copy()
    scores_and_reasons = [
        _score(row, profile, seniority_penalty_terms)
        for _, row in ranked.iterrows()
    ]
    ranked["match_score"] = [score for score, _ in scores_and_reasons]
    ranked["match_reasons"] = [", ".join(reasons) for _, reasons in scores_and_reasons]
    ranked = ranked[ranked["match_score"] >= minimum_score]
    return ranked.sort_values("match_score", ascending=False, kind="stable").reset_index(drop=True)
