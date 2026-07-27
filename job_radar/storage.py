"""Shared SQLite connection and schema migration helpers."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


SCHEMA_VERSION = 1

_TABLES = {
    "seen_jobs": """
        CREATE TABLE IF NOT EXISTS seen_jobs (
            job_id TEXT PRIMARY KEY,
            title TEXT,
            company TEXT,
            site TEXT,
            first_seen_at TIMESTAMP,
            notification_payload TEXT,
            notified_at TIMESTAMP,
            expired_at TIMESTAMP,
            is_active INTEGER NOT NULL DEFAULT 1,
            normalized_company TEXT
        )
    """,
    "job_feedback": """
        CREATE TABLE IF NOT EXISTS job_feedback (
            job_id TEXT PRIMARY KEY,
            label TEXT NOT NULL CHECK(label IN ('relevant', 'irrelevant', 'applied')),
            updated_at TIMESTAMP NOT NULL,
            FOREIGN KEY(job_id) REFERENCES seen_jobs(job_id)
        )
    """,
    "applications": """
        CREATE TABLE IF NOT EXISTS applications (
            job_id TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL,
            status TEXT NOT NULL,
            last_contact TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            next_follow_up_at TEXT,
            interview_at TEXT,
            resume_version TEXT,
            cover_letter_version TEXT,
            description_snapshot TEXT,
            updated_at TEXT,
            FOREIGN KEY(job_id) REFERENCES seen_jobs(job_id)
        )
    """,
    "provider_state": """
        CREATE TABLE IF NOT EXISTS provider_state (
            provider TEXT PRIMARY KEY,
            blocked_until TEXT,
            consecutive_429 INTEGER NOT NULL DEFAULT 0,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            failure_alert_sent INTEGER NOT NULL DEFAULT 0,
            consecutive_empty_results INTEGER NOT NULL DEFAULT 0,
            last_status TEXT,
            last_result_count INTEGER,
            updated_at TEXT NOT NULL
        )
    """,
    "runtime_state": """
        CREATE TABLE IF NOT EXISTS runtime_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """,
    "scrape_runs": """
        CREATE TABLE IF NOT EXISTS scrape_runs (
            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            duration_seconds REAL,
            mode TEXT NOT NULL,
            selected_searches TEXT NOT NULL,
            provider_status TEXT,
            scraped_count INTEGER NOT NULL DEFAULT 0,
            matched_count INTEGER NOT NULL DEFAULT 0,
            new_count INTEGER NOT NULL DEFAULT 0,
            pending_count INTEGER NOT NULL DEFAULT 0,
            queued_count INTEGER NOT NULL DEFAULT 0,
            deferred_count INTEGER NOT NULL DEFAULT 0,
            expired_count INTEGER NOT NULL DEFAULT 0,
            sent_count INTEGER NOT NULL DEFAULT 0,
            all_providers_failed INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'running',
            error TEXT,
            health_alert_sent INTEGER NOT NULL DEFAULT 0
        )
    """,
    "web_trigger_history": """
        CREATE TABLE IF NOT EXISTS web_trigger_history (
            trigger_id INTEGER PRIMARY KEY AUTOINCREMENT,
            requested_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            status TEXT NOT NULL,
            run_id INTEGER,
            error TEXT
        )
    """,
    "feedback_events": """
        CREATE TABLE IF NOT EXISTS feedback_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            label TEXT NOT NULL CHECK(label IN ('relevant', 'irrelevant', 'applied')),
            source TEXT NOT NULL DEFAULT 'manual',
            created_at TEXT NOT NULL,
            match_score INTEGER,
            match_reasons TEXT,
            company TEXT,
            FOREIGN KEY(job_id) REFERENCES seen_jobs(job_id)
        )
    """,
    "application_events": """
        CREATE TABLE IF NOT EXISTS application_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            event_type TEXT NOT NULL,
            old_status TEXT,
            new_status TEXT,
            note TEXT,
            occurred_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES seen_jobs(job_id)
        )
    """,
    "audit_log": """
        CREATE TABLE IF NOT EXISTS audit_log (
            audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            job_id TEXT,
            actor TEXT NOT NULL,
            details TEXT,
            created_at TEXT NOT NULL
        )
    """,
    "dedupe_events": """
        CREATE TABLE IF NOT EXISTS dedupe_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT NOT NULL,
            identity_type TEXT NOT NULL,
            detected_at TEXT NOT NULL,
            FOREIGN KEY(job_id) REFERENCES seen_jobs(job_id)
        )
    """,
}

_MISSING_COLUMNS = {
    "seen_jobs": {
        "notification_payload": "TEXT",
        "notified_at": "TIMESTAMP",
        "expired_at": "TIMESTAMP",
        "is_active": "INTEGER NOT NULL DEFAULT 1",
        "normalized_company": "TEXT",
    },
    "applications": {
        "notes": "TEXT NOT NULL DEFAULT ''",
        "next_follow_up_at": "TEXT",
        "interview_at": "TEXT",
        "resume_version": "TEXT",
        "cover_letter_version": "TEXT",
        "description_snapshot": "TEXT",
        "updated_at": "TEXT",
    },
    "provider_state": {
        "consecutive_failures": "INTEGER NOT NULL DEFAULT 0",
        "failure_alert_sent": "INTEGER NOT NULL DEFAULT 0",
        "consecutive_empty_results": "INTEGER NOT NULL DEFAULT 0",
    },
    "scrape_runs": {"health_alert_sent": "INTEGER NOT NULL DEFAULT 0"},
}


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})")
    }


def migrate(connection: sqlite3.Connection) -> None:
    """Create current tables and add compatible columns to older databases."""
    for statement in _TABLES.values():
        connection.execute(statement)
    for table, columns in _MISSING_COLUMNS.items():
        existing = _columns(connection, table)
        for name, definition in columns.items():
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        "INSERT OR IGNORE INTO schema_version(version, applied_at) "
        "VALUES (?, CURRENT_TIMESTAMP)",
        (SCHEMA_VERSION,),
    )


def connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    """Open SQLite with safe concurrency defaults and current schema."""
    if read_only:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10
        )
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=10)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    if not read_only:
        if os.environ.get("JOB_RADAR_SQLITE_WAL", "1") != "0":
            try:
                connection.execute("PRAGMA journal_mode = WAL")
            except sqlite3.OperationalError:
                # ponytail: keep SQLite's native fallback for filesystems that cannot create WAL sidecars.
                pass
        connection.execute("PRAGMA synchronous = NORMAL")
        migrate(connection)
        connection.commit()
    return connection


def integrity_check(path: Path) -> str:
    """Return SQLite's integrity-check result."""
    connection = connect(path, read_only=True)
    try:
        result = connection.execute("PRAGMA integrity_check").fetchone()
        return str(result[0] if result else "unknown")
    finally:
        connection.close()
