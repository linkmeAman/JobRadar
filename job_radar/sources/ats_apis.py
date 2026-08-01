"""ATS public JSON API adapters (Greenhouse, Lever, Ashby, SmartRecruiters)."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import requests

from . import is_within_window
from .common import frame


logger = logging.getLogger(__name__)


def fetch_greenhouse(
    slug: str,
    session: requests.Session,
    timeout: int = 30,
    max_age_days: int = 14,
    company_name: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch jobs from Greenhouse public JSON API."""
    if not slug or not slug.strip():
        return []
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug.strip()}/jobs?content=true"
    try:
        response = session.get(url, timeout=timeout)
        if response.status_code == 404:
            logger.warning("Greenhouse board not found for slug=%s", slug)
            return []
        response.raise_for_status()
        data = response.json() or {}
    except Exception as exc:
        logger.warning("Greenhouse fetch failed for slug=%s: %s", slug, exc)
        return []

    jobs_data = data.get("jobs") or []
    company = company_name or slug.strip().capitalize()
    rows: list[dict[str, Any]] = []

    for item in jobs_data:
        date_posted = item.get("updated_at") or item.get("first_published_at")
        if not is_within_window(date_posted, max_age_days):
            continue

        title = str(item.get("title", "")).strip()
        if not title:
            continue

        job_url = str(item.get("absolute_url", "")).strip()
        location_obj = item.get("location") or {}
        location_str = (
            location_obj.get("name")
            if isinstance(location_obj, dict)
            else str(location_obj)
        ) or ""

        is_remote = bool(
            re.search(r"\bremote\b", location_str, re.IGNORECASE)
            or re.search(r"\bremote\b", title, re.IGNORECASE)
        )

        content = str(item.get("content") or "").strip()

        rows.append(
            {
                "title": title,
                "company": company,
                "city": location_str or None,
                "state": None,
                "country": None,
                "job_url": job_url,
                "description": content[:12000] if content else title,
                "is_remote": is_remote,
                "remote_restriction": None,
                "site": "ats:greenhouse",
                "date_posted": date_posted,
                "min_amount": None,
                "max_amount": None,
                "currency": None,
            }
        )
    return rows


def fetch_lever(
    slug: str,
    session: requests.Session,
    timeout: int = 30,
    max_age_days: int = 14,
    company_name: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch jobs from Lever public JSON API."""
    if not slug or not slug.strip():
        return []
    url = f"https://api.lever.co/v0/postings/{slug.strip()}?mode=json"
    try:
        response = session.get(url, timeout=timeout)
        if response.status_code == 404:
            logger.warning("Lever postings not found for slug=%s", slug)
            return []
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            data = []
    except Exception as exc:
        logger.warning("Lever fetch failed for slug=%s: %s", slug, exc)
        return []

    company = company_name or slug.strip().capitalize()
    rows: list[dict[str, Any]] = []

    for item in data:
        if not isinstance(item, dict):
            continue
        created_at = item.get("createdAt")
        date_posted = None
        if created_at is not None:
            try:
                date_posted = datetime.fromtimestamp(
                    float(created_at) / 1000.0, tz=timezone.utc
                ).isoformat()
            except (ValueError, TypeError, OSError):
                date_posted = None

        if not is_within_window(date_posted or created_at, max_age_days):
            continue

        title = str(item.get("text", "")).strip()
        if not title:
            continue

        job_url = str(item.get("hostedUrl", "")).strip()
        categories = item.get("categories") or {}
        location_str = (
            categories.get("location") if isinstance(categories, dict) else ""
        ) or ""
        workplace_type = str(item.get("workplaceType") or "").lower()

        is_remote = workplace_type == "remote" or bool(
            re.search(r"\bremote\b", location_str, re.IGNORECASE)
            or re.search(r"\bremote\b", title, re.IGNORECASE)
        )

        desc = str(
            item.get("descriptionPlain") or item.get("description") or ""
        ).strip()

        rows.append(
            {
                "title": title,
                "company": company,
                "city": location_str or None,
                "state": None,
                "country": None,
                "job_url": job_url,
                "description": desc[:12000] if desc else title,
                "is_remote": is_remote,
                "remote_restriction": None,
                "site": "ats:lever",
                "date_posted": date_posted,
                "min_amount": None,
                "max_amount": None,
                "currency": None,
            }
        )
    return rows


def fetch_ashby(
    slug: str,
    session: requests.Session,
    timeout: int = 30,
    max_age_days: int = 14,
    company_name: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch jobs from Ashby public JSON API."""
    if not slug or not slug.strip():
        return []
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug.strip()}"
    try:
        response = session.get(url, timeout=timeout)
        if response.status_code == 404:
            logger.warning("Ashby board not found for slug=%s", slug)
            return []
        response.raise_for_status()
        data = response.json() or {}
    except Exception as exc:
        logger.warning("Ashby fetch failed for slug=%s: %s", slug, exc)
        return []

    jobs_data = data.get("jobs") or []
    company = company_name or slug.strip().capitalize()
    rows: list[dict[str, Any]] = []

    for item in jobs_data:
        date_posted = item.get("publishedAt")
        if not is_within_window(date_posted, max_age_days):
            continue

        title = str(item.get("title", "")).strip()
        if not title:
            continue

        job_url = str(item.get("jobUrl", "")).strip()
        location_str = str(
            item.get("locationName") or item.get("location") or ""
        ).strip()
        is_remote_flag = item.get("isRemote")
        is_remote = bool(
            is_remote_flag
            or re.search(r"\bremote\b", location_str, re.IGNORECASE)
            or re.search(r"\bremote\b", title, re.IGNORECASE)
        )

        desc = str(
            item.get("descriptionPlain") or item.get("descriptionHtml") or ""
        ).strip()

        rows.append(
            {
                "title": title,
                "company": company,
                "city": location_str or None,
                "state": None,
                "country": None,
                "job_url": job_url,
                "description": desc[:12000] if desc else title,
                "is_remote": is_remote,
                "remote_restriction": None,
                "site": "ats:ashby",
                "date_posted": date_posted,
                "min_amount": None,
                "max_amount": None,
                "currency": None,
            }
        )
    return rows


def fetch_smartrecruiters(
    slug: str,
    session: requests.Session,
    timeout: int = 30,
    max_age_days: int = 14,
    company_name: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch jobs from SmartRecruiters public JSON API."""
    if not slug or not slug.strip():
        return []
    url = f"https://api.smartrecruiters.com/v1/companies/{slug.strip()}/postings"
    try:
        response = session.get(url, timeout=timeout)
        if response.status_code == 404:
            logger.warning(
                "SmartRecruiters company not found for slug=%s", slug
            )
            return []
        response.raise_for_status()
        data = response.json() or {}
    except Exception as exc:
        logger.warning(
            "SmartRecruiters fetch failed for slug=%s: %s", slug, exc
        )
        return []

    items = data.get("content") or []
    company = company_name or slug.strip().capitalize()
    rows: list[dict[str, Any]] = []

    for item in items:
        date_posted = item.get("releasedDate")
        if not is_within_window(date_posted, max_age_days):
            continue

        title = str(item.get("name", "")).strip()
        if not title:
            continue

        job_id = str(item.get("id", "")).strip()
        job_url = (
            f"https://jobs.smartrecruiters.com/{slug.strip()}/{job_id}"
            if job_id
            else ""
        )
        location_obj = item.get("location") or {}
        city = (
            location_obj.get("city")
            if isinstance(location_obj, dict)
            else None
        )
        country = (
            location_obj.get("country")
            if isinstance(location_obj, dict)
            else None
        )
        is_remote_flag = (
            location_obj.get("remote")
            if isinstance(location_obj, dict)
            else None
        )
        is_remote = bool(
            is_remote_flag or re.search(r"\bremote\b", title, re.IGNORECASE)
        )

        rows.append(
            {
                "title": title,
                "company": company,
                "city": city,
                "state": None,
                "country": country,
                "job_url": job_url,
                "description": f"{title} at {company}",
                "is_remote": is_remote,
                "remote_restriction": None,
                "site": "ats:smartrecruiters",
                "date_posted": date_posted,
                "min_amount": None,
                "max_amount": None,
                "currency": None,
            }
        )
    return rows


FETCHERS = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
}


def scrape(
    settings: dict[str, Any],
    session: requests.Session,
    timeout: int,
) -> pd.DataFrame:
    """Fetch job postings for all configured and discovered ATS company slugs."""
    configured_companies = settings.get("ats_companies")
    if configured_companies is None:
        configured_companies = settings.get("companies", [])
    companies = [dict(item) for item in configured_companies]
    max_age_days = int(settings.get("max_posting_age_days", 14))

    try:
        from .. import scrape_state

        discovered = scrape_state.get_discovered_companies()
        for item in discovered:
            if not any(
                c.get("provider") == item.get("provider")
                and c.get("slug") == item.get("slug")
                for c in companies
            ):
                companies.append(item)
    except Exception as exc:
        logger.warning("Could not load discovered companies from DB: %s", exc)

    rows: list[dict[str, Any]] = []
    for company in companies:
        provider = str(company.get("provider", "")).lower()
        slug = str(company.get("slug", "")).strip()
        name = company.get("name")
        fetcher = FETCHERS.get(provider)
        if not fetcher:
            logger.warning("Unsupported ATS provider: %s", provider)
            continue
        try:
            company_jobs = fetcher(
                slug,
                session=session,
                timeout=timeout,
                max_age_days=max_age_days,
                company_name=name,
            )
            rows.extend(company_jobs)
        except Exception as exc:
            logger.warning(
                "Failed fetching ATS provider=%s slug=%s: %s",
                provider,
                slug,
                exc,
            )

    return frame(rows)
