"""Schedule and isolate external public job-source adapters."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd
import requests

from .. import scrape_state
from ..scraper import ScrapeOutcome
from . import ats_apis, cutshort, discovery, hacker_news, hirist
from .common import frame


Adapter = Callable[[dict[str, Any], requests.Session, int], pd.DataFrame]
ADAPTERS: dict[str, Adapter] = {
    "hn_whos_hiring": hacker_news.scrape,
    "cutshort": cutshort.scrape,
    "hirist": hirist.scrape,
    "ats_apis": ats_apis.scrape,
    "discovery": discovery.scrape,
}


def configured_names(config: dict[str, Any]) -> list[str]:
    sources = config.get("sources") or {}
    return [
        name
        for name in ADAPTERS
        if isinstance(sources.get(name), dict)
        and sources[name].get("enabled", False)
    ]


def run_all(
    config: dict[str, Any],
    *,
    persist_state: bool,
    force: bool = False,
) -> ScrapeOutcome:
    """Run due sources independently and merge JobSpy-compatible rows."""
    results: list[pd.DataFrame] = []
    status: dict[str, dict[str, Any]] = {}
    if not config.get("enabled", False):
        return ScrapeOutcome(pd.DataFrame(), status)

    timeout = int(config.get("timeout_seconds", 30))
    user_agent = str(
        config.get(
            "user_agent",
            "JobRadar/1.0 personal job search",
        )
    )
    session = requests.Session()
    session.headers.update({"User-Agent": user_agent})
    base_cooldown = int(config.get("cooldown_minutes", 120))
    max_cooldown = int(config.get("max_cooldown_minutes", 720))

    max_posting_age_days = int(config.get("max_posting_age_days", 14))

    for name in configured_names(config):
        settings = dict(config["sources"][name])
        settings.setdefault("max_posting_age_days", max_posting_age_days)
        if "ats_companies" in config and "ats_companies" not in settings:
            settings["ats_companies"] = config["ats_companies"]
        if "searches" in config and "searches" not in settings:
            settings["searches"] = config["searches"]
        interval = float(settings.get("interval_hours", 6))
        if not force and not scrape_state.source_due(
            name, interval, read_only=not persist_state
        ):
            status[name] = {
                "status": "scheduled_skip",
                "results": 0,
                "interval_hours": interval,
            }
            print(f"source={name} skipped=interval")
            continue

        cooldown_end = scrape_state.blocked_until(
            name, read_only=not persist_state
        )
        if cooldown_end is not None:
            status[name] = {
                "status": "cooldown",
                "results": 0,
                "cooldown_until": cooldown_end.isoformat(),
            }
            print(
                f"source={name} skipped=cooldown "
                f"until={cooldown_end.isoformat()}"
            )
            continue

        if persist_state:
            scrape_state.record_source_attempt(name)
        try:
            jobs = ADAPTERS[name](settings, session, timeout)
            if not jobs.empty:
                results.append(jobs)
            status[name] = {
                "status": "success",
                "results": len(jobs),
                "interval_hours": interval,
            }
            if persist_state:
                scrape_state.record_success(name, len(jobs))
            print(f"source={name} scraped={len(jobs)}")
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            response = getattr(exc, "response", None)
            response_status = getattr(response, "status_code", None)
            if response_status == 429 or "429" in str(exc):
                cooldown_end = None
                if persist_state:
                    cooldown_end = scrape_state.record_blocked(
                        name, base_cooldown, max_cooldown
                    )
                status[name] = {
                    "status": "blocked_429",
                    "results": 0,
                    "error": message[:200],
                    "cooldown_until": (
                        cooldown_end.isoformat() if cooldown_end else None
                    ),
                }
            else:
                if persist_state:
                    scrape_state.record_failure(name, message)
                status[name] = {
                    "status": "failure",
                    "results": 0,
                    "error": message[:200],
                }
            print(f"source={name} failed={message}")

    records = [
        record
        for result in results
        for record in result.to_dict(orient="records")
    ]
    jobs = frame(records) if records else pd.DataFrame()
    if not jobs.empty and "job_url" in jobs.columns:
        jobs = jobs.drop_duplicates(
            subset=["job_url"], keep="first"
        ).reset_index(drop=True)
    return ScrapeOutcome(jobs=jobs, provider_status=status)
