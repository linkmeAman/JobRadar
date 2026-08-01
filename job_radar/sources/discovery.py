"""Google Dork ATS Company Discovery adapter using Google Custom Search API."""

from __future__ import annotations

import logging
import os
import re
from typing import Any
from urllib.parse import urlsplit

import pandas as pd
import requests

from .. import scrape_state, search_generator
from . import ats_apis
from .common import frame


logger = logging.getLogger(__name__)

CSE_URL = "https://www.googleapis.com/customsearch/v1"
ATS_DOMAINS = {
    "greenhouse": "boards.greenhouse.io",
    "lever": "jobs.lever.co",
    "ashby": "jobs.ashbyhq.com",
}


def extract_slug_from_url(url: str) -> tuple[str, str] | None:
    """Extract (provider, slug) from an ATS result URL."""
    if not url:
        return None
    try:
        parsed = urlsplit(url)
        netloc = parsed.netloc.lower()
        path_parts = [p for p in parsed.path.split("/") if p]
        if not path_parts:
            return None

        slug = path_parts[0].lower().strip()
        if not slug:
            return None

        if "greenhouse.io" in netloc:
            if slug in {"embed", "jobs", "get", "api"}:
                return None
            return ("greenhouse", slug)
        elif "lever.co" in netloc:
            if slug in {"parse", "postings"}:
                return None
            return ("lever", slug)
        elif "ashbyhq.com" in netloc:
            if slug in {"api", "posting-api"}:
                return None
            return ("ashby", slug)
    except Exception:
        return None
    return None


def extract_role_terms(config: dict[str, Any]) -> list[str]:
    """Extract unique role search terms from config and search generator."""
    terms: list[str] = []
    searches = config.get("searches") or []
    for s in searches:
        term = s.get("search_term") or s.get("google_search_term")
        if term and isinstance(term, str):
            # Clean boolean operators or long filters for dork role term
            cleaned = re.sub(
                r"\b(OR|AND|fulltime|remote)\b", "", term, flags=re.IGNORECASE
            )
            cleaned = re.sub(r"[()\"']", "", cleaned).strip()
            if cleaned:
                terms.append(cleaned)

    # Fallback to standard terms if none extracted
    if not terms:
        terms = [
            "python backend engineer",
            "golang backend engineer",
            "platform engineer",
        ]

    # Deduplicate and limit terms to budget Google CSE quota
    unique_terms: list[str] = []
    for t in terms:
        normalized = " ".join(t.lower().split())
        if normalized and normalized not in unique_terms:
            unique_terms.append(normalized)
    return unique_terms[:3]


def discover_slugs(
    session: requests.Session,
    config: dict[str, Any],
    timeout: int = 30,
) -> list[tuple[str, str]]:
    """Query Google CSE to discover new ATS company slugs."""
    api_key = os.environ.get("GOOGLE_CSE_KEY")
    cse_id = os.environ.get("GOOGLE_CSE_ID")

    if not api_key or not cse_id:
        logger.warning(
            "GOOGLE_CSE_KEY or GOOGLE_CSE_ID environment variables not set; skipping dork discovery"
        )
        return []

    role_terms = extract_role_terms(config)
    discovered: list[tuple[str, str]] = []

    for provider, domain in ATS_DOMAINS.items():
        for term in role_terms:
            query = f'site:{domain} ("{term}")'
            params = {
                "key": api_key,
                "cx": cse_id,
                "q": query,
                "dateRestrict": "w2",
                "num": 10,
            }
            try:
                response = session.get(CSE_URL, params=params, timeout=timeout)
                if response.status_code != 200:
                    logger.warning(
                        "Google CSE returned HTTP %d for query=%s",
                        response.status_code,
                        query,
                    )
                    continue
                data = response.json() or {}
                items = data.get("items") or []

                for item in items:
                    link = str(item.get("link") or "")
                    result = extract_slug_from_url(link)
                    if not result:
                        continue
                    prov, slug = result
                    if not scrape_state.is_company_discovered(prov, slug):
                        scrape_state.save_discovered_company(prov, slug)
                        logger.info(
                            "Discovered new ATS company provider=%s slug=%s",
                            prov,
                            slug,
                        )
                        discovered.append((prov, slug))
            except Exception as exc:
                logger.warning(
                    "Google CSE search failed for query=%s: %s", query, exc
                )

    return discovered


def scrape(
    settings: dict[str, Any],
    session: requests.Session,
    timeout: int,
) -> pd.DataFrame:
    """Run Google dork discovery and fetch jobs from newly discovered ATS companies."""
    discovered = discover_slugs(session, settings, timeout=timeout)
    if not discovered:
        return frame([])

    max_age_days = int(settings.get("max_posting_age_days", 14))
    rows: list[dict[str, Any]] = []

    for provider, slug in discovered:
        fetcher = ats_apis.FETCHERS.get(provider)
        if not fetcher:
            continue
        try:
            company_jobs = fetcher(
                slug,
                session=session,
                timeout=timeout,
                max_age_days=max_age_days,
            )
            rows.extend(company_jobs)
        except Exception as exc:
            logger.warning(
                "Failed fetching discovered ATS provider=%s slug=%s: %s",
                provider,
                slug,
                exc,
            )

    return frame(rows)
