"""Public external job-source adapters for Job Radar."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd


def is_within_window(
    posted_date: Any,
    max_age_days: int = 14,
    now: datetime | None = None,
) -> bool:
    """Return True if posted_date is within max_age_days from current time.

    Returns True if posted_date is missing or unparseable to avoid dropping
    jobs with unknown posting dates.
    """
    if posted_date is None or pd.isna(posted_date):
        return True

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    dt: datetime | None = None
    if isinstance(posted_date, datetime):
        dt = posted_date
    elif isinstance(posted_date, (int, float)):
        ts = float(posted_date)
        if ts > 1e11:  # Timestamp in milliseconds
            ts /= 1000.0
        try:
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        except (ValueError, OSError):
            return True
    elif isinstance(posted_date, str):
        cleaned = posted_date.strip()
        if not cleaned:
            return True
        try:
            dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except ValueError:
            try:
                dt_pd = pd.to_datetime(cleaned)
                if pd.notna(dt_pd):
                    dt = dt_pd.to_pydatetime()
            except Exception:
                return True

    if dt is None:
        return True

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    cutoff = current - timedelta(days=max_age_days)
    return dt >= cutoff
