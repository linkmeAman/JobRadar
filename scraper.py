"""Run each configured JobSpy search and merge the results."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd
from jobspy import scrape_jobs

import scrape_state


_CONFIG_ONLY_KEYS = {"name", "always_run"}


def _validate_search(search: Mapping[str, Any]) -> None:
    """Check the JobSpy constraints that are easy to violate in YAML."""
    configured_sites = search.get("site_name", [])
    if isinstance(configured_sites, str):
        configured_sites = [configured_sites]
    site_names = {str(site).lower() for site in configured_sites}

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


def run_all(
    searches: Sequence[Mapping[str, Any]],
    cooldown_minutes: int = 120,
    max_cooldown_minutes: int = 720,
) -> pd.DataFrame:
    """Scrape every configured search, retaining results from successful runs.

    Each configured site is called separately. Once a site returns 429 it is
    skipped for the rest of the run, while all other sites continue normally.
    """
    results: list[pd.DataFrame] = []
    blocked_sites: set[str] = set()

    for search in searches:
        name = str(search.get("name", "unnamed"))
        configured_sites = search.get("site_name", [])
        if isinstance(configured_sites, str):
            configured_sites = [configured_sites]

        for configured_site in configured_sites:
            site = str(configured_site).lower()
            if site in blocked_sites:
                print(f"search={name} site={site} skipped=blocked")
                continue
            cooldown_end = scrape_state.blocked_until(site)
            if cooldown_end is not None:
                print(
                    f"search={name} site={site} skipped=cooldown "
                    f"until={cooldown_end.isoformat()}"
                )
                continue

            site_search = dict(search)
            site_search["site_name"] = [site]
            try:
                _validate_search(site_search)
                params = {
                    key: value
                    for key, value in site_search.items()
                    if key not in _CONFIG_ONLY_KEYS
                }
                jobs = scrape_jobs(**params)

                if jobs is None or jobs.empty:
                    scrape_state.record_success(site, 0)
                    print(f"search={name} site={site} scraped=0")
                    continue

                jobs = jobs.copy()
                if "site" not in jobs.columns:
                    jobs["site"] = site
                else:
                    jobs["site"] = jobs["site"].fillna(site)

                results.append(jobs)
                scrape_state.record_success(site, len(jobs))
                print(f"search={name} site={site} scraped={len(jobs)}")
            except Exception as exc:  # One provider must not stop other providers.
                message = str(exc)
                if "429" in message:
                    blocked_sites.add(site)
                    cooldown_end = scrape_state.record_blocked(
                        site,
                        base_cooldown_minutes=cooldown_minutes,
                        max_cooldown_minutes=max_cooldown_minutes,
                    )
                    print(
                        f"search={name} site={site} blocked=429; "
                        f"cooldown_until={cooldown_end.isoformat()}"
                    )
                else:
                    scrape_state.record_failure(
                        site, f"{type(exc).__name__}: {message}"
                    )
                    print(
                        f"search={name} site={site} "
                        f"failed={type(exc).__name__}: {message}"
                    )

    return pd.concat(results, ignore_index=True) if results else pd.DataFrame()
