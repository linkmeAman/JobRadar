"""SQLite-backed deduplication, delivery state, and relevance feedback."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import pandas as pd

from . import storage


DATABASE_PATH = Path(os.environ.get("JOB_RADAR_DATABASE_PATH", "data/jobs.db"))

_NOTIFICATION_FIELDS = (
    "title",
    "company",
    "city",
    "state",
    "country",
    "job_url",
    "min_amount",
    "max_amount",
    "currency",
    "is_remote",
    "description",
    "match_score",
    "match_reasons",
)

_TRACKING_QUERY_KEYS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "trk",
    "trackingid",
    "ref",
    "refid",
}
_COMPANY_SUFFIXES = re.compile(
    r"\b(incorporated|corporation|company|technologies|technology|limited|private|pvt|ltd|llc|inc|corp)\b"
)


def _text(value: Any) -> str:
    return "" if pd.isna(value) else str(value)


def normalize_company(value: Any) -> str:
    """Normalize common legal suffixes for company history and matching."""
    text = re.sub(r"[^a-z0-9]+", " ", _text(value).lower()).strip()
    return re.sub(r"\s+", " ", _COMPANY_SUFFIXES.sub(" ", text)).strip()


def _job_id(title: Any, company: Any, site: Any) -> str:
    """Return the legacy title/company/site identity."""
    value = f"{_text(title)}{_text(company)}{_text(site)}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_job_url(value: Any) -> str:
    """Normalize a job URL while retaining provider-specific identity fields."""
    if value is None or pd.isna(value) or not str(value).strip():
        return ""
    raw = str(value).strip()
    parsed = urlsplit(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw.rstrip("/")
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_QUERY_KEYS
        and not key.lower().startswith("utm_")
    ]
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path.rstrip("/") or "/",
            urlencode(sorted(query)),
            "",
        )
    )


def job_id_for(row: pd.Series) -> str:
    """Prefer normalized URL identity, falling back to the legacy hash."""
    normalized_url = normalize_job_url(row.get("job_url"))
    if normalized_url:
        return hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
    return _job_id(row.get("title"), row.get("company"), row.get("site"))


def _notification_payload(row: pd.Series) -> str:
    payload = {
        field: None if not _has_value(row.get(field)) else row.get(field)
        for field in _NOTIFICATION_FIELDS
    }
    return json.dumps(payload, default=str, allow_nan=False)


def _has_value(value: Any) -> bool:
    return value is not None and not pd.isna(value)


def _initialize_schema(connection: sqlite3.Connection) -> None:
    storage.migrate(connection)


def _connect(*, read_only: bool = False) -> sqlite3.Connection:
    return storage.connect(DATABASE_PATH, read_only=read_only)


def _record_duplicate(
    connection: sqlite3.Connection, job_id: str, identity_type: str
) -> None:
    connection.execute(
        """
        INSERT INTO dedupe_events(job_id, identity_type, detected_at)
        VALUES (?, ?, ?)
        """,
        (job_id, identity_type, datetime.now(timezone.utc).isoformat()),
    )


def _legacy_matches_current_url(
    existing_payload: str | None, current_url: str
) -> bool:
    if not existing_payload:
        return True
    try:
        existing_url = normalize_job_url(
            json.loads(existing_payload).get("job_url")
        )
    except (json.JSONDecodeError, TypeError):
        return True
    return not existing_url or existing_url == current_url


def seen_status(df: pd.DataFrame) -> pd.Series:
    """Return read-only seen flags without creating or changing the database."""
    if df.empty:
        return pd.Series([], index=df.index, dtype=bool)
    if not DATABASE_PATH.exists():
        return pd.Series(False, index=df.index, dtype=bool)

    connection = _connect(read_only=True)
    try:
        table = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'seen_jobs'
            """
        ).fetchone()
        if not table:
            return pd.Series(False, index=df.index, dtype=bool)
        rows = connection.execute(
            """
            SELECT job_id, notification_payload, title, company, site
            FROM seen_jobs
            """
        ).fetchall()
    finally:
        connection.close()
    existing = {job_id: payload for job_id, payload, _, _, _ in rows}
    legacy_keys = {
        _job_id(title, company, site)
        for _, _, title, company, site in rows
    }

    flags: list[bool] = []
    for _, row in df.iterrows():
        primary_id = job_id_for(row)
        legacy_id = _job_id(row.get("title"), row.get("company"), row.get("site"))
        if primary_id in existing:
            flags.append(True)
            continue
        normalized_url = normalize_job_url(row.get("job_url"))
        if not normalized_url and legacy_id in legacy_keys:
            flags.append(True)
            continue
        flags.append(
            legacy_id in existing
            and (
                not normalized_url
                or _legacy_matches_current_url(
                    existing[legacy_id], normalized_url
                )
            )
        )
    return pd.Series(flags, index=df.index, dtype=bool)


def filter_new(df: pd.DataFrame) -> pd.DataFrame:
    """Persist unseen listings, migrating compatible legacy IDs without resend."""
    if df.empty:
        return df.copy()

    required = {"title", "company", "site"}
    missing = required.difference(df.columns)
    if missing:
        raise ValueError(
            f"Cannot dedupe jobs without columns: {', '.join(sorted(missing))}"
        )

    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    new_positions: list[int] = []
    connection = _connect()
    try:
        _initialize_schema(connection)
        with connection:
            for position, (_, row) in enumerate(df.iterrows()):
                title = _text(row["title"])
                company = _text(row["company"])
                site = _text(row["site"])
                legacy_id = _job_id(title, company, site)
                primary_id = job_id_for(row)
                normalized_url = normalize_job_url(row.get("job_url"))
                payload = _notification_payload(row)

                existing = connection.execute(
                    """
                    SELECT notification_payload FROM seen_jobs
                    WHERE job_id = ?
                    """,
                    (primary_id,),
                ).fetchone()
                if existing:
                    _record_duplicate(
                        connection,
                        primary_id,
                        "url" if normalized_url else "legacy",
                    )
                    connection.execute(
                        """
                        UPDATE seen_jobs
                        SET notification_payload = ?,
                            normalized_company = COALESCE(normalized_company, ?)
                        WHERE job_id = ? AND notified_at IS NULL
                            AND expired_at IS NULL
                        """,
                        (payload, normalize_company(company), primary_id),
                    )
                    continue

                if not normalized_url:
                    fallback = connection.execute(
                        """
                        SELECT job_id FROM seen_jobs
                        WHERE title = ? AND company = ? AND site = ?
                        LIMIT 1
                        """,
                        (title, company, site),
                    ).fetchone()
                    if fallback:
                        _record_duplicate(connection, fallback[0], "legacy")
                        connection.execute(
                            """
                            UPDATE seen_jobs
                            SET notification_payload = ?,
                                normalized_company = COALESCE(normalized_company, ?)
                            WHERE job_id = ? AND notified_at IS NULL
                                AND expired_at IS NULL
                            """,
                            (payload, normalize_company(company), fallback[0]),
                        )
                        continue

                legacy = None
                if primary_id != legacy_id:
                    legacy = connection.execute(
                        """
                        SELECT notification_payload FROM seen_jobs
                        WHERE job_id = ?
                        """,
                        (legacy_id,),
                    ).fetchone()
                if (
                    legacy
                    and normalized_url
                    and _legacy_matches_current_url(legacy[0], normalized_url)
                ):
                    connection.execute(
                        """
                        UPDATE seen_jobs
                        SET job_id = ?,
                            normalized_company = ?,
                            notification_payload = CASE
                                WHEN notified_at IS NULL AND expired_at IS NULL
                                THEN ? ELSE notification_payload END
                        WHERE job_id = ?
                        """,
                        (primary_id, normalize_company(company), payload, legacy_id),
                    )
                    connection.execute(
                        """
                        UPDATE job_feedback SET job_id = ? WHERE job_id = ?
                        """,
                        (primary_id, legacy_id),
                    )
                    applications_table = connection.execute(
                        """
                        SELECT 1 FROM sqlite_master
                        WHERE type = 'table' AND name = 'applications'
                        """
                    ).fetchone()
                    if applications_table:
                        connection.execute(
                            """
                            UPDATE applications SET job_id = ?
                            WHERE job_id = ?
                            """,
                            (primary_id, legacy_id),
                        )
                    _record_duplicate(connection, primary_id, "legacy_migrated")
                    continue

                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO seen_jobs
                        (job_id, title, company, site, first_seen_at,
                         notification_payload, normalized_company)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?)
                    """,
                    (
                        primary_id,
                        title,
                        company,
                        site,
                        payload,
                        normalize_company(company),
                    ),
                )
                if cursor.rowcount == 1:
                    new_positions.append(position)
    finally:
        connection.close()

    return df.iloc[new_positions].copy()


def pending_notifications(limit: int | None = None) -> pd.DataFrame:
    """Return non-expired pending alerts from strongest to weakest match."""
    if not DATABASE_PATH.exists():
        return pd.DataFrame()

    connection = _connect()
    try:
        _initialize_schema(connection)
        rows = connection.execute(
            """
            SELECT job_id, notification_payload
            FROM seen_jobs
            WHERE notified_at IS NULL
                AND expired_at IS NULL
                AND is_active = 1
                AND notification_payload IS NOT NULL
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


def aman_os_sync_candidates(limit: int = 100) -> pd.DataFrame:
    """Return persisted roles not yet synchronized with Aman OS.

    A payload hash makes changed, still-active source records eligible again
    without creating a second Job Radar identity or resending Telegram alerts.
    """
    if limit <= 0 or not DATABASE_PATH.exists():
        return pd.DataFrame()

    connection = _connect()
    try:
        _initialize_schema(connection)
        rows = connection.execute(
            """
            SELECT s.job_id, s.notification_payload, sync.payload_hash
            FROM seen_jobs AS s
            LEFT JOIN aman_os_sync AS sync ON sync.job_id = s.job_id
            WHERE s.is_active = 1
              AND s.notification_payload IS NOT NULL
            ORDER BY s.first_seen_at, s.rowid
            """,
        ).fetchall()
    finally:
        connection.close()

    candidates = []
    for job_id, payload, stored_hash in rows:
        payload_hash = hashlib.sha256(str(payload).encode("utf-8")).hexdigest()
        if stored_hash == payload_hash:
            continue
        try:
            record = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        record["job_id"] = job_id
        record["aman_os_payload_hash"] = payload_hash
        candidates.append(record)
        if len(candidates) >= limit:
            break
    return pd.DataFrame(candidates)


def mark_aman_os_synced(job_ids: list[str], payload_hashes: dict[str, str]) -> None:
    """Persist successful Aman OS delivery without storing credentials."""
    if not job_ids:
        return
    connection = _connect()
    try:
        _initialize_schema(connection)
        with connection:
            connection.executemany(
                """
                INSERT INTO aman_os_sync
                    (job_id, payload_hash, synced_at, last_attempt_at, last_error)
                VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL)
                ON CONFLICT(job_id) DO UPDATE SET
                    payload_hash = excluded.payload_hash,
                    synced_at = CURRENT_TIMESTAMP,
                    last_attempt_at = CURRENT_TIMESTAMP,
                    last_error = NULL
                """,
                [(job_id, payload_hashes[job_id]) for job_id in job_ids],
            )
    finally:
        connection.close()


def record_aman_os_sync_error(job_ids: list[str], error: str) -> None:
    """Record a bounded delivery error while retaining every candidate to retry."""
    if not job_ids:
        return
    connection = _connect()
    try:
        _initialize_schema(connection)
        with connection:
            connection.executemany(
                """
                INSERT INTO aman_os_sync
                    (job_id, payload_hash, synced_at, last_attempt_at, last_error)
                VALUES (?, '', '', CURRENT_TIMESTAMP, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    last_attempt_at = CURRENT_TIMESTAMP,
                    last_error = excluded.last_error
                """,
                [(job_id, error[:500]) for job_id in job_ids],
            )
    finally:
        connection.close()


def expire_pending(days: int) -> int:
    """Expire undelivered jobs older than the configured retry window."""
    if days <= 0 or not DATABASE_PATH.exists():
        return 0
    connection = _connect()
    try:
        _initialize_schema(connection)
        with connection:
            cursor = connection.execute(
                """
                UPDATE seen_jobs
                SET expired_at = CURRENT_TIMESTAMP
                WHERE notified_at IS NULL AND expired_at IS NULL
                    AND is_active = 1
                    AND first_seen_at < datetime('now', ?)
                """,
                (f"-{days} days",),
            )
            return int(cursor.rowcount)
    finally:
        connection.close()


def mark_notified(job_id: str) -> None:
    connection = _connect()
    try:
        with connection:
            connection.execute(
                """
                UPDATE seen_jobs SET notified_at = CURRENT_TIMESTAMP
                WHERE job_id = ?
                """,
                (job_id,),
            )
    finally:
        connection.close()


def set_active(job_id_or_prefix: str, active: bool) -> str:
    """Enable or disable a stored listing from the pending queue."""
    job_id = resolve_job_id(job_id_or_prefix)
    connection = _connect()
    try:
        with connection:
            cursor = connection.execute(
                "UPDATE seen_jobs SET is_active = ? WHERE job_id = ?",
                (1 if active else 0, job_id),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"no job matches ID {job_id_or_prefix}")
    finally:
        connection.close()
    return job_id


def resolve_job_id(job_id_or_prefix: str) -> str:
    """Resolve one exact or uniquely prefixed ID from the seen jobs table."""
    if not DATABASE_PATH.exists():
        raise ValueError("no Job Radar database exists yet")
    normalized = job_id_or_prefix.strip().lower()
    if not re.fullmatch(r"[0-9a-f]{6,64}", normalized):
        raise ValueError("job ID must contain 6-64 hexadecimal characters")

    connection = _connect()
    try:
        _initialize_schema(connection)
        rows = connection.execute(
            """
            SELECT job_id FROM seen_jobs
            WHERE job_id = ? OR job_id LIKE ?
            ORDER BY CASE WHEN job_id = ? THEN 0 ELSE 1 END
            LIMIT 2
            """,
            (normalized, f"{normalized}%", normalized),
        ).fetchall()
    finally:
        connection.close()
    if not rows:
        raise ValueError(f"no job matches ID {normalized}")
    exact = [row[0] for row in rows if row[0] == normalized]
    if not exact and len(rows) > 1:
        raise ValueError("job ID prefix is ambiguous; provide more characters")
    return exact[0] if exact else rows[0][0]


def record_feedback(
    job_id_or_prefix: str, label: str, *, source: str = "manual"
) -> str:
    """Attach a relevance label to one exact or uniquely prefixed job ID."""
    if label not in {"relevant", "irrelevant", "applied"}:
        raise ValueError("feedback must be relevant, irrelevant, or applied")

    job_id = resolve_job_id(job_id_or_prefix)
    connection = _connect()
    try:
        _initialize_schema(connection)
        stored = connection.execute(
            """
            SELECT notification_payload, normalized_company, company
            FROM seen_jobs WHERE job_id = ?
            """,
            (job_id,),
        ).fetchone()
        payload: dict[str, Any] = {}
        if stored and stored[0]:
            try:
                payload = json.loads(stored[0])
            except (TypeError, json.JSONDecodeError):
                payload = {}
        timestamp = datetime.now(timezone.utc).isoformat()
        with connection:
            connection.execute(
                """
                INSERT INTO job_feedback (job_id, label, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    label = excluded.label,
                    updated_at = excluded.updated_at
                """,
                (job_id, label, timestamp),
            )
            connection.execute(
                """
                INSERT INTO feedback_events
                    (job_id, label, source, created_at, match_score,
                     match_reasons, company)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    label,
                    source,
                    timestamp,
                    payload.get("match_score"),
                    payload.get("match_reasons"),
                    (stored[1] if stored else None)
                    or normalize_company(
                        (stored[2] if stored else None)
                        or payload.get("company")
                    ),
                ),
            )
        return job_id
    finally:
        connection.close()


def feedback_adjustments(*, read_only: bool = False) -> dict[str, list[str]]:
    """Build conservative scoring hints from stored user labels."""
    if not DATABASE_PATH.exists():
        return {"preferred_skills": [], "penalized_companies": []}
    connection = _connect(read_only=read_only)
    try:
        if not read_only:
            _initialize_schema(connection)
        elif not connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'job_feedback'
            """
        ).fetchone():
            return {"preferred_skills": [], "penalized_companies": []}
        rows = connection.execute(
            """
            SELECT f.label, COALESCE(s.normalized_company, s.company),
                   s.notification_payload
            FROM job_feedback f
            JOIN seen_jobs s ON s.job_id = f.job_id
            """
        ).fetchall()
    finally:
        connection.close()

    skill_counts: dict[str, int] = {}
    company_counts: dict[str, int] = {}
    ignored = {"backend", "platform", "remote", "software engineer"}
    for label, company, payload in rows:
        try:
            reasons = str(json.loads(payload or "{}").get("match_reasons", ""))
        except json.JSONDecodeError:
            reasons = ""
        if label in {"relevant", "applied"}:
            for reason in (part.strip().lower() for part in reasons.split(",")):
                if (
                    reason
                    and reason not in ignored
                    and "feedback" not in reason
                    and "stretch" not in reason
                ):
                    skill_counts[reason] = skill_counts.get(reason, 0) + 1
        elif label == "irrelevant" and company:
            normalized = normalize_company(company)
            company_counts[normalized] = company_counts.get(normalized, 0) + 1
    return {
        "preferred_skills": sorted(
            skill for skill, count in skill_counts.items() if count >= 1
        ),
        "penalized_companies": sorted(
            company for company, count in company_counts.items() if count >= 1
        ),
    }


def metrics() -> dict[str, float | int]:
    """Return small, database-backed ranking and delivery metrics."""
    if not DATABASE_PATH.exists():
        return {
            "jobs": 0,
            "notified": 0,
            "applications": 0,
            "feedback_events": 0,
            "feedback_precision": 0.0,
            "alert_to_application_conversion": 0.0,
            "duplicate_rate": 0.0,
        }
    connection = _connect()
    try:
        jobs, notified = connection.execute(
            "SELECT COUNT(*), SUM(notified_at IS NOT NULL) FROM seen_jobs"
        ).fetchone()
        applications = connection.execute(
            "SELECT COUNT(*) FROM applications"
        ).fetchone()[0]
        feedback_events, positive = connection.execute(
            """
            SELECT COUNT(*), SUM(label IN ('relevant', 'applied'))
            FROM feedback_events
            """
        ).fetchone()
        duplicates = connection.execute(
            "SELECT COUNT(*) FROM dedupe_events"
        ).fetchone()[0]
    finally:
        connection.close()
    jobs = int(jobs or 0)
    notified = int(notified or 0)
    feedback_events = int(feedback_events or 0)
    positive = int(positive or 0)
    return {
        "jobs": jobs,
        "notified": notified,
        "applications": int(applications or 0),
        "feedback_events": feedback_events,
        "feedback_precision": round(positive / feedback_events, 4)
        if feedback_events
        else 0.0,
        "alert_to_application_conversion": round(
            int(applications or 0) / notified, 4
        )
        if notified
        else 0.0,
        "duplicate_rate": round(duplicates / (jobs + duplicates), 4)
        if jobs + duplicates
        else 0.0,
    }
