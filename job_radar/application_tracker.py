"""SQLite application tracking and Telegram command handling."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from . import dedupe, notifier, storage


DATABASE_PATH = dedupe.DATABASE_PATH
ACTIVE_STATUSES = {"applied", "screening", "interview"}
TERMINAL_STATUSES = {"offer", "rejected", "withdrawn"}
VALID_STATUSES = ACTIVE_STATUSES | TERMINAL_STATUSES
_UPDATE_OFFSET_KEY = "telegram_application_update_offset"
_REMINDER_CHECK_KEY = "application_last_reminder_check"
_UNSET = object()

def _database_path():
    """Use the dedupe database path as the single source of truth."""
    return dedupe.DATABASE_PATH


@dataclass(frozen=True)
class ApplicationChange:
    job_id: str
    created: bool = False
    status: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _connect() -> sqlite3.Connection:
    database_path = _database_path()
    return storage.connect(database_path)


def _job_snapshot(connection: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT notification_payload FROM seen_jobs WHERE job_id = ?",
        (job_id,),
    ).fetchone()
    if not row or not row[0]:
        return {}
    try:
        value = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def mark_applied(
    job_id_or_prefix: str, *, now: datetime | None = None
) -> ApplicationChange:
    """Track an application once and retain its original application time."""
    job_id = dedupe.resolve_job_id(job_id_or_prefix)
    timestamp = (now or _now()).isoformat()
    connection = _connect()
    try:
        snapshot = _job_snapshot(connection, job_id)
        with connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO applications
                    (job_id, applied_at, status, last_contact,
                     description_snapshot, updated_at)
                VALUES (?, ?, 'applied', ?, ?, ?)
                """,
                (
                    job_id,
                    timestamp,
                    timestamp,
                    snapshot.get("description"),
                    timestamp,
                ),
            )
            if cursor.rowcount == 1:
                connection.execute(
                    """
                    INSERT INTO application_events
                        (job_id, event_type, new_status, occurred_at)
                    VALUES (?, 'applied', 'applied', ?)
                    """,
                    (job_id, timestamp),
                )
    finally:
        connection.close()
    dedupe.record_feedback(job_id, "applied", source="application")
    return ApplicationChange(
        job_id=job_id,
        created=cursor.rowcount == 1,
        status="applied",
    )


def mark_contacted(
    job_id_or_prefix: str, *, now: datetime | None = None
) -> ApplicationChange:
    """Record the latest recruiter or company contact time."""
    job_id = dedupe.resolve_job_id(job_id_or_prefix)
    connection = _connect()
    try:
        with connection:
            cursor = connection.execute(
                """
                UPDATE applications SET last_contact = ?
                WHERE job_id = ?
                """,
                ((now or _now()).isoformat(), job_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    f"job {job_id[:12]} is not tracked; use /applied first"
                )
            status = connection.execute(
                "SELECT status FROM applications WHERE job_id = ?",
                (job_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO application_events
                    (job_id, event_type, occurred_at)
                VALUES (?, 'contacted', ?)
                """,
                (job_id, (now or _now()).isoformat()),
            )
    finally:
        connection.close()
    return ApplicationChange(job_id=job_id, status=str(status))


def set_status(
    job_id_or_prefix: str,
    status: str,
    *,
    now: datetime | None = None,
) -> ApplicationChange:
    """Set an application stage and treat the transition as fresh contact."""
    normalized = status.strip().lower()
    if normalized not in VALID_STATUSES:
        choices = ", ".join(sorted(VALID_STATUSES))
        raise ValueError(f"status must be one of: {choices}")
    job_id = dedupe.resolve_job_id(job_id_or_prefix)
    connection = _connect()
    try:
        with connection:
            old = connection.execute(
                "SELECT status FROM applications WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            cursor = connection.execute(
                """
                UPDATE applications
                SET status = ?, last_contact = ?
                WHERE job_id = ?
                """,
                (normalized, (now or _now()).isoformat(), job_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    f"job {job_id[:12]} is not tracked; use /applied first"
                )
            timestamp = (now or _now()).isoformat()
            connection.execute(
                """
                INSERT INTO application_events
                    (job_id, event_type, old_status, new_status, occurred_at)
                VALUES (?, 'status_changed', ?, ?, ?)
                """,
                (job_id, old[0] if old else None, normalized, timestamp),
            )
    finally:
        connection.close()
    return ApplicationChange(job_id=job_id, status=normalized)


def update_details(
    job_id_or_prefix: str,
    *,
    notes: str | None | object = _UNSET,
    next_follow_up_at: str | None | object = _UNSET,
    interview_at: str | None | object = _UNSET,
    resume_version: str | None | object = _UNSET,
    cover_letter_version: str | None | object = _UNSET,
) -> ApplicationChange:
    """Update optional application details without changing its status."""
    job_id = dedupe.resolve_job_id(job_id_or_prefix)
    fields = {
        "notes": notes,
        "next_follow_up_at": next_follow_up_at,
        "interview_at": interview_at,
        "resume_version": resume_version,
        "cover_letter_version": cover_letter_version,
    }
    updates = [(name, value) for name, value in fields.items() if value is not _UNSET]
    if not updates:
        raise ValueError("provide at least one application detail")
    connection = _connect()
    try:
        with connection:
            columns = ", ".join(f"{name} = ?" for name, _ in updates)
            values = [value for _, value in updates]
            values.extend([(datetime.now(timezone.utc)).isoformat(), job_id])
            cursor = connection.execute(
                f"UPDATE applications SET {columns}, updated_at = ? WHERE job_id = ?",
                values,
            )
            if cursor.rowcount != 1:
                raise ValueError(
                    f"job {job_id[:12]} is not tracked; use /applied first"
                )
            connection.execute(
                """
                INSERT INTO application_events
                    (job_id, event_type, note, occurred_at)
                VALUES (?, 'details_updated', ?, ?)
                """,
                (job_id, notes, datetime.now(timezone.utc).isoformat()),
            )
            status = connection.execute(
                "SELECT status FROM applications WHERE job_id = ?", (job_id,)
            ).fetchone()[0]
    finally:
        connection.close()
    return ApplicationChange(job_id=job_id, status=str(status))


def stale_applications(
    silent_days: int,
    *,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return active applications with no contact inside the threshold."""
    if silent_days <= 0 or not _database_path().exists():
        return []
    connection = _connect()
    try:
        cutoff = ((now or _now()) - timedelta(days=silent_days)).isoformat()
        rows = connection.execute(
            """
            SELECT a.job_id, a.applied_at, a.status, a.last_contact,
                   s.title, s.company, s.notification_payload
            FROM applications a
            JOIN seen_jobs s ON s.job_id = a.job_id
            WHERE a.status IN ('applied', 'screening', 'interview')
              AND a.last_contact <= ?
            ORDER BY a.last_contact, a.applied_at
            """,
            (cutoff,),
        ).fetchall()
    finally:
        connection.close()

    results: list[dict[str, Any]] = []
    for (
        job_id,
        applied_at,
        status,
        last_contact,
        title,
        company,
        payload,
    ) in rows:
        try:
            stored = json.loads(payload or "{}")
        except (json.JSONDecodeError, TypeError):
            stored = {}
        results.append(
            {
                "job_id": job_id,
                "applied_at": applied_at,
                "status": status,
                "last_contact": last_contact,
                "title": stored.get("title") or title,
                "company": stored.get("company") or company,
            }
        )
    return results


def _runtime_value(key: str) -> str | None:
    if not _database_path().exists():
        return None
    connection = _connect()
    try:
        row = connection.execute(
            "SELECT value FROM runtime_state WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row[0])
    finally:
        connection.close()


def _set_runtime_value(key: str, value: str) -> None:
    connection = _connect()
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO runtime_state (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
    finally:
        connection.close()


def update_offset() -> int | None:
    value = _runtime_value(_UPDATE_OFFSET_KEY)
    return None if value is None else int(value)


def _record_update(update_id: int) -> None:
    _set_runtime_value(_UPDATE_OFFSET_KEY, str(update_id + 1))


def reminder_due(
    interval_hours: float, *, now: datetime | None = None
) -> bool:
    value = _runtime_value(_REMINDER_CHECK_KEY)
    if value is None:
        return True
    return (now or _now()) >= (
        datetime.fromisoformat(value) + timedelta(hours=interval_hours)
    )


def _record_reminder_check(*, now: datetime | None = None) -> None:
    _set_runtime_value(_REMINDER_CHECK_KEY, (now or _now()).isoformat())


def _command_reply(text: str) -> str | None:
    parts = text.strip().split()
    if not parts:
        return None
    command = parts[0].split("@", 1)[0].lower()
    try:
        if command == "/applied":
            if len(parts) != 2:
                return "Usage: /applied <job_id>"
            change = mark_applied(parts[1])
            prefix = "Application saved" if change.created else "Already tracked"
            return f"✅ {prefix}: {change.job_id[:12]}"
        if command == "/contacted":
            if len(parts) != 2:
                return "Usage: /contacted <job_id>"
            change = mark_contacted(parts[1])
            return f"✅ Contact updated: {change.job_id[:12]}"
        if command == "/status":
            if len(parts) != 3:
                return "Usage: /status <job_id> <status>"
            change = set_status(parts[1], parts[2])
            return (
                f"✅ Status updated: {change.job_id[:12]} → "
                f"{change.status}"
            )
    except ValueError as exc:
        return f"❌ {exc}"
    return None


def _callback_reply(data: str) -> str | None:
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "jr":
        return None
    action, job_id = parts[1], parts[2]
    try:
        if action == "applied":
            change = mark_applied(job_id)
            prefix = "Application saved" if change.created else "Already tracked"
            return f"{prefix}: {change.job_id[:12]}"
        if action == "contacted":
            change = mark_contacted(job_id)
            return f"Contact updated: {change.job_id[:12]}"
        if action in {"relevant", "irrelevant"}:
            saved = dedupe.record_feedback(job_id, action)
            return f"Feedback saved: {saved[:12]} {action}"
    except ValueError as exc:
        return str(exc)
    return None


def process_telegram_commands() -> int:
    """Apply supported commands from the authorized Telegram chat."""
    _, configured_chat_id = notifier.get_credentials()
    updates = notifier.fetch_updates(offset=update_offset())
    processed = 0
    for update in updates:
        update_id = update.get("update_id")
        if not isinstance(update_id, int):
            continue
        message = update.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        text = message.get("text")
        if chat_id == configured_chat_id and isinstance(text, str):
            reply = _command_reply(text)
            if reply is not None:
                notifier.send_text(reply)
                processed += 1
        callback = update.get("callback_query") or {}
        callback_message = callback.get("message") or {}
        callback_chat_id = str((callback_message.get("chat") or {}).get("id", ""))
        callback_data = callback.get("data")
        callback_id = callback.get("id")
        if (
            callback_chat_id == configured_chat_id
            and isinstance(callback_data, str)
            and isinstance(callback_id, str)
        ):
            reply = _callback_reply(callback_data)
            if reply is not None:
                notifier.answer_callback_query(callback_id, reply)
                processed += 1
        _record_update(update_id)
    return processed


def maybe_send_stale_reminder(
    settings: dict[str, Any], *, now: datetime | None = None
) -> int:
    """Send at most one stale-application summary per configured interval."""
    if not settings.get("enabled", False):
        return 0
    interval = float(settings.get("reminder_interval_hours", 24))
    if not reminder_due(interval, now=now):
        return 0

    silent_days = int(settings.get("stale_after_days", 7))
    stale = stale_applications(silent_days, now=now)
    if not stale:
        _record_reminder_check(now=now)
        return 0

    limit = max(1, int(settings.get("max_reminders_per_message", 10)))
    lines = [
        f"📋 Application follow-up: {len(stale)} silent for "
        f"{silent_days}+ days"
    ]
    for application in stale[:limit]:
        lines.append(
            "• {title} — {company} [{status}] · 🆔 {job_id}".format(
                title=application["title"],
                company=application["company"],
                status=application["status"],
                job_id=application["job_id"][:12],
            )
        )
    if len(stale) > limit:
        lines.append(f"…and {len(stale) - limit} more")
    notifier.send_text("\n".join(lines))
    _record_reminder_check(now=now)
    return len(stale)


def run_automation(settings: dict[str, Any]) -> tuple[int, int]:
    """Process commands and the daily reminder without blocking scraping."""
    if not settings.get("enabled", False):
        return 0, 0
    commands = 0
    reminders = 0
    try:
        commands = process_telegram_commands()
    except (RuntimeError, ValueError, sqlite3.Error) as exc:
        logging.error("application_commands=failed error=%s", exc)
    try:
        reminders = maybe_send_stale_reminder(settings)
    except (RuntimeError, ValueError, sqlite3.Error) as exc:
        logging.error("application_reminder=failed error=%s", exc)
    return commands, reminders
