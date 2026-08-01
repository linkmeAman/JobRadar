"""Cutshort server-rendered public job-page adapter."""

from __future__ import annotations

import re
from typing import Any

from .. import scrape_state
from . import is_within_window
from .common import frame
from scrapling import Fetcher
from scrapling.parser import Adaptor


_JOB_LINK = re.compile(r"^https?://(?:www\.)?cutshort\.io/job/")
_EXPERIENCE = re.compile(r"(\d+)\s*(?:-|to)\s*(\d+)\s*yrs?", re.IGNORECASE)
_SALARY = re.compile(
    r"₹\s*([\d.]+)\s*L\s*-\s*₹?\s*([\d.]+)\s*L",
    re.IGNORECASE,
)


def _is_job_link(href: str | None) -> bool:
    return bool(href and _JOB_LINK.match(str(href)))


def _css_first(node: Adaptor, selector: str) -> Adaptor | None:
    """Return the first CSS match or None (Scrapling has no css_first)."""
    results = node.css(selector)
    return results[0] if results else None


def _card_for(anchor: Adaptor) -> Adaptor | None:
    for parent in anchor.iterancestors():
        if parent.tag != "div":
            continue
        text = parent.get_all_text(separator=" ", strip=True)
        job_links = [
            link
            for link in parent.css("a[href]")
            if _is_job_link(link.attrib.get("href"))
        ]
        if (
            len(job_links) == 1
            and _EXPERIENCE.search(text)
            and _css_first(parent, "noscript") is not None
        ):
            return parent
    return None


def _description(card: Adaptor) -> str:
    descriptions: list[str] = []
    for element in card.css("noscript"):
        inner_html = element.html_content or ""
        # html_content includes the outer tag; parse it to get inner text.
        parsed = Adaptor(inner_html, auto_match=False)
        value = parsed.get_all_text(separator=" ", strip=True)
        if value:
            descriptions.append(value)
    return max(descriptions, key=len, default="")[:12000]


def _location(card: Adaptor) -> str | None:
    text = card.get_all_text(separator="\n", strip=True)
    values = [s.strip() for s in text.split("\n") if s.strip()]
    try:
        apply_position = values.index("Apply now")
    except ValueError:
        return None
    for value in values[apply_position + 1 : apply_position + 5]:
        if _EXPERIENCE.search(value):
            break
        if value.strip() and not value.strip().isdigit():
            return value.strip()
    return None


def _job_row(anchor: Adaptor, card: Adaptor) -> dict[str, Any]:
    title = (anchor.text or "").strip()
    company_link = _css_first(card, "a[href*='/company/']")
    company = (
        (company_link.text or "").strip()
        if company_link is not None
        else "Unknown company"
    )
    text = card.get_all_text(separator=" ", strip=True)
    location = _location(card)
    experience_match = _EXPERIENCE.search(text)
    minimum_years = (
        int(experience_match.group(1)) if experience_match else None
    )
    description = _description(card)
    if minimum_years is not None:
        description = (
            f"Requires {minimum_years}+ years of experience. {description}"
        )
    salary = _SALARY.search(text)
    is_remote = bool(
        location and re.search(r"\bremote\b", location, re.IGNORECASE)
    )

    return {
        "title": title,
        "company": company,
        "city": location,
        "state": None,
        "country": "India",
        "job_url": str(anchor.attrib.get("href", "")),
        "description": description,
        "is_remote": is_remote,
        "remote_restriction": None,
        "site": "cutshort",
        "date_posted": None,
        "min_amount": float(salary.group(1)) * 100000 if salary else None,
        "max_amount": float(salary.group(2)) * 100000 if salary else None,
        "currency": "INR" if salary else None,
    }


def scrape(
    settings: dict[str, Any],
    session: requests.Session,
    timeout: int,
) -> Any:
    """Fetch configured public Cutshort search pages.

    Uses Scrapling Fetcher for adaptive scraping.  The ``session`` parameter
    is accepted for API compatibility but not used internally.
    """
    rows: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    max_results = max(1, int(settings.get("max_results_per_page", 20)))
    relocate_events = 0
    for page_url in settings.get("pages") or []:
        page = Fetcher.get(
            str(page_url),
            timeout=timeout,
        )
        added = 0
        for anchor in page.css("a[href]"):
            href = anchor.attrib.get("href")
            if not _is_job_link(href):
                continue
            job_url = str(href)
            if job_url in seen_urls:
                continue
            card = _card_for(anchor)
            if card is None:
                continue
            seen_urls.add(job_url)
            rows.append(_job_row(anchor, card))
            added += 1
            if added >= max_results:
                break
        if added == 0:
            # Record a health event — Scrapling may have failed to locate
            # elements adaptively, or the page structure changed significantly.
            relocate_events += 1
            scrape_state.record_scraper_health_event(
                "cutshort", f"zero_results:{page_url}"
            )
            raise RuntimeError(
                f"Cutshort returned no parseable job cards: {page_url}"
            )
    if relocate_events > 0:
        scrape_state.check_adaptive_degradation("cutshort")
    max_age_days = int(settings.get("max_posting_age_days", 14))
    filtered_rows = [
        r for r in rows if is_within_window(r.get("date_posted"), max_age_days)
    ]
    return frame(filtered_rows)
