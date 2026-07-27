"""Entrypoint for the scheduled Job Radar scrape."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

import dedupe
import matcher
import notifier
import resume_profile
import scraper
import scrape_state


CONFIG_PATH = Path("config.yaml")


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Load the configured JobSpy searches."""
    with path.open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file) or {}

    if not isinstance(config.get("searches"), list):
        raise ValueError("config.yaml must define searches as a list")
    if not isinstance(config.get("resume"), dict):
        raise ValueError("config.yaml must define resume settings")
    if not isinstance(config.get("scraping"), dict):
        raise ValueError("config.yaml must define scraping settings")
    if not isinstance(config.get("matching"), dict):
        raise ValueError("config.yaml must define matching settings")
    return config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = load_config()
    # Validate before scraping so bad Telegram settings cannot mark jobs as seen.
    notifier.validate_delivery_target()
    resume = config["resume"]
    profile = resume_profile.load_or_refresh(
        resume.get("paths") or resume["path"],
        resume.get("cache_path", "data/resume_profile.json"),
    )

    scraping = config.get("scraping", {})
    selected_searches = scrape_state.select_searches(
        config["searches"], int(scraping.get("searches_per_run", 2))
    )
    logging.info(
        "selected_searches=%s",
        ",".join(str(search.get("name", "unnamed")) for search in selected_searches),
    )
    scraped = scraper.run_all(
        selected_searches,
        cooldown_minutes=int(scraping.get("cooldown_minutes", 120)),
        max_cooldown_minutes=int(scraping.get("max_cooldown_minutes", 720)),
    )
    matching = config.get("matching", {})
    matched = matcher.filter_and_rank(
        scraped,
        profile,
        minimum_score=int(matching.get("minimum_score", 6)),
        excluded_title_terms=list(matching.get("excluded_title_terms", [])),
        seniority_penalty_terms=list(
            matching.get("seniority_penalty_terms", [])
        ),
        maximum_required_years=int(
            matching.get("maximum_required_years", 5)
        ),
        allowed_countries=list(matching.get("allowed_countries", ["India"])),
    )
    new_jobs = dedupe.filter_new(matched)
    all_pending = dedupe.pending_notifications()
    max_alerts = int(matching.get("max_alerts_per_run", 10))
    pending = all_pending.head(max_alerts)
    sent = notifier.send_all(pending, on_sent=dedupe.mark_notified)

    logging.info(
        "scraped=%d matched=%d new=%d pending=%d queued=%d deferred=%d sent=%d",
        len(scraped),
        len(matched),
        len(new_jobs),
        len(all_pending),
        len(pending),
        max(0, len(all_pending) - len(pending)),
        sent,
    )


if __name__ == "__main__":
    main()
