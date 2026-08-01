-- Migration 005: scraper_health table for tracking Scrapling adaptive re-locate events.
-- Actual creation is handled by storage.py migrate(); this file is a reference artifact.

CREATE TABLE IF NOT EXISTS scraper_health (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    source  TEXT    NOT NULL,
    event   TEXT    NOT NULL,
    ts      DATETIME NOT NULL
);
