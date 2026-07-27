"""Run each configured JobSpy search and merge the results."""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd
from jobspy import scrape_jobs

import scrape_state


_CONFIG_ONLY_KEYS = {"name", "always_run"}


@dataclass
class ScrapeOutcome:
    """Merged jobs plus provider-level status for run monitoring."""

    jobs: pd.DataFrame
    provider_status: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def all_providers_failed(self) -> bool:
        return bool(self.provider_status) and all(
            state.get("status") != "success"
            for state in self.provider_status.values()
        )


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
    *,
    persist_state: bool = True,
    return_report: bool = False,
) -> pd.DataFrame | ScrapeOutcome:
    """Scrape every configured search, retaining results from successful runs.

    Each configured site is called separately. Once a site returns 429 it is
    skipped for the rest of the run, while all other sites continue normally.
    """
    results: list[pd.DataFrame] = []
    blocked_sites: set[str] = set()
    provider_status: dict[str, dict[str, Any]] = {}

    def state_for(site: str) -> dict[str, Any]:
        return provider_status.setdefault(
            site,
            {
                "status": "pending",
                "results": 0,
                "searches": {},
                "errors": [],
            },
        )

    for search in searches:
        name = str(search.get("name", "unnamed"))
        configured_sites = search.get("site_name", [])
        if isinstance(configured_sites, str):
            configured_sites = [configured_sites]

        for configured_site in configured_sites:
            site = str(configured_site).lower()
            if site in blocked_sites:
                print(f"search={name} site={site} skipped=blocked")
                state = state_for(site)
                state["searches"][name] = {
                    "status": "skipped_blocked",
                    "results": 0,
                }
                continue
            cooldown_end = scrape_state.blocked_until(
                site, read_only=not persist_state
            )
            if cooldown_end is not None:
                print(
                    f"search={name} site={site} skipped=cooldown "
                    f"until={cooldown_end.isoformat()}"
                )
                state = state_for(site)
                state["status"] = "cooldown"
                state["cooldown_until"] = cooldown_end.isoformat()
                state["searches"][name] = {
                    "status": "cooldown",
                    "results": 0,
                    "cooldown_until": cooldown_end.isoformat(),
                }
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
                    if persist_state:
                        scrape_state.record_success(site, 0)
                    state = state_for(site)
                    state["status"] = "success"
                    state["searches"][name] = {
                        "status": "success",
                        "results": 0,
                    }
                    print(f"search={name} site={site} scraped=0")
                    continue

                jobs = jobs.copy()
                if "site" not in jobs.columns:
                    jobs["site"] = site
                else:
                    jobs["site"] = jobs["site"].fillna(site)

                results.append(jobs)
                if persist_state:
                    scrape_state.record_success(site, len(jobs))
                state = state_for(site)
                state["status"] = "success"
                state["results"] = int(state["results"]) + len(jobs)
                state["searches"][name] = {
                    "status": "success",
                    "results": len(jobs),
                }
                print(f"search={name} site={site} scraped={len(jobs)}")
            except Exception as exc:  # One provider must not stop other providers.
                message = str(exc)
                if "429" in message:
                    blocked_sites.add(site)
                    cooldown_end = None
                    if persist_state:
                        cooldown_end = scrape_state.record_blocked(
                            site,
                            base_cooldown_minutes=cooldown_minutes,
                            max_cooldown_minutes=max_cooldown_minutes,
                        )
                    state = state_for(site)
                    if state["status"] != "success":
                        state["status"] = "blocked_429"
                    state["cooldown_until"] = (
                        cooldown_end.isoformat() if cooldown_end else None
                    )
                    state["errors"].append(message[:200])
                    state["searches"][name] = {
                        "status": "blocked_429",
                        "results": 0,
                        "cooldown_until": state["cooldown_until"],
                        "error": message[:200],
                    }
                    print(
                        f"search={name} site={site} blocked=429; "
                        + (
                            f"cooldown_until={cooldown_end.isoformat()}"
                            if cooldown_end
                            else "dry_run=no_cooldown_written"
                        )
                    )
                else:
                    if persist_state:
                        scrape_state.record_failure(
                            site, f"{type(exc).__name__}: {message}"
                        )
                    state = state_for(site)
                    error = f"{type(exc).__name__}: {message}"[:200]
                    if state["status"] != "success":
                        state["status"] = "failure"
                    state["errors"].append(error)
                    state["searches"][name] = {
                        "status": "failure",
                        "results": 0,
                        "error": error,
                    }
                    print(
                        f"search={name} site={site} "
                        f"failed={type(exc).__name__}: {message}"
                    )

    jobs = pd.concat(results, ignore_index=True) if results else pd.DataFrame()
    outcome = ScrapeOutcome(jobs=jobs, provider_status=provider_status)
    return outcome if return_report else jobs
