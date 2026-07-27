"""Persistent scraper cooldown and search-rotation state."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DATABASE_PATH = Path("data") / "jobs.db"

_PROVIDER_SCHEMA = """
CREATE TABLE IF NOT EXISTS provider_state (
    provider TEXT PRIMARY KEY,
    blocked_until TEXT,
    consecutive_429 INTEGER NOT NULL DEFAULT 0,
    last_status TEXT,
    last_result_count INTEGER,
    updated_at TEXT NOT NULL
)
"""

_RUNTIME_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _connect() -> sqlite3.Connection:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.execute(_PROVIDER_SCHEMA)
    connection.execute(_RUNTIME_SCHEMA)
    return connection


def blocked_until(provider: str, now: datetime | None = None) -> datetime | None:
    """Return an active provider cooldown, or None when it may be called."""
    current = now or _now()
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT blocked_until FROM provider_state WHERE provider = ?",
            (provider,),
        ).fetchone()
    finally:
        connection.close()

    if not row or not row[0]:
        return None
    cooldown_end = datetime.fromisoformat(row[0])
    return cooldown_end if cooldown_end > current else None


def record_blocked(
    provider: str,
    base_cooldown_minutes: int,
    max_cooldown_minutes: int,
    now: datetime | None = None,
) -> datetime:
    """Persist an exponential provider cooldown after HTTP 429."""
    current = now or _now()
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT consecutive_429 FROM provider_state WHERE provider = ?",
            (provider,),
        ).fetchone()
        consecutive = (int(row[0]) if row else 0) + 1
        cooldown_minutes = min(
            base_cooldown_minutes * (2 ** (consecutive - 1)),
            max_cooldown_minutes,
        )
        cooldown_end = current + timedelta(minutes=cooldown_minutes)
        with connection:
            connection.execute(
                """
                INSERT INTO provider_state
                    (provider, blocked_until, consecutive_429, last_status,
                     last_result_count, updated_at)
                VALUES (?, ?, ?, 'blocked_429', NULL, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    blocked_until = excluded.blocked_until,
                    consecutive_429 = excluded.consecutive_429,
                    last_status = excluded.last_status,
                    last_result_count = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    provider,
                    cooldown_end.isoformat(),
                    consecutive,
                    current.isoformat(),
                ),
            )
    finally:
        connection.close()
    return cooldown_end


def record_success(
    provider: str, result_count: int, now: datetime | None = None
) -> None:
    """Clear provider cooldown state after a successful request."""
    current = now or _now()
    connection = _connect()
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO provider_state
                    (provider, blocked_until, consecutive_429, last_status,
                     last_result_count, updated_at)
                VALUES (?, NULL, 0, 'success', ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    blocked_until = NULL,
                    consecutive_429 = 0,
                    last_status = 'success',
                    last_result_count = excluded.last_result_count,
                    updated_at = excluded.updated_at
                """,
                (provider, result_count, current.isoformat()),
            )
    finally:
        connection.close()


def record_failure(provider: str, status: str, now: datetime | None = None) -> None:
    """Record a non-rate-limit provider failure without starting a cooldown."""
    current = now or _now()
    connection = _connect()
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO provider_state
                    (provider, blocked_until, consecutive_429, last_status,
                     last_result_count, updated_at)
                VALUES (?, NULL, 0, ?, NULL, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    last_status = excluded.last_status,
                    last_result_count = NULL,
                    updated_at = excluded.updated_at
                """,
                (provider, status[:200], current.isoformat()),
            )
    finally:
        connection.close()


def select_searches(
    searches: Sequence[Mapping[str, Any]], searches_per_run: int
) -> list[Mapping[str, Any]]:
    """Round-robin rotating searches while including every always-run search."""
    rotating = [search for search in searches if not search.get("always_run")]
    always_run = [search for search in searches if search.get("always_run")]
    if not rotating or searches_per_run <= 0 or searches_per_run >= len(rotating):
        return list(rotating) + list(always_run)

    connection = _connect()
    try:
        row = connection.execute(
            "SELECT value FROM runtime_state WHERE key = 'search_rotation_cursor'"
        ).fetchone()
        cursor = int(row[0]) % len(rotating) if row else 0
        selected = [
            rotating[(cursor + offset) % len(rotating)]
            for offset in range(searches_per_run)
        ]
        next_cursor = (cursor + searches_per_run) % len(rotating)
        with connection:
            connection.execute(
                """
                INSERT INTO runtime_state (key, value)
                VALUES ('search_rotation_cursor', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(next_cursor),),
            )
    finally:
        connection.close()
    return selected + list(always_run)
