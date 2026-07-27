"""Small localhost-only web UI for Job Radar state and manual runs."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from .. import application_tracker, dedupe, main, storage


STATIC_DIR = Path(__file__).with_name("static")
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connection() -> sqlite3.Connection:
    return storage.connect(dedupe.DATABASE_PATH)


def _audit(action: str, job_id: str | None, details: dict[str, Any]) -> None:
    connection = _connection()
    try:
        with connection:
            connection.execute(
                """
                INSERT INTO audit_log(action, job_id, actor, details, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    action,
                    job_id,
                    "web",
                    json.dumps(details, default=str, sort_keys=True),
                    _now(),
                ),
            )
    finally:
        connection.close()


def _health() -> dict[str, Any]:
    try:
        connection = _connection()
        try:
            connection.execute("SELECT 1").fetchone()
        finally:
            connection.close()
        return {"ok": True, "database": "ok", "time": _now()}
    except sqlite3.Error as exc:
        return {"ok": False, "database": str(exc), "time": _now()}


def _rows_as_dicts(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = [description[0] for description in cursor.description or []]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def summary() -> dict[str, Any]:
    connection = _connection()
    try:
        jobs = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN is_active = 1 THEN 1 ELSE 0 END) AS active,
                SUM(CASE WHEN is_active = 1 AND notified_at IS NULL
                    AND expired_at IS NULL THEN 1 ELSE 0 END) AS pending,
                SUM(CASE WHEN a.job_id IS NOT NULL THEN 1 ELSE 0 END) AS applied
            FROM seen_jobs s
            LEFT JOIN applications a ON a.job_id = s.job_id
            """
        ).fetchone()
        last_run = connection.execute(
            """
            SELECT run_id, started_at, completed_at, status,
                   scraped_count, matched_count, sent_count
            FROM scrape_runs ORDER BY run_id DESC LIMIT 1
            """
        ).fetchone()
    finally:
        connection.close()
    return {
        "total": int(jobs[0] or 0),
        "active": int(jobs[1] or 0),
        "pending": int(jobs[2] or 0),
        "applied": int(jobs[3] or 0),
        "last_run": (
            {
                "run_id": last_run[0],
                "started_at": last_run[1],
                "completed_at": last_run[2],
                "status": last_run[3],
                "scraped_count": last_run[4],
                "matched_count": last_run[5],
                "sent_count": last_run[6],
            }
            if last_run
            else None
        ),
    }


def jobs(status: str | None = None, limit: int = 10000) -> list[dict[str, Any]]:
    connection = _connection()
    try:
        rows = connection.execute(
            """
            SELECT s.job_id, s.title, s.company, s.site, s.first_seen_at,
                   s.notification_payload, s.notified_at, s.expired_at,
                   s.is_active, a.applied_at, a.status AS application_status,
                   a.last_contact, f.label AS feedback_label, a.notes,
                   a.next_follow_up_at, a.interview_at, a.resume_version,
                   a.cover_letter_version, a.description_snapshot
            FROM seen_jobs s
            LEFT JOIN applications a ON a.job_id = s.job_id
            LEFT JOIN job_feedback f ON f.job_id = s.job_id
            ORDER BY s.first_seen_at DESC, s.rowid DESC
            LIMIT ?
            """,
            (max(1, min(limit, 10000)),),
        ).fetchall()
    finally:
        connection.close()

    result: list[dict[str, Any]] = []
    for row in rows:
        payload = json.loads(row[5] or "{}")
        if row[8] == 0:
            current_status = "inactive"
        elif row[10]:
            current_status = str(row[10])
        elif row[7]:
            current_status = "expired"
        elif row[6]:
            current_status = "notified"
        else:
            current_status = "pending"
        item = {
            "job_id": row[0],
            "title": payload.get("title") or row[1],
            "company": payload.get("company") or row[2],
            "site": row[3],
            "first_seen_at": row[4],
            "notified_at": row[6],
            "expired_at": row[7],
            "is_active": bool(row[8]),
            "status": current_status,
            "application_status": row[10],
            "applied_at": row[9],
            "last_contact": row[11],
            "feedback_label": row[12],
            "notes": row[13],
            "next_follow_up_at": row[14],
            "interview_at": row[15],
            "resume_version": row[16],
            "cover_letter_version": row[17],
            "description_snapshot": row[18],
            "job_url": payload.get("job_url"),
            "city": payload.get("city"),
            "state": payload.get("state"),
            "is_remote": payload.get("is_remote"),
            "match_score": payload.get("match_score"),
            "match_reasons": payload.get("match_reasons"),
            "description": payload.get("description"),
            "country": payload.get("country"),
            "min_amount": payload.get("min_amount"),
            "max_amount": payload.get("max_amount"),
            "currency": payload.get("currency"),
        }
        if status and status != "all" and item["status"] != status:
            continue
        result.append(item)
    return result


def runs(limit: int = 10000) -> list[dict[str, Any]]:
    connection = _connection()
    try:
        cursor = connection.execute(
            """
            SELECT run_id, started_at, completed_at, duration_seconds, mode,
                   provider_status, scraped_count, matched_count, new_count,
                   pending_count, sent_count, status, error
            FROM scrape_runs ORDER BY run_id DESC LIMIT ?
            """,
            (max(1, min(limit, 10000)),),
        )
        return _rows_as_dicts(cursor)
    finally:
        connection.close()


def trigger_history(limit: int = 10000) -> list[dict[str, Any]]:
    connection = _connection()
    try:
        cursor = connection.execute(
            """
            SELECT trigger_id, requested_at, started_at, completed_at,
                   status, run_id, error
            FROM web_trigger_history
            ORDER BY trigger_id DESC LIMIT ?
            """,
            (max(1, min(limit, 10000)),),
        )
        return _rows_as_dicts(cursor)
    finally:
        connection.close()


def _update_trigger(
    trigger_id: int,
    status: str,
    *,
    started_at: str | None = None,
    completed_at: str | None = None,
    run_id: int | None = None,
    error: str | None = None,
) -> None:
    connection = _connection()
    try:
        with connection:
            connection.execute(
                """
                UPDATE web_trigger_history
                SET status = ?, started_at = COALESCE(?, started_at),
                    completed_at = COALESCE(?, completed_at),
                    run_id = COALESCE(?, run_id), error = ?
                WHERE trigger_id = ?
                """,
                (
                    status,
                    started_at,
                    completed_at,
                    run_id,
                    error,
                    trigger_id,
                ),
            )
    finally:
        connection.close()


def _run_trigger(trigger_id: int) -> None:
    _update_trigger(trigger_id, "running", started_at=_now())
    try:
        result = main.run(mode="manual_ui") or {}
        _update_trigger(
            trigger_id,
            "completed",
            completed_at=_now(),
            run_id=result.get("run_id"),
        )
    except Exception as exc:  # UI must record failures and remain available.
        _update_trigger(
            trigger_id,
            "failed",
            completed_at=_now(),
            error=f"{type(exc).__name__}: {exc}"[:500],
        )
        logging.exception("ui_trigger=failed trigger_id=%d", trigger_id)


def queue_trigger(executor: ThreadPoolExecutor) -> int:
    connection = _connection()
    try:
        with connection:
            cursor = connection.execute(
                """
                INSERT INTO web_trigger_history (requested_at, status)
                VALUES (?, 'queued')
                """,
                (_now(),),
            )
            trigger_id = int(cursor.lastrowid)
    finally:
        connection.close()
    executor.submit(_run_trigger, trigger_id)
    return trigger_id


class JobRadarServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int]):
        super().__init__(address, RequestHandler)
        self.trigger_executor = ThreadPoolExecutor(max_workers=1)
        self.last_trigger_at = 0.0

    def server_close(self) -> None:
        self.trigger_executor.shutdown(wait=False, cancel_futures=True)
        super().server_close()


class RequestHandler(BaseHTTPRequestHandler):
    server: JobRadarServer

    def log_message(self, format: str, *args: Any) -> None:
        logging.info("ui %s", format % args)

    def _json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, message: str, status: int = HTTPStatus.BAD_REQUEST) -> None:
        self._json({"error": message}, status)

    def _authorized(self) -> bool:
        configured = os.environ.get("JOB_RADAR_UI_TOKEN", "").strip()
        if not configured:
            return True
        supplied = self.headers.get("Authorization", "")
        return supplied == f"Bearer {configured}"

    def _same_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urlsplit(origin).netloc == self.headers.get("Host", "")

    def _guard(self, *, mutating: bool = False) -> bool:
        if not self._authorized():
            self._error("authentication required", HTTPStatus.UNAUTHORIZED)
            return False
        if mutating and not self._same_origin():
            self._error("cross-origin request rejected", HTTPStatus.FORBIDDEN)
            return False
        return True

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length > 10000:
            raise ValueError("request body is too large")
        raw = self.rfile.read(length) if length else b"{}"
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("request body must be a JSON object")
        return value

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/":
            self._static("index.html")
            return
        try:
            if parsed.path == "/api/summary":
                if not self._guard():
                    return
                self._json(summary())
            elif parsed.path == "/api/jobs":
                if not self._guard():
                    return
                query = parse_qs(parsed.query)
                self._json(
                    jobs(
                        query.get("status", [None])[0],
                        int(query.get("limit", [10000])[0]),
                    )
                )
            elif parsed.path == "/api/runs":
                if not self._guard():
                    return
                self._json(runs())
            elif parsed.path == "/api/triggers":
                if not self._guard():
                    return
                self._json(trigger_history())
            elif parsed.path == "/api/metrics":
                if not self._guard():
                    return
                self._json(dedupe.metrics())
            elif parsed.path == "/health":
                self._json(_health(), HTTPStatus.OK)
            else:
                self._error("not found", HTTPStatus.NOT_FOUND)
        except (ValueError, sqlite3.Error) as exc:
            self._error(str(exc), HTTPStatus.BAD_REQUEST)

    def do_POST(self) -> None:
        if self.path != "/api/triggers":
            self._error("not found", HTTPStatus.NOT_FOUND)
            return
        if not self._guard(mutating=True):
            return
        if time.monotonic() - self.server.last_trigger_at < 30:
            self._error("trigger rate limited", HTTPStatus.TOO_MANY_REQUESTS)
            return
        try:
            trigger_id = queue_trigger(self.server.trigger_executor)
            self.server.last_trigger_at = time.monotonic()
            self._json(
                {"trigger_id": trigger_id, "status": "queued"},
                HTTPStatus.ACCEPTED,
            )
        except sqlite3.Error as exc:
            self._error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PATCH(self) -> None:
        prefix = "/api/jobs/"
        if not self.path.startswith(prefix):
            self._error("not found", HTTPStatus.NOT_FOUND)
            return
        if not self._guard(mutating=True):
            return
        job_id = unquote(self.path[len(prefix) :]).strip()
        try:
            body = self._body()
            response: dict[str, Any] = {"job_id": job_id}
            if "active" in body:
                if not isinstance(body["active"], bool):
                    raise ValueError("active must be true or false")
                response["job_id"] = dedupe.set_active(
                    job_id, body["active"]
                )
                response["active"] = body["active"]
            if "application_status" in body:
                status = str(body["application_status"]).strip().lower()
                if status == "applied":
                    change = application_tracker.mark_applied(job_id)
                else:
                    change = application_tracker.set_status(job_id, status)
                response["application_status"] = change.status
            if body.get("contacted") is True:
                application_tracker.mark_contacted(job_id)
                response["contacted"] = True
            detail_keys = {
                "notes",
                "next_follow_up_at",
                "interview_at",
                "resume_version",
                "cover_letter_version",
            }
            details = {key: body[key] for key in detail_keys if key in body}
            if details:
                change = application_tracker.update_details(job_id, **details)
                response["application_status"] = change.status
            if len(response) == 1:
                raise ValueError("provide active, application_status, or contacted")
            _audit("job_updated", job_id, body)
            self._json(response)
        except ValueError as exc:
            self._error(str(exc))
        except sqlite3.Error as exc:
            self._error(str(exc), HTTPStatus.INTERNAL_SERVER_ERROR)

    def _static(self, name: str) -> None:
        if name != "index.html":
            self._error("not found", HTTPStatus.NOT_FOUND)
            return
        body = (STATIC_DIR / name).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run_server(host: str = "127.0.0.1", port: int = 8765) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"} and os.environ.get(
        "JOB_RADAR_ALLOW_NONLOCAL_UI"
    ) != "1":
        raise ValueError(
            "non-loopback UI binding requires JOB_RADAR_ALLOW_NONLOCAL_UI=1"
        )
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    server = JobRadarServer((host, port))
    logging.info("Job Radar UI listening at http://%s:%d", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main_cli() -> None:
    parser = argparse.ArgumentParser(description="Job Radar local web UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    run_server(args.host, args.port)


if __name__ == "__main__":
    main_cli()
