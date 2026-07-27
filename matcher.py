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


def _country_eligibility(
    row: pd.Series,
    allowed_countries: list[str],
) -> tuple[bool, str]:
    is_remote = row.get("is_remote")
    remote = pd.notna(is_remote) and bool(is_remote)
    country = row.get("country")
    if remote:
        return True, "remote"
    if pd.isna(country) or not str(country).strip():
        return True, "country not listed"

    normalized_country = str(country).strip().lower()
    allowed = {value.strip().lower() for value in allowed_countries}
    eligible = not allowed or normalized_country in allowed
    return eligible, str(country)


def _exclusion_reason(
    row: pd.Series,
    excluded_title_terms: list[str],
    maximum_required_years: int,
    country_eligible: bool,
) -> str | None:
    title = _text(row, ("title",))
    for term in excluded_title_terms:
        if term.lower() in title:
            return f"excluded title: {term}"

    details = _text(row, ("description",))
    required_years = _required_years(details)
    if required_years is not None and required_years > maximum_required_years:
        return f"requires {required_years} years"

    if not country_eligible:
        return f"country not allowed: {row.get('country')}"
    return None


def _score(
    row: pd.Series,
    profile: dict[str, Any],
    seniority_penalty_terms: list[str],
    feedback: dict[str, Any] | None = None,
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

    feedback = feedback or {}
    preferred_skills = [
        str(skill).lower() for skill in feedback.get("preferred_skills", [])
    ]
    feedback_matches = [
        skill for skill in preferred_skills if _has_term(details, skill)
    ]
    if feedback_matches:
        bonus = min(len(feedback_matches), 3)
        score += bonus
        reasons.append(f"feedback +{bonus}")

    company = _text(row, ("company",)).strip()
    penalized_companies = {
        str(value).strip().lower()
        for value in feedback.get("penalized_companies", [])
    }
    if company and company in penalized_companies:
        score -= 3
        reasons.append("feedback -3")
    return score, reasons


def evaluate_jobs(
    df: pd.DataFrame,
    profile: dict[str, Any],
    minimum_score: int,
    excluded_title_terms: list[str] | None = None,
    seniority_penalty_terms: list[str] | None = None,
    maximum_required_years: int = 5,
    allowed_countries: list[str] | None = None,
    feedback: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Explain every scraped row and flag jobs that pass deterministic matching."""
    if df.empty:
        empty = df.copy()
        for column in (
            "required_experience",
            "country_eligible",
            "country_eligibility",
            "exclusion_reason",
            "match_score",
            "match_reasons",
            "matched",
        ):
            empty[column] = pd.Series(dtype="object")
        return empty

    excluded_title_terms = excluded_title_terms or []
    seniority_penalty_terms = seniority_penalty_terms or []
    allowed_countries = allowed_countries or []

    ranked = df.copy()
    requirements = [
        _required_years(_text(row, ("description",)))
        for _, row in ranked.iterrows()
    ]
    eligibility = [
        _country_eligibility(row, allowed_countries)
        for _, row in ranked.iterrows()
    ]
    ranked["required_experience"] = requirements
    ranked["country_eligible"] = [eligible for eligible, _ in eligibility]
    ranked["country_eligibility"] = [reason for _, reason in eligibility]
    exclusion_reasons = [
        _exclusion_reason(
            row,
            excluded_title_terms,
            maximum_required_years,
            bool(ranked.iloc[position]["country_eligible"]),
        )
        for position, (_, row) in enumerate(ranked.iterrows())
    ]
    ranked["exclusion_reason"] = exclusion_reasons
    scores_and_reasons = [
        _score(row, profile, seniority_penalty_terms, feedback)
        for _, row in ranked.iterrows()
    ]
    ranked["match_score"] = [score for score, _ in scores_and_reasons]
    ranked["match_reasons"] = [", ".join(reasons) for _, reasons in scores_and_reasons]
    ranked["matched"] = ranked["exclusion_reason"].isna() & (
        ranked["match_score"] >= minimum_score
    )
    return ranked.reset_index(drop=True)


def select_matches(df: pd.DataFrame, minimum_score: int) -> pd.DataFrame:
    """Select and rank jobs after deterministic or optional semantic scoring."""
    if df.empty:
        return df.copy()
    selected = df[
        df["exclusion_reason"].isna() & (df["match_score"] >= minimum_score)
    ].copy()
    selected["matched"] = True
    return selected.sort_values(
        "match_score", ascending=False, kind="stable"
    ).reset_index(drop=True)


def filter_and_rank(
    df: pd.DataFrame,
    profile: dict[str, Any],
    minimum_score: int,
    excluded_title_terms: list[str] | None = None,
    seniority_penalty_terms: list[str] | None = None,
    maximum_required_years: int = 5,
    allowed_countries: list[str] | None = None,
    feedback: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Keep resume-matched jobs, ordered from strongest to weakest fit."""
    evaluated = evaluate_jobs(
        df,
        profile,
        minimum_score,
        excluded_title_terms,
        seniority_penalty_terms,
        maximum_required_years,
        allowed_countries,
        feedback,
    )
    return select_matches(evaluated, minimum_score)
