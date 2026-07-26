"""Entrypoint for the scheduled Job Radar scrape."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

import dedupe
import notifier
import scraper


CONFIG_PATH = Path("config.yaml")


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Load the configured JobSpy searches."""
    with path.open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file) or {}

    if not isinstance(config.get("searches"), list):
        raise ValueError("config.yaml must define searches as a list")
    return config


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    config = load_config()
    # Validate before scraping so bad Telegram settings cannot mark jobs as seen.
    notifier.validate_delivery_target()

    scraped = scraper.run_all(config["searches"])
    new_jobs = dedupe.filter_new(scraped)
    pending = dedupe.pending_notifications()
    sent = notifier.send_all(pending, on_sent=dedupe.mark_notified)

    logging.info(
        "scraped=%d new=%d pending=%d sent=%d",
        len(scraped),
        len(new_jobs),
        len(pending),
        sent,
    )


if __name__ == "__main__":
    main()
