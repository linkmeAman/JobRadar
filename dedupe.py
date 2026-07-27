"""SQLite-backed deduplication for Job Radar listings."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd


DATABASE_PATH = Path("data") / "jobs.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_jobs (
    job_id TEXT PRIMARY KEY,
    title TEXT,
    company TEXT,
    site TEXT,
    first_seen_at TIMESTAMP,
    notification_payload TEXT,
    notified_at TIMESTAMP
)
"""

_NOTIFICATION_FIELDS = (
    "title",
    "company",
    "city",
    "state",
    "job_url",
    "min_amount",
    "max_amount",
    "currency",
    "is_remote",
    "match_score",
    "match_reasons",
)


def _text(value: Any) -> str:
    """Use an empty string for null DataFrame values when building a hash."""
    return "" if pd.isna(value) else str(value)


def _job_id(title: Any, company: Any, site: Any) -> str:
    value = f"{_text(title)}{_text(company)}{_text(site)}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _notification_payload(row: pd.Series) -> str:
    """Serialize the fields needed to retry a Telegram notification later."""
    payload = {
        field: None if not _has_value(row.get(field)) else row.get(field)
        for field in _NOTIFICATION_FIELDS
    }
    return json.dumps(payload, default=str, allow_nan=False)


def _has_value(value: Any) -> bool:
    return value is not None and not pd.isna(value)


def _initialize_schema(connection: sqlite3.Connection) -> None:
    """Create the table and migrate databases created by earlier versions."""
    connection.execute(_SCHEMA)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(seen_jobs)")
    }
    if "notification_payload" not in columns:
        connection.execute("ALTER TABLE seen_jobs ADD COLUMN notification_payload TEXT")
    if "notified_at" not in columns:
        connection.execute("ALTER TABLE seen_jobs ADD COLUMN notified_at TIMESTAMP")


def filter_new(df: pd.DataFrame) -> pd.DataFrame:
    """Return listings not seen before and persist them as seen.

    The database path is always ``data/jobs.db`` relative to the process
    working directory, which is set to the project directory by systemd.
    """
    if df.empty:
        return df.copy()

    required = {"title", "company", "site"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(f"Cannot dedupe jobs without columns: {', '.join(sorted(missing))}")

    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_positions: list[int] = []

    connection = sqlite3.connect(DATABASE_PATH)
    try:
        _initialize_schema(connection)

        with connection:
            for position, (_, row) in enumerate(df.iterrows()):
                title = _text(row["title"])
                company = _text(row["company"])
                site = _text(row["site"])
                job_id = _job_id(title, company, site)
                payload = _notification_payload(row)
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO seen_jobs
                        (job_id, title, company, site, first_seen_at, notification_payload)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                    """,
                    (job_id, title, company, site, payload),
                )
                if cursor.rowcount == 1:
                    new_positions.append(position)
                else:
                    # Backfill older records and refresh pending jobs for retry.
                    connection.execute(
                        """
                        UPDATE seen_jobs
                        SET notification_payload = ?
                        WHERE job_id = ? AND notified_at IS NULL
                        """,
                        (payload, job_id),
                    )
    finally:
        connection.close()

    return df.iloc[new_positions].copy()


def pending_notifications(limit: int | None = None) -> pd.DataFrame:
    """Return pending alerts from strongest to weakest resume match."""
    if not DATABASE_PATH.exists():
        return pd.DataFrame()

    connection = sqlite3.connect(DATABASE_PATH)
    try:
        _initialize_schema(connection)
        rows = connection.execute(
            """
            SELECT job_id, notification_payload
            FROM seen_jobs
            WHERE notified_at IS NULL AND notification_payload IS NOT NULL
            ORDER BY first_seen_at, rowid
            """
        ).fetchall()
    finally:
        connection.close()

    notifications = []
    for job_id, payload in rows:
        notification = json.loads(payload)
        notification["job_id"] = job_id
        notifications.append(notification)
    pending = pd.DataFrame(notifications)
    if pending.empty:
        return pending
    if "match_score" in pending.columns:
        pending["_sort_score"] = pd.to_numeric(
            pending["match_score"], errors="coerce"
        ).fillna(-1)
        pending = pending.sort_values(
            "_sort_score", ascending=False, kind="stable"
        ).drop(columns="_sort_score")
    if limit is not None:
        pending = pending.head(limit)
    return pending.reset_index(drop=True)


def mark_notified(job_id: str) -> None:
    """Set the delivery flag after Telegram accepts an individual message."""
    connection = sqlite3.connect(DATABASE_PATH)
    try:
        with connection:
            connection.execute(
                """
                UPDATE seen_jobs
                SET notified_at = CURRENT_TIMESTAMP
                WHERE job_id = ?
                """,
                (job_id,),
            )
    finally:
        connection.close()
