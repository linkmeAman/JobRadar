"""SQLite-backed deduplication, delivery state, and relevance feedback."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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
    notified_at TIMESTAMP,
    expired_at TIMESTAMP,
    is_active INTEGER NOT NULL DEFAULT 1
)
"""

_FEEDBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS job_feedback (
    job_id TEXT PRIMARY KEY,
    label TEXT NOT NULL CHECK(label IN ('relevant', 'irrelevant', 'applied')),
    updated_at TIMESTAMP NOT NULL,
    FOREIGN KEY(job_id) REFERENCES seen_jobs(job_id)
)
"""

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


def _text(value: Any) -> str:
    return "" if pd.isna(value) else str(value)


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
    connection.execute(_SCHEMA)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(seen_jobs)")
    }
    if "notification_payload" not in columns:
        connection.execute(
            "ALTER TABLE seen_jobs ADD COLUMN notification_payload TEXT"
        )
    if "notified_at" not in columns:
        connection.execute(
            "ALTER TABLE seen_jobs ADD COLUMN notified_at TIMESTAMP"
        )
    if "expired_at" not in columns:
        connection.execute(
            "ALTER TABLE seen_jobs ADD COLUMN expired_at TIMESTAMP"
        )
    if "is_active" not in columns:
        connection.execute(
            "ALTER TABLE seen_jobs ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
        )
    connection.execute(_FEEDBACK_SCHEMA)


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

    connection = sqlite3.connect(DATABASE_PATH)
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
    connection = sqlite3.connect(DATABASE_PATH)
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
                    connection.execute(
                        """
                        UPDATE seen_jobs SET notification_payload = ?
                        WHERE job_id = ? AND notified_at IS NULL
                            AND expired_at IS NULL
                        """,
                        (payload, primary_id),
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
                        connection.execute(
                            """
                            UPDATE seen_jobs SET notification_payload = ?
                            WHERE job_id = ? AND notified_at IS NULL
                                AND expired_at IS NULL
                            """,
                            (payload, fallback[0]),
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
                            notification_payload = CASE
                                WHEN notified_at IS NULL AND expired_at IS NULL
                                THEN ? ELSE notification_payload END
                        WHERE job_id = ?
                        """,
                        (primary_id, payload, legacy_id),
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
                    continue

                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO seen_jobs
                        (job_id, title, company, site, first_seen_at,
                         notification_payload)
                    VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP, ?)
                    """,
                    (primary_id, title, company, site, payload),
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

    connection = sqlite3.connect(DATABASE_PATH)
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


def expire_pending(days: int) -> int:
    """Expire undelivered jobs older than the configured retry window."""
    if days <= 0 or not DATABASE_PATH.exists():
        return 0
    connection = sqlite3.connect(DATABASE_PATH)
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
    connection = sqlite3.connect(DATABASE_PATH)
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
    connection = sqlite3.connect(DATABASE_PATH)
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

    connection = sqlite3.connect(DATABASE_PATH)
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


def record_feedback(job_id_or_prefix: str, label: str) -> str:
    """Attach a relevance label to one exact or uniquely prefixed job ID."""
    if label not in {"relevant", "irrelevant", "applied"}:
        raise ValueError("feedback must be relevant, irrelevant, or applied")

    job_id = resolve_job_id(job_id_or_prefix)
    connection = sqlite3.connect(DATABASE_PATH)
    try:
        _initialize_schema(connection)
        with connection:
            connection.execute(
                """
                INSERT INTO job_feedback (job_id, label, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    label = excluded.label,
                    updated_at = excluded.updated_at
                """,
                (job_id, label, datetime.now(timezone.utc).isoformat()),
            )
        return job_id
    finally:
        connection.close()


def feedback_adjustments(*, read_only: bool = False) -> dict[str, list[str]]:
    """Build conservative scoring hints from stored user labels."""
    if not DATABASE_PATH.exists():
        return {"preferred_skills": [], "penalized_companies": []}
    connection = sqlite3.connect(DATABASE_PATH)
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
            SELECT f.label, s.company, s.notification_payload
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
            normalized = str(company).strip().lower()
            company_counts[normalized] = company_counts.get(normalized, 0) + 1
    return {
        "preferred_skills": sorted(
            skill for skill, count in skill_counts.items() if count >= 1
        ),
        "penalized_companies": sorted(
            company for company, count in company_counts.items() if count >= 1
        ),
    }
