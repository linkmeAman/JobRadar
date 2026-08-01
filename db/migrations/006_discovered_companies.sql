-- Migration 006: discovered_companies table for Google Dork ATS discovery.
-- Actual creation is handled by storage.py migrate(); this file is a reference artifact.

CREATE TABLE IF NOT EXISTS discovered_companies (
    provider        TEXT    NOT NULL,
    slug            TEXT    NOT NULL,
    company_name    TEXT,
    discovered_at   TEXT    NOT NULL,
    last_scraped_at TEXT,
    PRIMARY KEY(provider, slug)
);
