"""Persistent scraper cooldown and search-rotation state."""

from __future__ import annotations

import sqlite3
import json
import os
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import storage


DATABASE_PATH = Path(os.environ.get("JOB_RADAR_DATABASE_PATH", "data/jobs.db"))

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _connect(*, read_only: bool = False) -> sqlite3.Connection:
    return storage.connect(DATABASE_PATH, read_only=read_only)


def _connect_read_only() -> sqlite3.Connection:
    return storage.connect(DATABASE_PATH, read_only=True)


def blocked_until(
    provider: str,
    now: datetime | None = None,
    *,
    read_only: bool = False,
) -> datetime | None:
    """Return an active provider cooldown, or None when it may be called."""
    current = now or _now()
    if not DATABASE_PATH.exists():
        return None
    connection = _connect_read_only() if read_only else _connect()
    try:
        if read_only and not connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'provider_state'
            """
        ).fetchone():
            return None
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
            """SELECT consecutive_429, consecutive_failures
               FROM provider_state WHERE provider = ?""",
            (provider,),
        ).fetchone()
        consecutive = (int(row[0]) if row else 0) + 1
        failures = (int(row[1]) if row else 0) + 1
        cooldown_minutes = min(
            base_cooldown_minutes * (2 ** (consecutive - 1)),
            max_cooldown_minutes,
        )
        cooldown_end = current + timedelta(minutes=cooldown_minutes)
        with connection:
            connection.execute(
                """
                INSERT INTO provider_state
                    (provider, blocked_until, consecutive_429,
                     consecutive_failures, last_status, last_result_count,
                     updated_at)
                VALUES (?, ?, ?, ?, 'blocked_429', NULL, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    blocked_until = excluded.blocked_until,
                    consecutive_429 = excluded.consecutive_429,
                    consecutive_failures = excluded.consecutive_failures,
                    consecutive_empty_results = 0,
                    last_status = excluded.last_status,
                    last_result_count = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    provider,
                    cooldown_end.isoformat(),
                    consecutive,
                    failures,
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
        previous = connection.execute(
            "SELECT consecutive_empty_results FROM provider_state WHERE provider = ?",
            (provider,),
        ).fetchone()
        empty_results = (
            (int(previous[0]) if previous else 0) + 1
            if result_count == 0
            else 0
        )
        status = "success_empty" if result_count == 0 else "success"
        with connection:
            connection.execute(
                """
                INSERT INTO provider_state
                    (provider, blocked_until, consecutive_429,
                     consecutive_failures, failure_alert_sent,
                     consecutive_empty_results, last_status,
                     last_result_count, updated_at)
                VALUES (?, NULL, 0, 0, 0, ?, ?, ?, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    blocked_until = NULL,
                    consecutive_429 = 0,
                    consecutive_failures = 0,
                    failure_alert_sent = 0,
                    consecutive_empty_results = excluded.consecutive_empty_results,
                    last_status = excluded.last_status,
                    last_result_count = excluded.last_result_count,
                    updated_at = excluded.updated_at
                """,
                (
                    provider,
                    empty_results,
                    status,
                    result_count,
                    current.isoformat(),
                ),
            )
    finally:
        connection.close()


def record_failure(provider: str, status: str, now: datetime | None = None) -> None:
    """Record a non-rate-limit provider failure without starting a cooldown."""
    current = now or _now()
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT consecutive_failures FROM provider_state WHERE provider = ?",
            (provider,),
        ).fetchone()
        failures = (int(row[0]) if row else 0) + 1
        with connection:
            connection.execute(
                """
                INSERT INTO provider_state
                    (provider, blocked_until, consecutive_429,
                     consecutive_failures, last_status, last_result_count,
                     updated_at)
                VALUES (?, NULL, 0, ?, ?, NULL, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    consecutive_failures = excluded.consecutive_failures,
                    consecutive_empty_results = 0,
                    last_status = excluded.last_status,
                    last_result_count = NULL,
                    updated_at = excluded.updated_at
                """,
                (provider, failures, status[:200], current.isoformat()),
            )
    finally:
        connection.close()


def record_cooldown(provider: str, now: datetime | None = None) -> None:
    """Count a provider that remained unavailable during this run."""
    current = now or _now()
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT consecutive_failures FROM provider_state WHERE provider = ?",
            (provider,),
        ).fetchone()
        failures = (int(row[0]) if row else 0) + 1
        with connection:
            connection.execute(
                """
                INSERT INTO provider_state
                    (provider, blocked_until, consecutive_429,
                     consecutive_failures, last_status, last_result_count,
                     updated_at)
                VALUES (?, NULL, 0, ?, 'cooldown', NULL, ?)
                ON CONFLICT(provider) DO UPDATE SET
                    consecutive_failures = excluded.consecutive_failures,
                    consecutive_empty_results = 0,
                    last_status = 'cooldown',
                    last_result_count = NULL,
                    updated_at = excluded.updated_at
                """,
                (provider, failures, current.isoformat()),
            )
    finally:
        connection.close()


def degraded_providers(
    provider_status: Mapping[str, Mapping[str, Any]],
    threshold: int,
) -> list[dict[str, Any]]:
    """Return providers over a consecutive-run failure threshold."""
    if threshold <= 0 or not provider_status:
        return []

    if not DATABASE_PATH.exists():
        return []
    try:
        connection = _connect(read_only=True)
    except sqlite3.OperationalError:
        return []
    try:
        try:
            run_rows = connection.execute(
                """SELECT provider_status FROM scrape_runs
                   WHERE mode = 'normal' AND status IN ('completed', 'failed')
                   ORDER BY run_id DESC"""
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        degraded: list[dict[str, Any]] = []
        for provider, status in provider_status.items():
            if status.get("status") == "scheduled_skip":
                continue
            current_status = str(status.get("status", "unknown"))
            if current_status not in {
                "failure",
                "blocked_429",
                "cooldown",
                "skipped_blocked",
                "success",
            }:
                continue
            failures = 0
            for (serialized,) in run_rows:
                try:
                    history = json.loads(serialized or "{}")
                except (TypeError, json.JSONDecodeError):
                    break
                history_status = history.get(provider, {}).get("status")
                if history_status == "scheduled_skip":
                    break
                if history_status not in {
                    "failure",
                    "blocked_429",
                    "cooldown",
                    "skipped_blocked",
                }:
                    break
                failures += 1
            try:
                row = connection.execute(
                    """SELECT consecutive_failures, failure_alert_sent,
                              consecutive_empty_results
                       FROM provider_state WHERE provider = ?""",
                    (provider,),
                ).fetchone()
            except sqlite3.OperationalError:
                legacy_row = connection.execute(
                    """SELECT consecutive_failures, failure_alert_sent
                       FROM provider_state WHERE provider = ?""",
                    (provider,),
                ).fetchone()
                row = (*legacy_row, 0) if legacy_row else None
            if not row:
                continue
            alert_sent = bool(row[1])
            empty_results = int(row[2] or 0)
            if (
                current_status == "success"
                and empty_results >= threshold
                and not alert_sent
            ):
                degraded.append(
                    {
                        "provider": provider,
                        "failures": empty_results,
                        "status": "success_empty",
                        "error": "successful runs returned zero listings",
                    }
                )
                continue
            if failures >= threshold and not alert_sent:
                degraded.append(
                    {
                        "provider": provider,
                        "failures": failures,
                        "status": status.get("status", "unknown"),
                        "error": status.get("error")
                        or (status.get("errors") or [None])[-1],
                    }
                )
        return degraded
    finally:
        connection.close()


def mark_provider_alert_sent(providers: Sequence[str]) -> None:
    """Suppress duplicate alerts until each provider succeeds again."""
    if not providers:
        return
    connection = _connect()
    try:
        with connection:
            connection.executemany(
                "UPDATE provider_state SET failure_alert_sent = 1 WHERE provider = ?",
                [(provider,) for provider in providers],
            )
    finally:
        connection.close()


def select_searches(
    searches: Sequence[Mapping[str, Any]],
    searches_per_run: int,
    *,
    advance: bool = True,
) -> list[Mapping[str, Any]]:
    """Round-robin rotating searches while including every always-run search."""
    rotating = [search for search in searches if not search.get("always_run")]
    always_run = [search for search in searches if search.get("always_run")]
    if not rotating or searches_per_run <= 0 or searches_per_run >= len(rotating):
        return list(rotating) + list(always_run)

    if not advance and not DATABASE_PATH.exists():
        cursor = 0
        selected = [
            rotating[(cursor + offset) % len(rotating)]
            for offset in range(searches_per_run)
        ]
        return selected + list(always_run)

    connection = _connect_read_only() if not advance else _connect()
    try:
        if not advance and not connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'runtime_state'
            """
        ).fetchone():
            cursor = 0
            selected = [
                rotating[(cursor + offset) % len(rotating)]
                for offset in range(searches_per_run)
            ]
            return selected + list(always_run)
        row = connection.execute(
            "SELECT value FROM runtime_state WHERE key = 'search_rotation_cursor'"
        ).fetchone()
        cursor = int(row[0]) % len(rotating) if row else 0
        selected = [
            rotating[(cursor + offset) % len(rotating)]
            for offset in range(searches_per_run)
        ]
        if advance:
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


def source_due(
    source: str,
    interval_hours: float,
    *,
    now: datetime | None = None,
    read_only: bool = False,
) -> bool:
    """Return whether a low-frequency external source should run."""
    if interval_hours <= 0 or not DATABASE_PATH.exists():
        return True
    connection = _connect_read_only() if read_only else _connect()
    key = f"external_source_last_attempt:{source}"
    try:
        if read_only and not connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'runtime_state'
            """
        ).fetchone():
            return True
        row = connection.execute(
            "SELECT value FROM runtime_state WHERE key = ?", (key,)
        ).fetchone()
    finally:
        connection.close()
    if not row:
        return True
    last_attempt = datetime.fromisoformat(row[0])
    return (now or _now()) >= last_attempt + timedelta(hours=interval_hours)


def record_source_attempt(
    source: str, *, now: datetime | None = None
) -> None:
    """Persist the attempt time used to enforce source-specific intervals."""
    connection = _connect()
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO runtime_state (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (
                    f"external_source_last_attempt:{source}",
                    (now or _now()).isoformat(),
                ),
            )
    finally:
        connection.close()


def start_run(
    selected_searches: Sequence[Mapping[str, Any]], mode: str = "normal"
) -> int:
    """Create an auditable run record and return its identifier."""
    names = [str(search.get("name", "unnamed")) for search in selected_searches]
    connection = _connect()
    try:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO scrape_runs (started_at, mode, selected_searches)
                VALUES (?, ?, ?)
                """,
                (_now().isoformat(), mode, json.dumps(names)),
            )
            return int(cursor.lastrowid)
    finally:
        connection.close()


def complete_run(
    run_id: int,
    *,
    started_at: datetime,
    provider_status: Mapping[str, Any],
    scraped_count: int,
    matched_count: int,
    new_count: int,
    pending_count: int,
    queued_count: int,
    deferred_count: int,
    expired_count: int,
    sent_count: int,
    all_providers_failed: bool,
    status: str = "completed",
    error: str | None = None,
) -> None:
    """Finalize a run with counts and provider-level outcomes."""
    completed_at = _now()
    duration = max(0.0, (completed_at - started_at).total_seconds())
    connection = _connect()
    try:
        with connection:
            connection.execute(
                """
                UPDATE scrape_runs
                SET completed_at = ?, duration_seconds = ?, provider_status = ?,
                    scraped_count = ?, matched_count = ?, new_count = ?,
                    pending_count = ?, queued_count = ?, deferred_count = ?,
                    expired_count = ?, sent_count = ?,
                    all_providers_failed = ?, status = ?, error = ?
                WHERE run_id = ?
                """,
                (
                    completed_at.isoformat(),
                    duration,
                    json.dumps(provider_status, default=str, sort_keys=True),
                    scraped_count,
                    matched_count,
                    new_count,
                    pending_count,
                    queued_count,
                    deferred_count,
                    expired_count,
                    sent_count,
                    int(all_providers_failed),
                    status,
                    error[:1000] if error else None,
                    run_id,
                ),
            )
    finally:
        connection.close()


def consecutive_all_failed_runs() -> int:
    """Count consecutive completed normal runs where no provider succeeded."""
    if not DATABASE_PATH.exists():
        return 0
    connection = _connect()
    try:
        rows = connection.execute(
            """
            SELECT all_providers_failed
            FROM scrape_runs
            WHERE mode = 'normal' AND status IN ('completed', 'failed')
            ORDER BY run_id DESC
            """
        ).fetchall()
    finally:
        connection.close()

    count = 0
    for (all_failed,) in rows:
        if not all_failed:
            break
        count += 1
    return count


def mark_health_alert_sent(run_id: int) -> None:
    """Record successful delivery of the health warning for a run."""
    connection = _connect()
    try:
        with connection:
            connection.execute(
                """
                UPDATE scrape_runs
                SET health_alert_sent = 1
                WHERE run_id = ?
                """,
                (run_id,),
            )
    finally:
        connection.close()


def current_failure_streak_alerted() -> bool:
    """Return whether the current all-failed streak already emitted an alert."""
    if not DATABASE_PATH.exists():
        return False
    connection = _connect()
    try:
        rows = connection.execute(
            """
            SELECT all_providers_failed, health_alert_sent
            FROM scrape_runs
            WHERE mode = 'normal' AND status IN ('completed', 'failed')
            ORDER BY run_id DESC
            """
        ).fetchall()
    finally:
        connection.close()
    for all_failed, alerted in rows:
        if not all_failed:
            break
        if alerted:
            return True
    return False
