"""Shared source-adapter helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import pandas as pd


JOB_COLUMNS = (
    "title",
    "company",
    "city",
    "state",
    "country",
    "job_url",
    "description",
    "is_remote",
    "remote_restriction",
    "site",
    "date_posted",
    "min_amount",
    "max_amount",
    "currency",
)


def frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Return rows with the common JobSpy-compatible columns present."""
    result = pd.DataFrame(rows)
    for column in JOB_COLUMNS:
        if column not in result.columns:
            result[column] = None
    return result


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def utc_from_milliseconds(value: Any) -> str | None:
    try:
        return datetime.fromtimestamp(
            float(value) / 1000, tz=timezone.utc
        ).isoformat()
    except (TypeError, ValueError, OSError):
        return None
