"""Create recoverable SQLite backups for Job Radar state and applications."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import dedupe
from . import storage


def backup_database(
    destination: Path = Path("data/backups"),
    retention_days: int = 14,
    *,
    source: Path | None = None,
    now: datetime | None = None,
) -> Path:
    """Write a consistent timestamped backup and prune old backup files."""
    source = source or dedupe.DATABASE_PATH
    if not source.exists():
        raise FileNotFoundError(f"database not found: {source}")
    if retention_days < 1:
        raise ValueError("retention_days must be at least 1")
    destination.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now(timezone.utc)).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    target = destination / f"jobs-{timestamp}.db"
    temporary = destination / f".{target.name}.tmp"
    source_connection = sqlite3.connect(source)
    target_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(target_connection)
        target_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()
    temporary.replace(target)
    if storage.integrity_check(target) != "ok":
        target.unlink(missing_ok=True)
        raise sqlite3.DatabaseError(f"backup failed integrity check: {target}")

    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    for previous in destination.glob("jobs-*.db"):
        if previous == target:
            continue
        modified = datetime.fromtimestamp(
            previous.stat().st_mtime, tz=timezone.utc
        )
        if modified < cutoff:
            previous.unlink()
    return target


def restore_database(source: Path, destination: Path | None = None) -> Path:
    """Restore a verified backup atomically into the active database path."""
    if not source.is_file():
        raise FileNotFoundError(f"backup not found: {source}")
    if storage.integrity_check(source) != "ok":
        raise sqlite3.DatabaseError(f"backup failed integrity check: {source}")
    target = destination or dedupe.DATABASE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.restore.tmp")
    source_connection = sqlite3.connect(source)
    target_connection = sqlite3.connect(temporary)
    try:
        source_connection.backup(target_connection)
        target_connection.commit()
    finally:
        target_connection.close()
        source_connection.close()
    temporary.replace(target)
    if storage.integrity_check(target) != "ok":
        raise sqlite3.DatabaseError(f"restored database failed integrity check: {target}")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Back up Job Radar SQLite state")
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--retention-days", type=int)
    parser.add_argument("--restore", type=Path)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    if args.restore:
        target = restore_database(args.restore, args.database)
        print(f"database_restored={target}")
        return
    settings: dict[str, object] = {}
    if args.config.exists():
        with args.config.open(encoding="utf-8") as config_file:
            loaded = yaml.safe_load(config_file) or {}
        if isinstance(loaded, dict) and isinstance(loaded.get("backups"), dict):
            settings = loaded["backups"]
    destination = args.destination or Path(
        str(settings.get("path", "data/backups"))
    )
    retention_days = args.retention_days or int(
        settings.get("retention_days", 14)
    )
    if settings.get("enabled", True) is False:
        print("backup_skipped=disabled")
        return
    target = backup_database(destination, retention_days)
    print(f"backup_created={target}")


if __name__ == "__main__":
    main()
