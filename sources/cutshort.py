"""Cutshort server-rendered public job-page adapter."""

from __future__ import annotations

import re
from typing import Any

import requests
from bs4 import BeautifulSoup, Tag

from sources.common import frame


_JOB_LINK = re.compile(r"^https?://(?:www\.)?cutshort\.io/job/")
_EXPERIENCE = re.compile(r"(\d+)\s*(?:-|to)\s*(\d+)\s*yrs?", re.IGNORECASE)
_SALARY = re.compile(
    r"₹\s*([\d.]+)\s*L\s*-\s*₹?\s*([\d.]+)\s*L",
    re.IGNORECASE,
)


def _card_for(anchor: Tag) -> Tag | None:
    for parent in anchor.parents:
        if not isinstance(parent, Tag) or parent.name != "div":
            continue
        text = parent.get_text(" ", strip=True)
        job_links = [
            link
            for link in parent.find_all("a", href=True)
            if _JOB_LINK.match(str(link["href"]))
        ]
        if (
            len(job_links) == 1
            and _EXPERIENCE.search(text)
            and parent.find("noscript") is not None
        ):
            return parent
    return None


def _description(card: Tag) -> str:
    descriptions: list[str] = []
    for element in card.find_all("noscript"):
        parsed = BeautifulSoup(element.decode_contents(), "html.parser")
        value = parsed.get_text(" ", strip=True)
        if value:
            descriptions.append(value)
    return max(descriptions, key=len, default="")[:12000]


def _location(card: Tag) -> str | None:
    values = list(card.stripped_strings)
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


def _job_row(anchor: Tag, card: Tag) -> dict[str, Any]:
    title = anchor.get_text(" ", strip=True)
    company_link = card.find(
        "a", href=lambda value: bool(value and "/company/" in value)
    )
    company = (
        company_link.get_text(" ", strip=True)
        if company_link
        else "Unknown company"
    )
    text = card.get_text(" ", strip=True)
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
        "job_url": str(anchor["href"]),
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
    """Fetch configured public Cutshort search pages."""
    rows: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    max_results = max(1, int(settings.get("max_results_per_page", 20)))
    for page_url in settings.get("pages") or []:
        response = session.get(str(page_url), timeout=timeout)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        added = 0
        for anchor in soup.find_all("a", href=_JOB_LINK):
            job_url = str(anchor["href"])
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
            raise RuntimeError(
                f"Cutshort returned no parseable job cards: {page_url}"
            )
    return frame(rows)
