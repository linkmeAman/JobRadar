"""Telegram Bot API notifications for new Job Radar listings."""

from __future__ import annotations

import json
import math
import os
import re
import time
from collections.abc import Callable
from typing import Any

import pandas as pd
import requests


TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
TELEGRAM_UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"
TELEGRAM_CALLBACK_URL = "https://api.telegram.org/bot{token}/answerCallbackQuery"
SEND_DELAY_SECONDS = 0.3
MAX_SEND_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 1.0
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
_BOT_TOKEN_PATTERN = re.compile(r"^\d+:[A-Za-z0-9_-]+$")
_CHAT_ID_PATTERN = re.compile(r"^-?\d+$")


def get_credentials() -> tuple[str, str]:
    """Read and validate Telegram credentials without exposing their values."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set")
    if not _BOT_TOKEN_PATTERN.fullmatch(token):
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is invalid. Copy the complete BotFather token "
            "in the form <numeric_bot_id>:<secret>."
        )
    if not _CHAT_ID_PATTERN.fullmatch(chat_id):
        raise RuntimeError("TELEGRAM_CHAT_ID must be a numeric Telegram chat ID")

    return token, chat_id


def validate_delivery_target() -> None:
    """Confirm the configured chat is reachable before a scrape starts."""
    token, chat_id = get_credentials()
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{token}/getMe", timeout=20
        )
        response.raise_for_status()
        bot = response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError("Telegram could not validate TELEGRAM_BOT_TOKEN") from None

    if bot.get("ok") is not True:
        raise RuntimeError("Telegram rejected TELEGRAM_BOT_TOKEN")
    if str(bot["result"]["id"]) == chat_id:
        raise RuntimeError(
            "TELEGRAM_CHAT_ID is the bot's own ID. Set it to the chat.id from "
            "a message you send to the bot, not the id returned by getMe."
        )

    try:
        response = requests.get(
            f"https://api.telegram.org/bot{token}/getChat",
            params={"chat_id": chat_id},
            timeout=20,
        )
        response.raise_for_status()
        chat = response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError(
            "Telegram could not access TELEGRAM_CHAT_ID. Open the bot, send /start, "
            "then use that conversation's chat.id from getUpdates."
        ) from None

    if chat.get("ok") is not True:
        raise RuntimeError(
            "Telegram could not access TELEGRAM_CHAT_ID. Open the bot, send /start, "
            "then use that conversation's chat.id from getUpdates."
        )


def _is_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    return not pd.isna(value)


def _value(row: pd.Series, field: str) -> Any:
    return row.get(field)


def _display(value: Any, fallback: str) -> str:
    return str(value) if _is_present(value) else fallback


def format_job_message(row: pd.Series) -> str:
    """Format one JobSpy listing using the project-defined Telegram layout."""
    title = _display(_value(row, "title"), "Untitled role")
    company = _display(_value(row, "company"), "Unknown company")
    city = _value(row, "city")
    state = _value(row, "state")
    is_remote = _value(row, "is_remote")

    if _is_present(is_remote) and bool(is_remote):
        location = "Remote"
    else:
        location_parts = [str(value) for value in (city, state) if _is_present(value)]
        location = ", ".join(location_parts) or "Location not listed"

    lines = [f"🆕 {title}", f"🏢 {company} | 📍 {location}"]

    match_score = _value(row, "match_score")
    match_reasons = _value(row, "match_reasons")
    if _is_present(match_score):
        reasons = f" · {match_reasons}" if _is_present(match_reasons) else ""
        lines.append(f"🎯 Match score: {match_score}{reasons}")

    min_amount = _value(row, "min_amount")
    max_amount = _value(row, "max_amount")
    currency = _value(row, "currency")
    if _is_present(min_amount) or _is_present(max_amount):
        minimum = str(min_amount) if _is_present(min_amount) else ""
        maximum = str(max_amount) if _is_present(max_amount) else ""
        currency_text = f" {currency}" if _is_present(currency) else ""
        lines.append(f"💰 {minimum}-{maximum}{currency_text}")

    job_url = _display(_value(row, "job_url"), "")
    lines.append(f"🔗 {job_url}")
    job_id = _value(row, "job_id")
    if _is_present(job_id):
        lines.append(f"🆔 {str(job_id)[:12]}")
    return "\n".join(lines)


def _retry_delay(response: requests.Response | None, attempt: int) -> float:
    """Use Telegram's retry hint when available, otherwise exponential backoff."""
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after is None:
            try:
                retry_after = response.json().get("parameters", {}).get("retry_after")
            except ValueError:
                pass
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
    return RETRY_BACKOFF_SECONDS * (2**attempt)


def job_reply_markup(row: pd.Series) -> dict[str, Any] | None:
    """Return inline actions for one job alert."""
    job_id = _value(row, "job_id")
    if not _is_present(job_id):
        return None
    prefix = str(job_id)[:12]
    keyboard = [
        [
            {"text": "Apply", "callback_data": f"jr:applied:{prefix}"},
            {"text": "Save", "callback_data": f"jr:relevant:{prefix}"},
            {"text": "Reject", "callback_data": f"jr:irrelevant:{prefix}"},
        ],
        [{"text": "Contacted", "callback_data": f"jr:contacted:{prefix}"}],
    ]
    job_url = _value(row, "job_url")
    if _is_present(job_url):
        keyboard[-1].append({"text": "Open job", "url": str(job_url)})
    keyboard.append(
        [{"text": "Show fewer like this", "callback_data": f"jr:irrelevant:{prefix}"}]
    )
    return {"inline_keyboard": keyboard}


def _send_message(
    url: str,
    chat_id: str,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> None:
    """Send one message, retrying only temporary Telegram/network failures."""
    for attempt in range(MAX_SEND_ATTEMPTS):
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=20,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            response = exc.response
            status = getattr(response, "status_code", None)
            is_retryable = status is None or status in _RETRYABLE_STATUS_CODES
            if is_retryable and attempt < MAX_SEND_ATTEMPTS - 1:
                delay = _retry_delay(response, attempt)
                print(
                    f"telegram retry={attempt + 1}/{MAX_SEND_ATTEMPTS - 1} "
                    f"status={status or 'network'} delay={delay:.1f}s"
                )
                time.sleep(delay)
                continue

            detail = f" (HTTP {status})" if status is not None else ""
            raise RuntimeError(f"Telegram notification request failed{detail}") from None

        try:
            if response.json().get("ok") is not True:
                raise RuntimeError("Telegram rejected the notification")
        except ValueError:
            raise RuntimeError("Telegram returned an invalid response") from None
        return


def send_all(
    df: pd.DataFrame, on_sent: Callable[[str], None] | None = None
) -> int:
    """Send one Telegram notification per row and return the number sent.

    When supplied, ``on_sent`` is called after each accepted Telegram message
    with that row's ``job_id``. This keeps failed messages pending for retry.
    """
    if df.empty:
        return 0

    token, chat_id = get_credentials()

    sent = 0
    url = TELEGRAM_API_URL.format(token=token)
    rows = list(df.iterrows())
    for position, (_, row) in enumerate(rows):
        _send_message(url, chat_id, format_job_message(row), job_reply_markup(row))

        sent += 1
        if on_sent is not None:
            on_sent(str(row["job_id"]))
        if position < len(rows) - 1:
            time.sleep(SEND_DELAY_SECONDS)

    return sent


def send_text(text: str) -> None:
    """Send one non-job message to the configured Telegram chat."""
    token, chat_id = get_credentials()
    _send_message(TELEGRAM_API_URL.format(token=token), chat_id, text)


def answer_callback_query(callback_query_id: str, text: str) -> None:
    """Acknowledge one inline button press."""
    token, _ = get_credentials()
    try:
        response = requests.post(
            TELEGRAM_CALLBACK_URL.format(token=token),
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=20,
        )
        response.raise_for_status()
        if response.json().get("ok") is not True:
            raise RuntimeError("Telegram rejected the callback answer")
    except (requests.RequestException, ValueError):
        raise RuntimeError("Telegram callback answer failed") from None


def fetch_updates(offset: int | None = None) -> list[dict[str, Any]]:
    """Fetch pending message updates without using a Telegram SDK."""
    token, _ = get_credentials()
    params: dict[str, Any] = {
        "timeout": 0,
        "allowed_updates": json.dumps(["message", "callback_query"]),
    }
    if offset is not None:
        params["offset"] = offset
    try:
        response = requests.get(
            TELEGRAM_UPDATES_URL.format(token=token),
            params=params,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError):
        raise RuntimeError("Telegram command polling failed") from None
    if payload.get("ok") is not True or not isinstance(
        payload.get("result"), list
    ):
        raise RuntimeError("Telegram returned invalid command updates")
    return payload["result"]


def send_health_alert(message: str) -> None:
    """Send one operational alert through the configured Telegram chat."""
    send_text(f"⚠️ Job Radar health alert\n{message}")
