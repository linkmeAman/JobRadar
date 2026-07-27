"""Hacker News monthly Who's Hiring adapter using the official API."""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import requests
from bs4 import BeautifulSoup

from .common import frame


ASK_STORIES_URL = "https://hacker-news.firebaseio.com/v0/askstories.json"
ITEM_URL = "https://hacker-news.firebaseio.com/v0/item/{item_id}.json"
SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
_THREAD_TITLE = re.compile(r"^ask hn:\s*who is hiring\?", re.IGNORECASE)


def _get_item(
    session: requests.Session, item_id: int, timeout: int
) -> dict[str, Any]:
    response = session.get(
        ITEM_URL.format(item_id=item_id), timeout=timeout
    )
    response.raise_for_status()
    return response.json() or {}


def _find_thread(
    session: requests.Session,
    timeout: int,
    thread_id: int | None,
) -> dict[str, Any]:
    if thread_id:
        return _get_item(session, thread_id, timeout)

    try:
        search_response = session.get(
            SEARCH_URL,
            params={
                "query": "Ask HN: Who is hiring?",
                "tags": "story",
                "hitsPerPage": 20,
            },
            timeout=timeout,
        )
        search_response.raise_for_status()
        hits = list((search_response.json() or {}).get("hits") or [])
    except (requests.RequestException, ValueError):
        hits = []
    exact_hits = [
        hit
        for hit in hits
        if _THREAD_TITLE.match(str(hit.get("title", "")))
    ]
    if exact_hits:
        newest = max(
            exact_hits, key=lambda hit: str(hit.get("created_at", ""))
        )
        return _get_item(session, int(newest["objectID"]), timeout)

    # Fallback to the official recent Ask HN list if the search index has not
    # caught up with the current monthly thread.
    response = session.get(ASK_STORIES_URL, timeout=timeout)
    response.raise_for_status()
    story_ids = list(response.json() or [])[:200]
    with ThreadPoolExecutor(max_workers=8) as executor:
        stories = list(
            executor.map(
                lambda item_id: _get_item(session, item_id, timeout),
                story_ids,
            )
        )
    candidates = [
        story
        for story in stories
        if _THREAD_TITLE.match(str(story.get("title", "")))
    ]
    if not candidates:
        raise RuntimeError("current Hacker News Who's Hiring thread not found")
    return max(candidates, key=lambda story: int(story.get("time", 0)))


def _remote_restriction(text: str) -> str | None:
    normalized = " ".join(text.lower().split())
    restricted_patterns = (
        (
            r"(?:\bremote\b[^|\n]{0,20}\b(?:us|usa|united states)\b|"
            r"\b(?:us|usa|united states)\s*(?:only|remote only)\b)",
            "United States",
        ),
        (r"\bmust be (?:based|located) in (?:the )?(?:us|usa|united states)\b", "United States"),
        (r"\bcanada\s*(?:only|remote only)\b", "Canada"),
        (r"\b(?:eu|europe)\s*(?:only|remote only)\b", "Europe"),
        (r"\bamericas?\s*(?:only|remote only)\b", "Americas"),
    )
    for pattern, label in restricted_patterns:
        if re.search(pattern, normalized):
            return label
    return None


def _comment_row(comment: dict[str, Any]) -> dict[str, Any] | None:
    raw_html = str(comment.get("text") or "")
    if not raw_html or comment.get("deleted") or comment.get("dead"):
        return None
    soup = BeautifulSoup(raw_html, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return None

    headline_source = (
        " | ".join(lines[:3]) if len(lines[0]) < 80 else lines[0]
    )
    headline = headline_source[:180]
    parts = [part.strip() for part in headline.split("|") if part.strip()]
    company = (parts[0] if parts else str(comment.get("by", "Unknown")))[:100]
    normalized = text.lower()
    is_remote = bool(re.search(r"\bremote\b", normalized))
    country = "India" if re.search(r"\bindia\b", normalized) else None
    posted = None
    if comment.get("time"):
        posted = datetime.fromtimestamp(
            int(comment["time"]), tz=timezone.utc
        ).isoformat()

    return {
        "title": headline,
        "company": company,
        "city": None,
        "state": None,
        "country": country,
        "job_url": (
            f"https://news.ycombinator.com/item?id={int(comment['id'])}"
        ),
        "description": text[:12000],
        "is_remote": is_remote,
        "remote_restriction": (
            _remote_restriction(text) if is_remote else None
        ),
        "site": "hn_whos_hiring",
        "date_posted": posted,
        "min_amount": None,
        "max_amount": None,
        "currency": None,
    }


def scrape(
    settings: dict[str, Any],
    session: requests.Session,
    timeout: int,
) -> Any:
    """Fetch top-level company posts from the newest monthly thread."""
    configured_thread = settings.get("thread_id")
    thread = _find_thread(
        session,
        timeout,
        int(configured_thread) if configured_thread else None,
    )
    max_comments = max(1, int(settings.get("max_comments", 150)))
    comment_ids = list(thread.get("kids") or [])[:max_comments]
    with ThreadPoolExecutor(max_workers=8) as executor:
        comments = list(
            executor.map(
                lambda item_id: _get_item(session, item_id, timeout),
                comment_ids,
            )
        )
    rows = [
        row for row in (_comment_row(comment) for comment in comments) if row
    ]
    return frame(rows)
