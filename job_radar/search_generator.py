"""Generate rotating JobSpy searches from the latest resume profile."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any


_ROLE_SKILLS = {
    "python backend engineer": ("python", "fastapi", "django"),
    "golang backend engineer": ("go", "aws", "microservices"),
    "api engineer": ("rest api", "mysql", "redis"),
    "platform engineer": ("aws", "docker", "linux"),
    "backend engineer": ("python", "go", "aws"),
    "software engineer": ("python", "go", "sql"),
}


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def generate_searches(
    profile: Mapping[str, Any],
    settings: Mapping[str, Any],
    configured_searches: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return resume-driven rotating searches plus configured always-run searches."""
    if not settings.get("enabled", True):
        return [dict(search) for search in configured_searches]

    profile_roles = [str(role).lower() for role in profile.get("roles", [])]
    profile_skills = {str(skill).lower() for skill in profile.get("skills", [])}
    always_run: list[dict[str, Any]] = []
    for configured in configured_searches:
        if not configured.get("always_run"):
            continue
        search = dict(configured)
        sites = search.get("site_name", [])
        if isinstance(sites, str):
            sites = [sites]
        if "indeed" in {str(site).lower() for site in sites}:
            strongest = [
                skill
                for skill in (
                    "python",
                    "go",
                    "fastapi",
                    "django",
                    "aws",
                    "docker",
                    "microservices",
                )
                if skill in profile_skills
            ][:5]
            if strongest:
                terms = ["golang" if skill == "go" else skill for skill in strongest]
                search["search_term"] = (
                    f"backend engineer ({' OR '.join(terms)}) fulltime remote"
                )
        always_run.append(search)
    role_limit = max(1, int(settings.get("role_limit", 4)))
    selected_roles = [
        role for role in _ROLE_SKILLS if role in profile_roles
    ][:role_limit]
    if not selected_roles:
        selected_roles = ["backend engineer", "software engineer"][:role_limit]

    location = str(settings.get("location", "India"))
    results_wanted = int(settings.get("results_wanted", 20))
    hours_old = int(settings.get("hours_old", 6))
    configured_sites = settings.get("site_name", ["linkedin", "google"])
    sites = (
        [configured_sites]
        if isinstance(configured_sites, str)
        else list(configured_sites)
    )
    generated: list[dict[str, Any]] = []

    for role in selected_roles:
        candidates = _ROLE_SKILLS[role]
        skills = [
            skill
            for skill in candidates
            if skill in profile_skills
            and skill not in role
            and not (skill == "go" and "golang" in role)
        ][:3]
        if not skills:
            skills = list(candidates[:2])
        search_term = " ".join([role, *skills])
        generated.append(
            {
                "name": f"resume_{_slug(role)}",
                "site_name": sites,
                "search_term": search_term,
                "google_search_term": (
                    f"{search_term} jobs {location} remote"
                ),
                "location": location,
                "is_remote": bool(settings.get("is_remote", True)),
                "job_type": str(settings.get("job_type", "fulltime")),
                "hours_old": hours_old,
                "results_wanted": results_wanted,
            }
        )

    return generated + always_run
