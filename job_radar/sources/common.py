"""Shared source-adapter helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

import pandas as pd

# Fix Scrapling 0.4.12 hardcoded Chromium version 149 exceeding browserforge header dataset bounds (141)
try:
    import scrapling.engines.toolbelt.fingerprints as _scrapling_fp

    _scrapling_fp.chromium_version = 141
    _scrapling_fp.chrome_version = 141
except (ImportError, AttributeError):
    pass


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
