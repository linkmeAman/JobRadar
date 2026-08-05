"""Reliable sender for the private Aman OS Job Radar import endpoint."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import pandas as pd
import requests

from . import dedupe


@dataclass(frozen=True)
class SyncResult:
    attempted: int = 0
    synchronized: int = 0
    skipped: int = 0
    error: str | None = None


def _text(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value).strip()


def _published_at(value: Any) -> str | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return _text(value) or None


def _record(row: pd.Series) -> dict[str, Any] | None:
    description = _text(row.get("description"))
    job_id = _text(row.get("job_id"))
    title = _text(row.get("title"))
    company = _text(row.get("company"))
    if not job_id or not title or not company or len(description) < 80:
        return None
    location = ", ".join(
        part
        for part in (_text(row.get("city")), _text(row.get("state")), _text(row.get("country")))
        if part
    )
    remote = row.get("is_remote")
    is_remote = None if remote is None or pd.isna(remote) else bool(remote)
    work_mode = (
        "remote" if is_remote is True
        else "onsite" if is_remote is False
        else "unspecified"
    )
    return {
        "externalId": job_id,
        "source": _text(row.get("site")) or "Job Radar",
        "sourceUrl": _text(row.get("job_url")),
        "company": company,
        "title": title,
        "location": location or ("Remote" if work_mode == "remote" else ""),
        "workMode": work_mode,
        "publishedAt": _published_at(row.get("date_posted")),
        "jdText": description,
    }


def _payload_hash(row: pd.Series) -> str:
    stored = _text(row.get("aman_os_payload_hash"))
    if stored:
        return stored
    payload = {
        key: None if pd.isna(value) else value
        for key, value in row.items()
        if key not in {"job_id", "aman_os_payload_hash"}
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def sync_pending(config: dict[str, Any]) -> SyncResult:
    """Push queued source records; errors are recorded and retried next run."""
    if not config.get("enabled", False):
        return SyncResult()
    endpoint = os.environ.get("AMAN_OS_ENDPOINT", "").strip()
    api_key = os.environ.get("AMAN_OS_API_KEY", "").strip()
    if not endpoint or not api_key:
        error = "AMAN_OS_ENDPOINT and AMAN_OS_API_KEY are required"
        logging.warning("aman_os_sync=not_configured error=%s", error)
        return SyncResult(error=error)
    if not endpoint.startswith("https://"):
        error = "AMAN_OS_ENDPOINT must use HTTPS"
        logging.error("aman_os_sync=invalid_config error=%s", error)
        return SyncResult(error=error)

    candidates = dedupe.aman_os_sync_candidates(int(config.get("batch_size", 50)))
    if candidates.empty:
        return SyncResult()
    records: list[dict[str, Any]] = []
    job_ids: list[str] = []
    hashes: dict[str, str] = {}
    skipped = 0
    for _, row in candidates.iterrows():
        record = _record(row)
        job_id = _text(row.get("job_id"))
        if record is None:
            skipped += 1
            continue
        records.append(record)
        job_ids.append(job_id)
        hashes[job_id] = _payload_hash(row)
    if not records:
        return SyncResult(skipped=skipped)
    try:
        response = requests.post(
            endpoint,
            headers={"Authorization": f"Bearer {api_key}"},
            json={"records": records},
            timeout=float(config.get("timeout_seconds", 20)),
        )
        response.raise_for_status()
        data = response.json()
        if int(data.get("received", 0)) != len(records):
            raise RuntimeError("Aman OS returned an incomplete import result")
    except (requests.RequestException, ValueError, RuntimeError) as exc:
        error = f"{type(exc).__name__}: {exc}"
        dedupe.record_aman_os_sync_error(job_ids, error)
        logging.error("aman_os_sync=failed attempted=%d error=%s", len(records), error)
        return SyncResult(attempted=len(records), skipped=skipped, error=error)
    dedupe.mark_aman_os_synced(job_ids, hashes)
    logging.info("aman_os_sync=succeeded attempted=%d", len(records))
    return SyncResult(attempted=len(records), synchronized=len(records), skipped=skipped)
