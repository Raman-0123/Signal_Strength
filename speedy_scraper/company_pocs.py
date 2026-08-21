from __future__ import annotations

import argparse
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from speedy_scraper.background_jobs import (
    JobHeartbeat,
    read_json,
    stop_requested,
    update_status,
    write_json,
)
from speedy_scraper.linkedin import linkedin_id, normalize_linkedin_url
from speedy_scraper.models import DEFAULT_SOURCE_NAMES
from speedy_scraper.parser import candidates_from_results
from speedy_scraper.pipeline import load_existing_people_keys, load_existing_urls
from speedy_scraper.sources import (
    SearchSource,
    SourceError,
    build_sources,
    close_sources,
    configure_google_challenge_wait,
)
from speedy_scraper.taxonomy import resolve_role
from speedy_scraper.text import (
    any_term_in_text,
    clean_spaces,
    normalize_text,
    or_group,
    term_in_text,
    unique_terms,
)
from speedy_scraper.validator import (
    company_match_strength,
    company_matches,
    location_match,
    role_match_strength,
    role_matches,
)

COMPLETED_WITH_WARNINGS = "completed_with_warnings"
DEFAULT_RETRY_ATTEMPTS = 2
REVIEWABLE_REASONS = {"company_mismatch", "designation_mismatch", "invalid_name"}
DEFAULT_QUERY_EXCLUDES = ("jobs", "hiring", "recruiter", "recruitment", "careers")


@dataclass(frozen=True)
class CompanyPoc:
    name: str
    designation: str
    company: str
    linkedin_url: str
    source: str
    confidence: int
    evidence: str
    requested_company: str
    requested_designation: str

    def as_row(self) -> dict[str, object]:
        return {
            "Name": self.name,
            "Designation": self.designation,
            "Company": self.company,
            "LinkedIn ID": linkedin_id(self.linkedin_url),
            "LinkedIn URL": self.linkedin_url,
            "Source": self.source,
            "Confidence": self.confidence,
            "Match Evidence": self.evidence,
            "Requested Company": self.requested_company,
            "Requested Designation": self.requested_designation,
        }


def build_company_poc_tasks(
    companies: list[str],
    designations: list[str],
    locations: list[str] = None,
    include_terms: list[str] | None = None,
    exclude_terms: list[str] | None = None,
    query_passes: int = 1,
) -> list[dict[str, str]]:
    base_tasks: list[dict[str, str]] = []
    locs = unique_terms(locations) if locations else [""]
    for company in unique_terms(companies):
        for designation in unique_terms(designations):
            for loc in locs:
                role_clause = or_group(_role_aliases(designation))
                loc_clause = f' "{_safe_quote(loc)}"' if loc else ""
                include_clause = " ".join(f'"{_safe_quote(term)}"' for term in unique_terms(include_terms or []))
                excludes = unique_terms([*DEFAULT_QUERY_EXCLUDES, *(exclude_terms or [])])
                exclude_clause = " ".join(f'-"{_safe_quote(term)}"' for term in excludes)
                base_tasks.append(
                    {
                        "company": company,
                        "designation": designation,
                        "location": loc,
                        "query": (
                            f'site:linkedin.com/in "{_safe_quote(company)}" '
                            f'{role_clause}{loc_clause} {include_clause} {exclude_clause}'
                        ).strip(),
                    }
                )
    passes = max(1, min(int(query_passes or 1), 8))
    if passes == 1:
        return base_tasks

    tasks: list[dict[str, str]] = []
    for task in base_tasks:
        for variant_index, query in enumerate(_company_query_variants(task), start=1):
            if variant_index > passes:
                break
            tasks.append({
                **task,
                "query": query,
                "query_variant": str(variant_index),
            })
    return tasks


def _company_query_variants(task: dict[str, str]) -> list[str]:
    """Create distinct high-signal queries for user-selected search depth."""
    base = str(task.get("query") or "").strip()
    company = _safe_quote(str(task.get("company") or ""))
    designation = _safe_quote(str(task.get("designation") or ""))
    location = _safe_quote(str(task.get("location") or ""))
    if not base:
        return []

    candidates = [
        base,
        f'{base} "{designation}"',
        f'{base} intitle:"{designation}"',
        f'{base} "{company}" "{designation}"',
        f'{base} "{designation}" "{location}"' if location else f'{base} "{company}"',
        (
            f'{base} intitle:"{designation}" "{company}" "{location}"'
            if location
            else f'{base} intitle:"{designation}" "{company}"'
        ),
        f'{base} "{company}" "{designation}" -jobs -recruiter',
        f'{base} intitle:"{designation}" -jobs -recruiter',
    ]
    return list(dict.fromkeys(" ".join(value.split()) for value in candidates if value.strip()))


def run_company_poc_job(
    job_dir: Path | str,
    *,
    source_builder=None,
) -> list[CompanyPoc]:
    path = Path(job_dir)
    config = read_json(path / "config.json", default={})
    if not isinstance(config, dict):
        raise ValueError("Invalid company POC job config")
    checkpoint_path = path / "checkpoint.json"
    checkpoint = read_json(checkpoint_path, default={})
    tasks = build_company_poc_tasks(
        _strings(config.get("companies")), 
        _strings(config.get("designations")),
        _strings(config.get("locations")),
        _strings(config.get("include_terms")),
        _strings(config.get("exclude_terms")),
        int(config.get("query_passes") or 1),
    )
    if not tasks:
        raise ValueError("Enter at least one company and one designation")
    requested_sources = _strings(config.get("sources")) or list(DEFAULT_SOURCE_NAMES)
    sources: list[SearchSource] = configure_google_challenge_wait(
        (source_builder or build_sources)(requested_sources),
        int(config.get("google_manual_challenge_seconds") or 0),
    )
    target_count = max(1, int(config.get("target_count") or 100))
    max_results = max(1, int(config.get("max_results_per_search") or 20))
    headless = bool(config.get("browser_headless", True))
    pocs = (
        filter_company_pocs([_poc_from_data(item) for item in checkpoint.get("pocs", [])])
        if checkpoint
        else []
    )
    rejections = list(checkpoint.get("rejections", [])) if checkpoint else []
    errors = list(checkpoint.get("errors", [])) if checkpoint else []
    source_errors = list(
        (checkpoint.get("source_errors") or checkpoint.get("errors") or [])
    ) if checkpoint else []
    task_index = int(checkpoint.get("task_index") or 0) if checkpoint else 0
    source_index = int(checkpoint.get("source_index") or 0) if checkpoint else 0
    retry_queue = [
        dict(item) for item in checkpoint.get("retry_queue", [])
        if isinstance(item, dict)
    ] if checkpoint else []
    retry_queue = _coalesce_retry_queue(retry_queue)
    if config.get("retry_failed_searches") and not retry_queue and source_errors:
        # Migrate pre-v2 completed jobs that only stored provider errors. Their
        # exact failed cursor was not persisted, so replay each task once through
        # the current fallback policy instead of silently doing nothing.
        failed_source = str(source_errors[0]).split(":", 1)[0].strip() or "google_browser"
        for index, task in enumerate(tasks):
            retry_queue = _upsert_retry(
                retry_queue,
                task_index=index,
                task=task,
                failed_source=failed_source,
                reason="Migrated from a pre-recovery checkpoint",
                challenge="challenge" in " ".join(source_errors).lower(),
            )
    if config.get("retry_failed_searches") and retry_queue:
        # A targeted recovery is allowed to revisit the provider that previously
        # failed; the user may have solved its CAPTCHA or the provider may have
        # recovered since the original attempt.
        for item in retry_queue:
            item["attempted_sources"] = []
            item["attempts"] = 0
        config["retry_failed_searches"] = False
        write_json(path / "config.json", config)
    retry_attempts = max(1, int(config.get("retry_attempts") or DEFAULT_RETRY_ATTEMPTS))
    include_terms = _strings(config.get("include_terms"))
    exclude_terms = _strings(config.get("exclude_terms"))
    provider_outcomes = _provider_outcomes(
        checkpoint.get("provider_outcomes") if checkpoint else None,
        [source.name for source in sources],
    )
    existing_files = [Path(item) for item in _strings(config.get("existing_files"))]
    existing_urls = load_existing_urls(existing_files)
    existing_people = load_existing_people_keys(existing_files)
    seen = {poc.linkedin_url for poc in pocs if poc.linkedin_url} | existing_urls
    seen_people = {f"{normalize_text(poc.name)}|{normalize_text(poc.company)}" for poc in pocs}
    seen_people |= existing_people
    captcha_required = bool(checkpoint.get("captcha_required")) if checkpoint else False
    disabled_sources: set[str] = set()
    successful_task_keys: set[tuple[int, str]] = set()
    try:
        _write_poc_status(
            path,
            "running",
            pocs,
            task_index,
            len(tasks),
            "POC search running",
            searches_completed=task_index * len(sources) + source_index,
            searches_total=len(tasks) * len(sources),
            provider_outcomes=provider_outcomes,
            retry_count=sum(int(item.get("attempts") or 0) for item in retry_queue),
            failed_searches=len(retry_queue),
        )
        while task_index < len(tasks) and len(pocs) < target_count:
            if stop_requested(path):
                _save_poc_checkpoint(
                    checkpoint_path,
                    pocs,
                    rejections,
                    errors,
                    task_index,
                    source_index,
                    retry_queue=retry_queue,
                    provider_outcomes=provider_outcomes,
                    source_errors=source_errors,
                    captcha_required=captcha_required,
                )
                csv_path, xlsx_path = write_company_poc_exports(
                    pocs, rejections, path / "Company_Designation_POCs_partial"
                )
                _write_poc_status(
                    path,
                    "paused",
                    pocs,
                    task_index,
                    len(tasks),
                    "Paused safely; completed searches will not repeat",
                    csv_path=str(csv_path),
                    xlsx_path=str(xlsx_path),
                )
                return pocs

            task = tasks[task_index]
            source = sources[source_index]
            searches_completed = task_index * len(sources) + source_index
            _write_poc_status(
                path,
                "running",
                pocs,
                task_index,
                len(tasks),
                f"Searching {task['company']} · {task['designation']} · {source.name}",
                searches_completed=searches_completed,
                searches_total=len(tasks) * len(sources),
                current_company=task["company"],
                current_designation=task["designation"],
                current_source=source.name,
                current_query=task["query"],
                provider_outcomes=provider_outcomes,
                retry_count=sum(int(item.get("attempts") or 0) for item in retry_queue),
                failed_searches=len(retry_queue),
            )
            task_key = _retry_key(task_index, task)
            if source.name in disabled_sources:
                # A challenged browser provider is disabled for the rest of this
                # run. Do not repeatedly hit the same blocked session for every
                # company/role pair; retain one company-scoped recovery item.
                if task_key not in successful_task_keys:
                    retry_queue = _upsert_retry(
                        retry_queue,
                        task_index=task_index,
                        task=task,
                        failed_source=source.name,
                        reason=f"{source.name} was disabled after a provider challenge",
                        challenge=True,
                    )
                source_index += 1
                if source_index >= len(sources):
                    source_index = 0
                    task_index += 1
                _save_poc_checkpoint(
                    checkpoint_path,
                    pocs,
                    rejections,
                    errors,
                    task_index,
                    source_index,
                    retry_queue=retry_queue,
                    provider_outcomes=provider_outcomes,
                    source_errors=source_errors,
                    captcha_required=captcha_required,
                )
                continue
            try:
                results = []
                with JobHeartbeat(
                    path,
                    activity="Public search request in progress",
                    current_company=task["company"],
                    current_designation=task["designation"],
                    current_source=source.name,
                ):
                    results = source.search(
                        task["query"], max_results=max_results, headless=headless
                    )
                _record_provider_outcome(
                    provider_outcomes,
                    source.name,
                    result_count=len(results),
                    empty=not results,
                )
                if results:
                    successful_task_keys.add(task_key)
                    retry_queue = _remove_retry_for_task(retry_queue, task_key)
            except SourceError as exc:
                message = f"{source.name}: {exc}"
                if message not in errors:
                    errors.append(message)
                if message not in source_errors:
                    source_errors.append(message)
                results = []
                captcha_required = captcha_required or exc.challenge
                if exc.disable_source:
                    disabled_sources.add(source.name)
                _record_provider_outcome(
                    provider_outcomes,
                    source.name,
                    attempt=True,
                    error=True,
                    challenge=exc.challenge,
                )
                if task_key not in successful_task_keys:
                    retry_queue = _upsert_retry(
                        retry_queue,
                        task_index=task_index,
                        task=task,
                        failed_source=source.name,
                        reason=str(exc),
                        challenge=exc.challenge,
                    )
                update_status(
                    path,
                    source_errors=source_errors,
                    captcha_required=captcha_required,
                    captcha_source=source.name,
                    fallback_recommended=True,
                    provider_outcomes=provider_outcomes,
                    failed_searches=len(retry_queue),
                )
            _consume_results(
                results,
                task,
                pocs,
                rejections,
                seen,
                seen_people,
                include_terms=include_terms,
                exclude_terms=exclude_terms,
            )

            source_index += 1
            if source_index >= len(sources):
                source_index = 0
                task_index += 1
            _save_poc_checkpoint(
                checkpoint_path,
                pocs,
                rejections,
                errors,
                task_index,
                source_index,
                retry_queue=retry_queue,
                provider_outcomes=provider_outcomes,
                source_errors=source_errors,
                captcha_required=captcha_required,
            )
            _write_poc_status(
                path,
                "running",
                pocs,
                task_index,
                len(tasks),
                f"Found {len(pocs)} POCs · completed {task_index}/{len(tasks)} query groups",
                searches_completed=task_index * len(sources) + source_index,
                searches_total=len(tasks) * len(sources),
                current_company=task["company"],
                current_designation=task["designation"],
                current_source=source.name,
                captcha_required=captcha_required,
                captcha_source=source.name if captcha_required else "",
                provider_outcomes=provider_outcomes,
                retry_count=sum(int(item.get("attempts") or 0) for item in retry_queue),
                failed_searches=len(retry_queue),
                source_errors=source_errors,
            )

        retry_queue, captcha_required = _retry_failed_searches(
            path,
            tasks,
            sources,
            pocs,
            rejections,
            seen,
            seen_people,
            retry_queue,
            provider_outcomes,
            errors,
            source_errors,
            captcha_required,
            max_results=max_results,
            headless=headless,
            max_attempts=retry_attempts,
            checkpoint_path=checkpoint_path,
            include_terms=include_terms,
            exclude_terms=exclude_terms,
        )

        csv_path, xlsx_path = write_company_poc_exports(
            pocs, rejections, path / "Company_Designation_POCs"
        )
        final_state = COMPLETED_WITH_WARNINGS if retry_queue or errors else "completed"
        final_message = (
            f"Completed with warnings: {len(pocs)} matched POCs; "
            f"{len(retry_queue)} searches still need recovery"
            if final_state == COMPLETED_WITH_WARNINGS
            else f"Completed with {len(pocs)} matched POCs"
        )
        _save_poc_checkpoint(
            checkpoint_path,
            pocs,
            rejections,
            errors,
            task_index,
            source_index,
            retry_queue=retry_queue,
            provider_outcomes=provider_outcomes,
            source_errors=source_errors,
            captcha_required=captcha_required,
            warning_state=final_state,
        )
        _write_poc_status(
            path,
            final_state,
            pocs,
            task_index,
            len(tasks),
            final_message,
            csv_path=str(csv_path),
            xlsx_path=str(xlsx_path),
            provider_outcomes=provider_outcomes,
            retry_count=sum(int(item.get("attempts") or 0) for item in retry_queue),
            failed_searches=len(retry_queue),
            source_errors=source_errors,
            captcha_required=captcha_required,
            manual_recovery_available=bool(captcha_required),
            cloud_manual_recovery=False,
            fallback_recommended=bool(source_errors),
        )
        return pocs
    except Exception as exc:
        _save_poc_checkpoint(
            checkpoint_path,
            pocs,
            rejections,
            errors,
            task_index,
            source_index,
            retry_queue=retry_queue,
            provider_outcomes=provider_outcomes,
            source_errors=source_errors,
            captcha_required=captcha_required,
        )
        _write_poc_status(path, "failed", pocs, task_index, len(tasks), str(exc))
        raise
    finally:
        close_sources(sources)


def _provider_outcomes(value: object, source_names: list[str]) -> dict[str, dict[str, int]]:
    raw = value if isinstance(value, dict) else {}
    outcomes: dict[str, dict[str, int]] = {}
    for name in unique_terms(source_names):
        existing = raw.get(name) if isinstance(raw.get(name), dict) else {}
        outcomes[name] = {
            "attempts": int(existing.get("attempts") or 0),
            "successful_searches": int(existing.get("successful_searches") or 0),
            "empty_searches": int(existing.get("empty_searches") or 0),
            "errors": int(existing.get("errors") or 0),
            "challenges": int(existing.get("challenges") or 0),
            "results": int(existing.get("results") or 0),
        }
    return outcomes


def _record_provider_outcome(
    outcomes: dict[str, dict[str, int]],
    source_name: str,
    *,
    attempt: bool = False,
    result_count: int | None = None,
    empty: bool = False,
    error: bool = False,
    challenge: bool = False,
) -> None:
    bucket = outcomes.setdefault(
        source_name,
        {
            "attempts": 0,
            "successful_searches": 0,
            "empty_searches": 0,
            "errors": 0,
            "challenges": 0,
            "results": 0,
        },
    )
    if attempt or result_count is not None:
        bucket["attempts"] += 1
    if result_count is not None:
        bucket["results"] += max(0, int(result_count))
        if result_count:
            bucket["successful_searches"] += 1
    if empty:
        bucket["empty_searches"] += 1
    if error:
        bucket["errors"] += 1
    if challenge:
        bucket["challenges"] += 1


def _upsert_retry(
    queue: list[dict[str, Any]],
    *,
    task_index: int,
    task: dict[str, str],
    failed_source: str,
    reason: str,
    challenge: bool,
) -> list[dict[str, Any]]:
    key = _retry_key(task_index, task)
    for item in queue:
        item_key = _retry_key(int(item.get("task_index") or 0), item)
        if item_key == key:
            item["reason"] = reason
            item["challenge"] = bool(item.get("challenge") or challenge)
            attempted = [str(value) for value in item.get("attempted_sources") or []]
            if failed_source not in attempted:
                attempted.append(failed_source)
            item["attempted_sources"] = attempted
            failed_sources = [str(value) for value in item.get("failed_sources") or []]
            if failed_source not in failed_sources:
                failed_sources.append(failed_source)
            item["failed_sources"] = failed_sources
            item["failed_source"] = failed_source
            return queue
    queue.append(
        {
            "task_index": int(task_index),
            "company": str(task.get("company") or ""),
            "designation": str(task.get("designation") or ""),
            "location": str(task.get("location") or ""),
            "query": str(task.get("query") or ""),
            "failed_source": failed_source,
            "failed_sources": [failed_source],
            "attempted_sources": [failed_source],
            "attempts": 0,
            "reason": reason,
            "challenge": bool(challenge),
        }
    )
    return queue


def _retry_key(task_index: int, task: dict[str, Any]) -> tuple[int, str]:
    return int(task_index), str(task.get("query") or "")


def _coalesce_retry_queue(queue: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge legacy provider-scoped entries into one company-scoped retry item."""
    merged: dict[tuple[int, str], dict[str, Any]] = {}
    order: list[tuple[int, str]] = []
    for raw in queue:
        item = dict(raw)
        key = _retry_key(int(item.get("task_index") or 0), item)
        current = merged.get(key)
        if current is None:
            item["attempted_sources"] = [
                str(value) for value in item.get("attempted_sources") or [] if str(value)
            ]
            failed_sources = [
                str(value) for value in item.get("failed_sources") or [] if str(value)
            ]
            if item.get("failed_source") and str(item["failed_source"]) not in failed_sources:
                failed_sources.append(str(item["failed_source"]))
            item["failed_sources"] = failed_sources
            merged[key] = item
            order.append(key)
            continue
        attempted = [str(value) for value in current.get("attempted_sources") or []]
        for value in item.get("attempted_sources") or []:
            if str(value) and str(value) not in attempted:
                attempted.append(str(value))
        failed_sources = [str(value) for value in current.get("failed_sources") or []]
        for value in [*(item.get("failed_sources") or []), item.get("failed_source")]:
            if str(value) and str(value) not in failed_sources:
                failed_sources.append(str(value))
        current["attempted_sources"] = attempted
        current["failed_sources"] = failed_sources
        current["failed_source"] = str(item.get("failed_source") or current.get("failed_source") or "")
        current["reason"] = str(item.get("reason") or current.get("reason") or "")
        current["challenge"] = bool(current.get("challenge") or item.get("challenge"))
        current["attempts"] = max(int(current.get("attempts") or 0), int(item.get("attempts") or 0))
    return [merged[key] for key in order]


def _remove_retry_for_task(
    queue: list[dict[str, Any]], key: tuple[int, str]
) -> list[dict[str, Any]]:
    return [
        item for item in queue
        if _retry_key(int(item.get("task_index") or 0), item) != key
    ]


def _consume_results(
    results: list,
    task: dict[str, str],
    pocs: list[CompanyPoc],
    rejections: list[dict[str, Any]],
    seen: set[str],
    seen_people: set[str],
    *,
    include_terms: list[str] | None = None,
    exclude_terms: list[str] | None = None,
) -> None:
    rejected_urls = {
        str(item.get("LinkedIn URL") or "")
        for item in rejections
        if isinstance(item, dict)
    }
    for candidate in candidates_from_results(results):
        canonical = normalize_linkedin_url(candidate.linkedin_url)
        identity = f"{normalize_text(candidate.name)}|{normalize_text(candidate.company)}"
        if not canonical or canonical in seen or identity in seen_people:
            continue
        poc, reason = _match_candidate(
            candidate,
            task["company"],
            task["designation"],
            location=task.get("location", ""),
            include_terms=include_terms or [],
            exclude_terms=exclude_terms or [],
        )
        if poc:
            pocs.append(poc)
            seen.add(canonical)
            seen_people.add(f"{normalize_text(poc.name)}|{normalize_text(poc.company)}")
            continue
        if canonical in rejected_urls:
            continue
        rejected_urls.add(canonical)
        rejections.append(
            {
                "Name": candidate.name,
                "Designation": candidate.designation,
                "Company": candidate.company,
                "LinkedIn URL": canonical,
                "Source": candidate.source,
                "Evidence": clean_spaces(f"{candidate.title} {candidate.body}")[:700],
                "Requested Company": task["company"],
                "Requested Designation": task["designation"],
                "Reason": reason,
                "Reviewable": reason in REVIEWABLE_REASONS,
            }
        )


def _retry_failed_searches(
    path: Path,
    tasks: list[dict[str, str]],
    sources: list[SearchSource],
    pocs: list[CompanyPoc],
    rejections: list[dict[str, Any]],
    seen: set[str],
    seen_people: set[str],
    queue: list[dict[str, Any]],
    provider_outcomes: dict[str, dict[str, int]],
    errors: list[str],
    source_errors: list[str],
    captcha_required: bool,
    *,
    max_results: int,
    headless: bool,
    max_attempts: int,
    checkpoint_path: Path,
    include_terms: list[str],
    exclude_terms: list[str],
) -> tuple[list[dict[str, Any]], bool]:
    source_by_name = {source.name: source for source in sources}
    remaining: list[dict[str, Any]] = []
    for item in queue:
        task = {
            "company": str(item.get("company") or ""),
            "designation": str(item.get("designation") or ""),
            "location": str(item.get("location") or ""),
            "query": str(item.get("query") or ""),
        }
        attempted = [str(value) for value in item.get("attempted_sources") or []]
        attempts = int(item.get("attempts") or 0)
        while attempts < max_attempts:
            fallback = next(
                (
                    source for source in sources
                    if source.name not in attempted
                    and source.name in source_by_name
                ),
                None,
            )
            if fallback is None:
                break
            attempted.append(fallback.name)
            attempts += 1
            _write_poc_status(
                path,
                "running",
                pocs,
                int(item.get("task_index") or 0),
                len(tasks),
                f"Retrying {task['company']} · {task['designation']} with {fallback.name}",
                current_company=task["company"],
                current_designation=task["designation"],
                current_source=fallback.name,
                current_query=task["query"],
                provider_outcomes=provider_outcomes,
                retry_count=attempts,
                failed_searches=len(queue),
            )
            try:
                with JobHeartbeat(
                    path,
                    activity="Retrying a failed public search",
                    current_company=task["company"],
                    current_designation=task["designation"],
                    current_source=fallback.name,
                ):
                    results = fallback.search(task["query"], max_results=max_results, headless=headless)
                _record_provider_outcome(
                    provider_outcomes,
                    fallback.name,
                    result_count=len(results),
                    empty=not results,
                )
                _consume_results(
                    results,
                    task,
                    pocs,
                    rejections,
                    seen,
                    seen_people,
                    include_terms=include_terms,
                    exclude_terms=exclude_terms,
                )
                if results:
                    item["resolved_by"] = fallback.name
                    item["attempts"] = attempts
                    break
            except SourceError as exc:
                message = f"{fallback.name}: {exc}"
                if message not in errors:
                    errors.append(message)
                if message not in source_errors:
                    source_errors.append(message)
                captcha_required = captcha_required or exc.challenge
                item["reason"] = str(exc)
                _record_provider_outcome(
                    provider_outcomes,
                    fallback.name,
                    attempt=True,
                    error=True,
                    challenge=exc.challenge,
                )
                item["challenge"] = bool(item.get("challenge") or exc.challenge)
            item["attempted_sources"] = attempted
            item["attempts"] = attempts
            _save_poc_checkpoint(
                checkpoint_path,
                pocs,
                rejections,
                errors,
                len(tasks),
                0,
                retry_queue=queue,
                provider_outcomes=provider_outcomes,
                source_errors=source_errors,
                captcha_required=captcha_required,
            )
        if not item.get("resolved_by"):
            item["attempted_sources"] = attempted
            item["attempts"] = attempts
            remaining.append(item)
    return remaining, captcha_required


def load_company_poc_checkpoint(job_dir: Path | str) -> tuple[list[CompanyPoc], list[dict[str, str]]]:
    value = read_json(Path(job_dir) / "checkpoint.json", default={})
    if not isinstance(value, dict):
        return [], []
    pocs = filter_company_pocs([_poc_from_data(item) for item in value.get("pocs", [])])
    return pocs, list(value.get("rejections", []))


def filter_company_pocs(pocs: list[CompanyPoc]) -> list[CompanyPoc]:
    filtered = [
        poc for poc in pocs
        if role_matches(poc.designation, [poc.requested_designation])
        and company_matches(poc.company, poc.requested_company)
    ]
    unique: dict[str, CompanyPoc] = {}
    for poc in filtered:
        key = normalize_linkedin_url(poc.linkedin_url) or f"{normalize_text(poc.name)}|{normalize_text(poc.company)}"
        current = unique.get(key)
        if current is None or poc.confidence > current.confidence:
            unique[key] = poc
    return list(unique.values())


def company_pocs_frame(pocs: list[CompanyPoc]) -> pd.DataFrame:
    columns = [
        "Name",
        "Designation",
        "Company",
        "LinkedIn ID",
        "LinkedIn URL",
        "Source",
        "Confidence",
        "Match Evidence",
        "Requested Company",
        "Requested Designation",
    ]
    return pd.DataFrame([poc.as_row() for poc in pocs], columns=columns)


def company_poc_review_frame(rejections: list[dict[str, Any]]) -> pd.DataFrame:
    """Return near-matches separately so strict verified output stays clean."""
    columns = [
        "Name",
        "Designation",
        "Company",
        "LinkedIn URL",
        "Source",
        "Evidence",
        "Requested Company",
        "Requested Designation",
        "Reason",
    ]
    rows = [
        {column: item.get(column, "") for column in columns}
        for item in rejections
        if isinstance(item, dict)
        and bool(item.get("Reviewable", str(item.get("Reason") or "") in REVIEWABLE_REASONS))
    ]
    return pd.DataFrame(rows, columns=columns)


def write_company_poc_exports(
    pocs: list[CompanyPoc], rejections: list[dict[str, str]], output_base: Path | str
) -> tuple[Path, Path]:
    base = Path(output_base)
    base.parent.mkdir(parents=True, exist_ok=True)
    csv_path = base.with_suffix(".csv")
    xlsx_path = base.with_suffix(".xlsx")
    frame = company_pocs_frame(pocs)
    frame.to_csv(csv_path, index=False)
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Matched POCs", index=False)
        pd.DataFrame(rejections).to_excel(writer, sheet_name="Rejected", index=False)
    return csv_path, xlsx_path


def _match_candidate(
    candidate,
    company: str,
    designation: str,
    *,
    location: str = "",
    include_terms: list[str] | None = None,
    exclude_terms: list[str] | None = None,
) -> tuple[CompanyPoc | None, str]:
    name = clean_spaces(candidate.name)
    if not _valid_name(name):
        return None, "invalid_name"
    match_text = clean_spaces(
        f"{candidate.name} {candidate.designation} {candidate.company} "
        f"{candidate.title} {candidate.body} {candidate.evidence}"
    )
    if exclude_terms and any_term_in_text(match_text, exclude_terms):
        return None, "excluded_terms"
    if include_terms and not all(term_in_text(match_text, term) for term in include_terms):
        return None, "required_terms"
    parsed_company = clean_spaces(candidate.company)
    company_strength = company_match_strength(parsed_company, company)
    if not company_strength:
        return None, "company_mismatch"
    parsed_designation = clean_spaces(candidate.designation)
    role_strength = role_match_strength(parsed_designation, designation)
    if not role_strength:
        return None, "designation_mismatch"
    if location and not location_match(candidate, [location]):
        return None, "location_mismatch"
    evidence = clean_spaces(f"{candidate.title} {candidate.body}")[:700]
    confidence = 68 + company_strength * 4 + role_strength * 4
    if len(candidate.sources_seen) > 1:
        confidence += 4
    return (
        CompanyPoc(
            name=name,
            designation=parsed_designation,
            company=parsed_company,
            linkedin_url=normalize_linkedin_url(candidate.linkedin_url),
            source=", ".join(sorted(candidate.sources_seen)) or candidate.source,
            confidence=min(confidence, 96),
            evidence=evidence,
            requested_company=company,
            requested_designation=designation,
        ),
        "",
    )


def _valid_name(value: str) -> bool:
    key = normalize_text(value)
    return (
        key not in {"", "unknown", "linkedin member", "new cio", "new cto", "new cdo"}
        and 2 <= len(key.split()) <= 8
        and not bool(re.search(r"\d|@", value))
    )


def _role_aliases(designation: str) -> list[str]:
    return resolve_role(designation).terms


def _safe_quote(value: str) -> str:
    return clean_spaces(value).replace('"', "'")


def _save_poc_checkpoint(
    path: Path,
    pocs: list[CompanyPoc],
    rejections: list[dict[str, str]],
    errors: list[str],
    task_index: int,
    source_index: int,
    *,
    retry_queue: list[dict[str, Any]] | None = None,
    provider_outcomes: dict[str, dict[str, int]] | None = None,
    source_errors: list[str] | None = None,
    captcha_required: bool = False,
    warning_state: str = "",
) -> None:
    write_json(
        path,
        {
            "version": 2,
            "task_index": task_index,
            "source_index": source_index,
            "pocs": [asdict(poc) for poc in pocs],
            "rejections": rejections,
            "errors": errors,
            "source_errors": source_errors or errors,
            "retry_queue": retry_queue or [],
            "provider_outcomes": provider_outcomes or {},
            "captcha_required": captcha_required,
            "warning_state": warning_state,
        },
    )


def _write_poc_status(
    path: Path,
    state: str,
    pocs: list[CompanyPoc],
    processed: int,
    total: int,
    message: str,
    **extra: object,
) -> None:
    update_status(
        path,
        state=state,
        workflow="company_pocs",
        job_id=path.name,
        processed=processed,
        total=total,
        matched=len(pocs),
        message=message,
        **extra,
    )


def _poc_from_data(value: dict[str, Any]) -> CompanyPoc:
    return CompanyPoc(
        name=str(value.get("name") or ""),
        designation=str(value.get("designation") or ""),
        company=str(value.get("company") or ""),
        linkedin_url=str(value.get("linkedin_url") or ""),
        source=str(value.get("source") or ""),
        confidence=int(value.get("confidence") or 0),
        evidence=str(value.get("evidence") or ""),
        requested_company=str(value.get("requested_company") or ""),
        requested_designation=str(value.get("requested_designation") or ""),
    )


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [clean_spaces(str(item)) for item in value if clean_spaces(str(item))]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-dir", type=Path, required=True)
    args = parser.parse_args()
    run_company_poc_job(args.job_dir)


if __name__ == "__main__":
    main()
