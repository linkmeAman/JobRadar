"""Telegram Bot API notifications for new Job Radar listings."""

from __future__ import annotations

import math
import os
import re
import time
from collections.abc import Callable
from typing import Any

import pandas as pd
import requests


TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
SEND_DELAY_SECONDS = 0.3
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
    return "\n".join(lines)


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
        try:
            response = requests.post(
                url,
                json={"chat_id": chat_id, "text": format_job_message(row)},
                timeout=20,
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            status = getattr(exc.response, "status_code", None)
            detail = f" (HTTP {status})" if status is not None else ""
            raise RuntimeError(f"Telegram notification request failed{detail}") from None

        try:
            if response.json().get("ok") is not True:
                raise RuntimeError("Telegram rejected the notification")
        except ValueError:
            raise RuntimeError("Telegram returned an invalid response") from None

        sent += 1
        if on_sent is not None:
            on_sent(str(row["job_id"]))
        if position < len(rows) - 1:
            time.sleep(SEND_DELAY_SECONDS)

    return sent
