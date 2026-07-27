"""Long-running Docker scheduler for scraping and nightly backups."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from . import backup
from . import main as pipeline


def _positive_seconds(name: str, default: int) -> int:
    value = int(os.environ.get(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return value


def run_forever() -> None:
    """Run the pipeline on a fixed cadence until the container is stopped."""
    interval = _positive_seconds("JOB_RADAR_INTERVAL_SECONDS", 1800)
    backup_interval = _positive_seconds(
        "JOB_RADAR_BACKUP_INTERVAL_SECONDS", 86400
    )
    backup_path = Path(os.environ.get("JOB_RADAR_BACKUP_PATH", "data/backups"))
    backup_retention = int(os.environ.get("JOB_RADAR_BACKUP_RETENTION_DAYS", "14"))
    logging.info(
        "docker_scheduler=started interval_seconds=%d backup_interval_seconds=%d",
        interval,
        backup_interval,
    )
    next_backup = 0.0
    while True:
        started = time.monotonic()
        try:
            pipeline.run(mode="docker_scheduler")
        except Exception:
            logging.exception("docker_scheduler=scrape_failed")

        now = time.monotonic()
        if now >= next_backup:
            try:
                target = backup.backup_database(
                    backup_path, backup_retention
                )
                logging.info("docker_scheduler=backup_created path=%s", target)
            except Exception:
                logging.exception("docker_scheduler=backup_failed")
            next_backup = now + backup_interval

        elapsed = time.monotonic() - started
        time.sleep(max(1, interval - elapsed))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    run_forever()


if __name__ == "__main__":
    main()
