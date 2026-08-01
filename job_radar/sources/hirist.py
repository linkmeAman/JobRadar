"""Hirist public category-feed adapter."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlencode

from .. import scrape_state
from .common import frame, slugify, utc_from_milliseconds
from scrapling import Fetcher, StealthyFetcher
from scrapling.parser import Adaptor


FEED_URL = "https://gladiator.hirist.tech/job/category/"
_NEXT_DATA = re.compile(
    r"""<script[^>]+id=["']__NEXT_DATA__["'][^>]*>(.*?)</script>""",
    re.DOTALL,
)


def _category_id(page_url: str, timeout: int) -> int:
    """Fetch the category page through StealthyFetcher to bypass Cloudflare Turnstile."""
    page = StealthyFetcher.fetch(
        page_url,
        headless=True,
        timeout=timeout * 1000,
    )
    html = page.html_content or ""
    match = _NEXT_DATA.search(html)
    if not match:
        scrape_state.record_scraper_health_event(
            "hirist", f"next_data_missing:{page_url}"
        )
        raise RuntimeError("Hirist category metadata was not found")
    payload = json.loads(match.group(1))
    return int(payload["props"]["pageProps"]["categoryId"])


def _job_row(job: dict[str, Any]) -> dict[str, Any]:
    title = str(job.get("title") or "Untitled role")
    company_data = job.get("companyData") or {}
    company = str(company_data.get("companyName") or "Unknown company")
    locations = [
        str(location.get("name"))
        for location in (job.get("locations") or job.get("location") or [])
        if location.get("name")
    ]
    location_text = ", ".join(locations)
    is_remote = bool(job.get("workFromHome")) or bool(
        re.search(r"\b(remote|anywhere)\b", location_text, re.IGNORECASE)
    )
    tags = [
        str(tag.get("name"))
        for tag in (job.get("tags") or [])
        if tag.get("name")
    ]
    minimum_years = job.get("min")
    experience = (
        f"Requires {minimum_years}+ years of experience. "
        if minimum_years is not None
        else ""
    )
    description = experience + (
        f"Skills: {', '.join(tags)}." if tags else ""
    )
    job_id = int(job["id"])
    job_url = (
        f"https://www.hirist.tech/j/{slugify(title)}-{job_id}"
    )
    salary_hidden = bool(job.get("hideSal"))
    multiplier = 100000

    return {
        "title": title,
        "company": company,
        "city": location_text or None,
        "state": None,
        "country": "India",
        "job_url": job_url,
        "description": description,
        "is_remote": is_remote,
        "remote_restriction": None,
        "site": "hirist",
        "date_posted": utc_from_milliseconds(
            job.get("createdTimeMs") or job.get("createdTime")
        ),
        "min_amount": (
            None if salary_hidden or not job.get("minSal")
            else float(job["minSal"]) * multiplier
        ),
        "max_amount": (
            None if salary_hidden or not job.get("maxSal")
            else float(job["maxSal"]) * multiplier
        ),
        "currency": None if salary_hidden else "INR",
    }


def scrape(
    settings: dict[str, Any],
    session: requests.Session,
    timeout: int,
) -> Any:
    """Fetch configured public Hirist backend and AI category feeds.

    Uses StealthyFetcher for the category page (Cloudflare Turnstile) and
    plain Fetcher for the JSON API.  The ``session`` parameter is accepted
    for API compatibility but not used internally.
    """
    rows: list[dict[str, Any]] = []
    categories = settings.get("categories") or []
    max_results = max(1, int(settings.get("max_results_per_category", 20)))
    for category in categories:
        category_id = category.get("id")
        if category_id is None:
            category_id = _category_id(
                str(category["page_url"]), timeout
            )

        # The feed API endpoint is a plain JSON service without anti-bot
        # protection, so we use the lightweight Fetcher instead of
        # StealthyFetcher.
        params = urlencode({
            "categoryId": int(category_id),
            "size": max_results,
            "page": 0,
        })
        api_page = Fetcher.get(
            f"{FEED_URL}?{params}",
            timeout=timeout,
        )
        # Fetcher returns an Adaptor; extract text for JSON parsing.
        raw_text = api_page.get_all_text(strip=True)
        try:
            api_data = json.loads(raw_text) or {}
        except (json.JSONDecodeError, TypeError):
            api_data = {}
        jobs = list(api_data.get("data") or [])
        rows.extend(_job_row(job) for job in jobs[:max_results])
    return frame(rows)
