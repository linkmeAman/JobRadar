"""Entrypoint for scheduled and diagnostic Job Radar runs."""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import pandas as pd
import yaml

from . import (
    application_tracker,
    dedupe,
    matcher,
    notifier,
    resume_profile,
    run_lock,
    scrape_state,
    scraper,
    search_generator,
    semantic,
)
from .sources import runner as source_runner


CONFIG_PATH = Path("config.yaml")


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Load and validate the Job Radar configuration shape."""
    with path.open(encoding="utf-8") as config_file:
        config = yaml.safe_load(config_file) or {}

    if not isinstance(config.get("searches"), list):
        raise ValueError("config.yaml must define searches as a list")
    for section in ("resume", "scraping", "matching"):
        if not isinstance(config.get(section), dict):
            raise ValueError(f"config.yaml must define {section} settings")
    return config


def _matching_options(config: dict[str, Any]) -> dict[str, Any]:
    matching = config["matching"]
    return {
        "minimum_score": int(matching.get("minimum_score", 6)),
        "excluded_title_terms": list(
            matching.get("excluded_title_terms", [])
        ),
        "seniority_penalty_terms": list(
            matching.get("seniority_penalty_terms", [])
        ),
        "maximum_required_years": int(
            matching.get("maximum_required_years", 5)
        ),
        "allowed_countries": list(
            matching.get("allowed_countries", ["India"])
        ),
    }


def _print_explanations(evaluated: pd.DataFrame) -> None:
    """Print one compact, tuneable decision record per scraped job."""
    if evaluated.empty:
        print("explain=no_jobs")
        return
    seen = dedupe.seen_status(evaluated)
    for position, (_, row) in enumerate(evaluated.iterrows(), start=1):
        required = row.get("required_experience")
        required_text = (
            "not_detected" if pd.isna(required) else str(int(required))
        )
        exclusion = row.get("exclusion_reason")
        exclusion_text = (
            "none" if exclusion is None or pd.isna(exclusion) else str(exclusion)
        )
        print(
            "explain={position} job_id={job_id} title={title!r} company={company!r} "
            "score={score} reasons={reasons!r} excluded={excluded!r} "
            "required_years={required} country_eligible={country} "
            "country_reason={country_reason!r} seen={seen}".format(
                position=position,
                job_id=dedupe.job_id_for(row)[:12],
                title=str(row.get("title", "")),
                company=str(row.get("company", "")),
                score=row.get("match_score", 0),
                reasons=str(row.get("match_reasons", "")),
                excluded=exclusion_text,
                required=required_text,
                country=bool(row.get("country_eligible", False)),
                country_reason=str(row.get("country_eligibility", "")),
                seen=bool(seen.iloc[position - 1]),
            )
        )


def _selected_searches(
    config: dict[str, Any],
    profile: dict[str, Any],
    *,
    advance: bool,
) -> list[dict[str, Any]]:
    searches = search_generator.generate_searches(
        profile,
        config.get("dynamic_searches", {}),
        config["searches"],
    )
    scraping = config["scraping"]
    return [
        dict(search)
        for search in scrape_state.select_searches(
            searches,
            int(scraping.get("searches_per_run", 2)),
            advance=advance,
        )
    ]


def run(
    *, dry_run: bool = False, explain: bool = False, mode: str = "normal"
) -> dict[str, Any] | None:
    """Execute one locked scrape; diagnostic runs make no SQLite changes."""
    config = load_config()
    with run_lock.single_instance():
        semantic.validate_settings(config.get("semantic", {}))
        if not dry_run:
            notifier.validate_delivery_target()
            commands, reminders = application_tracker.run_automation(
                config.get("applications", {})
            )
            logging.info(
                "application_commands=%d stale_applications=%d",
                commands,
                reminders,
            )

        resume = config["resume"]
        profile = resume_profile.load_or_refresh(
            resume.get("paths") or resume["path"],
            resume.get("cache_path", "data/resume_profile.json"),
        )
        selected = _selected_searches(config, profile, advance=not dry_run)
        external_names = source_runner.configured_names(
            config.get("external_sources", {})
        )
        logging.info(
            "selected_searches=%s",
            ",".join(str(search.get("name", "unnamed")) for search in selected),
        )
        logging.info(
            "external_sources=%s",
            ",".join(external_names) if external_names else "none",
        )

        started_at = datetime.now(timezone.utc)
        history_selections = [
            *selected,
            *[
                {"name": f"source:{source_name}"}
                for source_name in external_names
            ],
        ]
        run_id = (
            None
            if dry_run
            else scrape_state.start_run(history_selections, mode=mode)
        )
        provider_status: dict[str, Any] = {}
        counts = {
            "scraped_count": 0,
            "matched_count": 0,
            "new_count": 0,
            "pending_count": 0,
            "queued_count": 0,
            "deferred_count": 0,
            "expired_count": 0,
            "sent_count": 0,
        }
        all_failed = False
        try:
            scraping = config["scraping"]
            outcome = scraper.run_all(
                selected,
                cooldown_minutes=int(
                    scraping.get("cooldown_minutes", 120)
                ),
                max_cooldown_minutes=int(
                    scraping.get("max_cooldown_minutes", 720)
                ),
                persist_state=not dry_run,
                return_report=True,
            )
            if isinstance(outcome, pd.DataFrame):
                outcome = scraper.ScrapeOutcome(
                    jobs=outcome, provider_status={}
                )
            external_outcome = source_runner.run_all(
                config.get("external_sources", {}),
                persist_state=not dry_run,
                force=dry_run,
            )
            frames = [
                frame
                for frame in (outcome.jobs, external_outcome.jobs)
                if not frame.empty
            ]
            scraped = (
                pd.concat(
                    [
                        frame.dropna(axis=1, how="all")
                        for frame in frames
                    ],
                    ignore_index=True,
                    sort=False,
                )
                if frames
                else pd.DataFrame()
            )
            provider_status = {
                **outcome.provider_status,
                **external_outcome.provider_status,
            }
            all_failed = scraper.ScrapeOutcome(
                scraped, provider_status
            ).all_providers_failed
            counts["scraped_count"] = len(scraped)

            feedback = dedupe.feedback_adjustments(read_only=dry_run)
            options = _matching_options(config)
            evaluated = matcher.evaluate_jobs(
                scraped, profile, feedback=feedback, **options
            )
            semantic_settings = config.get("semantic", {})
            if semantic_settings.get("enabled", False):
                try:
                    evaluated = semantic.apply_semantic_scoring(
                        evaluated, profile, semantic_settings
                    )
                except Exception as exc:
                    if not semantic_settings.get("fail_open", True):
                        raise
                    logging.error(
                        "semantic_scoring=failed error=%s: %s",
                        type(exc).__name__,
                        exc,
                    )

            if dry_run or explain:
                _print_explanations(evaluated)
            matched = matcher.select_matches(
                evaluated, options["minimum_score"]
            )
            counts["matched_count"] = len(matched)

            if dry_run:
                logging.info(
                    "dry_run=true scraped=%d matched=%d writes=0 sent=0",
                    len(scraped),
                    len(matched),
                )
                return None

            new_jobs = dedupe.filter_new(matched)
            counts["new_count"] = len(new_jobs)
            counts["expired_count"] = dedupe.expire_pending(
                int(config["matching"].get("pending_expiry_days", 7))
            )
            all_pending = dedupe.pending_notifications()
            counts["pending_count"] = len(all_pending)
            max_alerts = int(
                config["matching"].get("max_alerts_per_run", 10)
            )
            pending = all_pending.head(max_alerts)
            counts["queued_count"] = len(pending)
            counts["deferred_count"] = max(
                0, len(all_pending) - len(pending)
            )
            counts["sent_count"] = notifier.send_all(
                pending, on_sent=dedupe.mark_notified
            )

            assert run_id is not None
            scrape_state.complete_run(
                run_id,
                started_at=started_at,
                provider_status=provider_status,
                all_providers_failed=all_failed,
                **counts,
            )
            _maybe_send_health_alert(config, run_id, all_failed)
            _maybe_send_provider_alert(config, provider_status)
        except Exception as exc:
            if run_id is not None:
                scrape_state.complete_run(
                    run_id,
                    started_at=started_at,
                    provider_status=provider_status,
                    all_providers_failed=all_failed,
                    status="failed",
                    error=f"{type(exc).__name__}: {exc}",
                    **counts,
                )
            raise

        logging.info(
            "scraped=%d matched=%d new=%d pending=%d queued=%d "
            "deferred=%d expired=%d sent=%d",
            counts["scraped_count"],
            counts["matched_count"],
            counts["new_count"],
            counts["pending_count"],
            counts["queued_count"],
            counts["deferred_count"],
            counts["expired_count"],
            counts["sent_count"],
        )
        return {"run_id": run_id, **counts}


def _maybe_send_health_alert(
    config: dict[str, Any], run_id: int, all_failed: bool
) -> None:
    if not all_failed:
        return
    threshold = int(
        config.get("monitoring", {}).get(
            "all_provider_failure_alert_runs", 3
        )
    )
    streak = scrape_state.consecutive_all_failed_runs()
    if streak < threshold or scrape_state.current_failure_streak_alerted():
        return
    try:
        notifier.send_health_alert(
            f"Every selected provider failed for {streak} consecutive runs."
        )
    except RuntimeError as exc:
        logging.error("health_alert=failed error=%s", exc)
        return
    scrape_state.mark_health_alert_sent(run_id)


def _maybe_send_provider_alert(
    config: dict[str, Any], provider_status: dict[str, Any]
) -> None:
    threshold = int(
        config.get("monitoring", {}).get("provider_failure_alert_runs", 3)
    )
    degraded = scrape_state.degraded_providers(provider_status, threshold)
    if not degraded:
        return
    details = "; ".join(
        f"{item['provider']} ({item['failures']} runs, {item['status']})"
        + (f": {item['error']}" if item.get("error") else "")
        for item in degraded
    )
    try:
        notifier.send_health_alert(f"Provider degradation: {details}")
    except RuntimeError as exc:
        logging.error("provider_health_alert=failed error=%s", exc)
        return
    scrape_state.mark_provider_alert_sent(
        [str(item["provider"]) for item in degraded]
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="job-radar", description="Resume-aware job scraping"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="scrape and explain without SQLite writes or Telegram sends",
    )
    parser.add_argument(
        "--explain",
        action="store_true",
        help="alias for --dry-run with per-job decision output",
    )
    parser.add_argument(
        "--feedback",
        nargs=2,
        metavar=("JOB_ID", "LABEL"),
        help="label one job as relevant, irrelevant, or applied",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parse_argv = argv
    if argv is None and __name__ != "__main__":
        parse_argv = []
    args = _parser().parse_args(parse_argv)
    if args.feedback:
        job_id, label = args.feedback
        if label == "applied":
            change = application_tracker.mark_applied(job_id)
            saved = change.job_id
        else:
            saved = dedupe.record_feedback(job_id, label)
        print(f"feedback_saved job_id={saved} label={label}")
        return
    run(dry_run=args.dry_run or args.explain, explain=args.explain)


if __name__ == "__main__":
    main()
