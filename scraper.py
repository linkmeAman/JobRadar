"""Run each configured JobSpy search and merge the results."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd
from jobspy import scrape_jobs


def _validate_search(search: Mapping[str, Any]) -> None:
    """Check the JobSpy constraints that are easy to violate in YAML."""
    site_names = {str(site).lower() for site in search.get("site_name", [])}

    if "indeed" not in site_names:
        return

    if not search.get("country_indeed"):
        raise ValueError("Indeed searches require country_indeed")

    if (
        "hours_old" in search
        and search.get("job_type")
        and search.get("is_remote")
    ):
        raise ValueError(
            "Indeed cannot combine hours_old with job_type and is_remote"
        )


def run_all(searches: Sequence[Mapping[str, Any]]) -> pd.DataFrame:
    """Scrape every configured search, retaining results from successful runs.

    Failures are handled per search so a rate-limited source cannot stop the
    remaining searches. A 429 is reported and not retried during this run.
    """
    results: list[pd.DataFrame] = []

    for search in searches:
        name = str(search.get("name", "unnamed"))
        try:
            _validate_search(search)
            params = {key: value for key, value in search.items() if key != "name"}
            jobs = scrape_jobs(**params)

            if jobs is None or jobs.empty:
                print(f"search={name} scraped=0")
                continue

            jobs = jobs.copy()
            if "site" not in jobs.columns:
                jobs["site"] = name
            else:
                jobs["site"] = jobs["site"].fillna(name)

            results.append(jobs)
            print(f"search={name} scraped={len(jobs)}")
        except Exception as exc:  # JobSpy errors must not stop later searches.
            message = str(exc)
            if "429" in message:
                print(f"search={name} blocked=429; not retrying this run")
            else:
                print(f"search={name} failed={type(exc).__name__}: {message}")

    return pd.concat(results, ignore_index=True) if results else pd.DataFrame()
