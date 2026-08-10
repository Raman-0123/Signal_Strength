from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from typing import Any

from speedy_scraper.background_jobs import (
    JobHeartbeat,
    read_json,
    stop_requested,
    update_status,
    write_json,
)
from speedy_scraper.config import config_from_mapping
from speedy_scraper.exports import write_result
from speedy_scraper.models import (
    RawCandidate,
    RejectedCandidate,
    ScrapeConfig,
    ScrapeResult,
    SearchResult,
    VerifiedLead,
)
from speedy_scraper.parser import candidates_from_results, merge_candidates
from speedy_scraper.pipeline import collect_company_evidence, load_existing_urls, rank_candidates
from speedy_scraper.query import build_queries
from speedy_scraper.sources import (
    DdgsSource,
    SourceError,
    build_sources,
    close_sources,
    configure_google_challenge_wait,
    independent_source_families,
    search_source_page,
)
from speedy_scraper.text import any_term_in_text, clean_spaces
from speedy_scraper.validator import validate_candidate

CHECKPOINT_VERSION = 3


def run_lead_job(job_dir: Path | str, *, source_builder=None) -> ScrapeResult:
    path = Path(job_dir)
    raw_config = read_json(path / "config.json", default={})
    if not isinstance(raw_config, dict):
        raise ValueError("Invalid lead-harvest job config")
    config = config_from_mapping(raw_config)
    if config.require_target_company and not config.company_names:
        raise ValueError("Hard company filtering requires at least one target company")
    if config.minimum_sources > len(independent_source_families(config.sources)):
        raise ValueError("Minimum evidence sources exceed the selected source count")
    queries = build_queries(config)
    if not queries:
        raise ValueError("The selected filters did not produce any search queries")

    checkpoint_path = path / "checkpoint.json"
    checkpoint = read_json(checkpoint_path, default={})
    if not isinstance(checkpoint, dict):
        checkpoint = {}

    phase = str(checkpoint.get("phase") or "search")
    query_index = int(checkpoint.get("query_index") or 0)
    source_index = int(checkpoint.get("source_index") or 0)
    page_index = int(checkpoint.get("page_index") or 0)
    validation_index = int(checkpoint.get("validation_index") or 0)
    candidates_by_url = {
        candidate.linkedin_url: candidate
        for candidate in (_raw_candidate_from_data(item) for item in checkpoint.get("candidates", []))
        if candidate.linkedin_url
    }
    leads = [_verified_lead_from_data(item) for item in checkpoint.get("leads", [])]
    rejections = [_rejection_from_data(item) for item in checkpoint.get("rejections", [])]
    source_errors = [str(item) for item in checkpoint.get("source_errors", [])]
    metrics: Counter[str] = Counter(
        {str(key): int(value) for key, value in dict(checkpoint.get("metrics") or {}).items()}
    )
    company_evidence_cache = {
        str(key): str(value)
        for key, value in dict(checkpoint.get("company_evidence_cache") or {}).items()
    }
    existing_urls = load_existing_urls(config.existing_files)
    existing_urls.update(lead.linkedin_url for lead in leads)
    pool_target = max(
        config.target_count * config.candidate_pool_multiplier,
        config.target_count + 25,
    )

    try:
        if phase == "search":
            query_index, source_index, page_index = _run_search_phase(
                path,
                checkpoint_path,
                config,
                queries,
                query_index,
                source_index,
                page_index,
                candidates_by_url,
                leads,
                rejections,
                source_errors,
                metrics,
                company_evidence_cache,
                existing_urls,
                pool_target,
                source_builder=source_builder,
            )
            if stop_requested(path):
                return _pause_job(
                    path,
                    checkpoint_path,
                    config,
                    queries,
                    "search",
                    query_index,
                    source_index,
                    validation_index,
                    candidates_by_url,
                    leads,
                    rejections,
                    source_errors,
                    metrics,
                    company_evidence_cache,
                )
            phase = "consolidate"
            validation_index = 0
            _save_checkpoint(
                checkpoint_path,
                phase,
                query_index,
                source_index,
                validation_index,
                candidates_by_url,
                leads,
                rejections,
                source_errors,
                metrics,
                company_evidence_cache,
            )
            _lead_status(
                path,
                "running",
                "consolidate",
                "Deduplicating identities and ranking candidate evidence",
                len(candidates_by_url),
                len(candidates_by_url),
                candidates_by_url,
                leads,
                rejections,
            )
            rank_candidates(candidates_by_url.values(), config)
            phase = "verify"

        result, validation_index = _run_verification_phase(
            path,
            checkpoint_path,
            config,
            queries,
            query_index,
            source_index,
            validation_index,
            candidates_by_url,
            leads,
            rejections,
            source_errors,
            metrics,
            company_evidence_cache,
            existing_urls,
        )
        if stop_requested(path):
            return _pause_job(
                path,
                checkpoint_path,
                config,
                queries,
                "verify",
                query_index,
                source_index,
                validation_index,
                candidates_by_url,
                result.leads,
                result.rejections,
                result.source_errors,
                Counter(result.metrics),
                company_evidence_cache,
            )

        metrics["candidates_found"] = len(candidates_by_url)
        metrics["rejected"] = len(rejections)
        result = ScrapeResult(
            leads=leads,
            rejections=rejections,
            metrics=dict(metrics),
            queries=queries,
            source_errors=source_errors,
        )
        csv_path, xlsx_path = _write_exports(result, path, config, partial=False)
        _save_checkpoint(
            checkpoint_path,
            "completed",
            query_index,
            source_index,
            validation_index,
            candidates_by_url,
            leads,
            rejections,
            source_errors,
            metrics,
            company_evidence_cache,
        )
        update_status(
            path,
            state="completed",
            phase="export",
            message=f"Completed with {len(leads)} verified leads",
            processed=validation_index,
            total=len(candidates_by_url),
            candidates=len(candidates_by_url),
            matched=len(leads),
            rejected=len(rejections),
            csv_path=str(csv_path),
            xlsx_path=str(xlsx_path),
        )
        return result
    except Exception as exc:
        metrics["candidates_found"] = len(candidates_by_url)
        metrics["rejected"] = len(rejections)
        _save_checkpoint(
            checkpoint_path,
            phase,
            query_index,
            source_index,
            validation_index,
            candidates_by_url,
            leads,
            rejections,
            source_errors,
            metrics,
            company_evidence_cache,
        )
        update_status(
            path,
            state="failed",
            phase=phase,
            message=str(exc),
            candidates=len(candidates_by_url),
            matched=len(leads),
            rejected=len(rejections),
        )
        raise


def load_lead_job_checkpoint(job_dir: Path | str) -> tuple[ScrapeResult, dict[str, Any]]:
    path = Path(job_dir)
    checkpoint = read_json(path / "checkpoint.json", default={})
    config_data = read_json(path / "config.json", default={})
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    if not isinstance(config_data, dict):
        config_data = {}
    config = config_from_mapping(config_data)
    result = ScrapeResult(
        leads=[_verified_lead_from_data(item) for item in checkpoint.get("leads", [])],
        rejections=[_rejection_from_data(item) for item in checkpoint.get("rejections", [])],
        metrics={
            str(key): int(value)
            for key, value in dict(checkpoint.get("metrics") or {}).items()
        },
        queries=build_queries(config),
        source_errors=[str(item) for item in checkpoint.get("source_errors", [])],
    )
    return result, checkpoint


def _run_search_phase(
    path: Path,
    checkpoint_path: Path,
    config: ScrapeConfig,
    queries: list[str],
    query_index: int,
    source_index: int,
    page_index: int,
    candidates_by_url: dict[str, RawCandidate],
    leads: list[VerifiedLead],
    rejections: list[RejectedCandidate],
    source_errors: list[str],
    metrics: Counter[str],
    company_evidence_cache: dict[str, str],
    existing_urls: set[str],
    pool_target: int,
    *,
    source_builder=None,
) -> tuple[int, int, int]:
    sources = configure_google_challenge_wait(
        (source_builder or build_sources)(config.sources),
        config.google_manual_challenge_seconds,
    )
    if not sources:
        raise ValueError("Select at least one search source")
    page_budget = min(
        config.max_pages_per_query,
        max(1, (config.max_results_per_query + 9) // 10),
    )
    searches_total = len(queries) * len(sources) * page_budget
    checkpoint = read_json(checkpoint_path, default={})
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    disabled_sources = {str(item) for item in checkpoint.get("disabled_sources") or []}
    exhausted_sources = {str(item) for item in checkpoint.get("exhausted_sources") or []}
    source_failures: Counter[str] = Counter(
        {
            str(key): int(value)
            for key, value in dict(checkpoint.get("source_failures") or {}).items()
        }
    )
    completed = (query_index * page_budget + page_index) * len(sources) + source_index
    _lead_status(
        path,
        "running",
        "search",
        "Discovering public LinkedIn candidates",
        completed,
        searches_total,
        candidates_by_url,
        leads,
        rejections,
        searches_completed=completed,
        searches_total=searches_total,
    )
    try:
        while query_index < len(queries) and len(candidates_by_url) < pool_target:
            if stop_requested(path):
                break
            query = queries[query_index]
            source = sources[source_index]
            results: list[SearchResult] = []
            if source.name not in disabled_sources and source.name not in exhausted_sources:
                with JobHeartbeat(
                    path,
                    activity="Public candidate search in progress",
                    current_query=query,
                    current_source=source.name,
                    current_page=page_index + 1,
                ):
                    try:
                        source_limit = (
                            config.max_results_per_query if source.name == "ddgs" else 10
                        )
                        batch = search_source_page(
                            source,
                            query,
                            page=page_index + 1,
                            max_results=source_limit,
                            headless=config.browser_headless,
                        )
                        results = batch.results
                        source_failures[source.name] = 0
                        if not batch.has_next:
                            exhausted_sources.add(source.name)
                    except SourceError as exc:
                        message = f"{source.name}: {exc}"
                        if message not in source_errors:
                            source_errors.append(message)
                        metrics[f"{source.name}_errors"] += 1
                        source_failures[source.name] += 1
                        if exc.disable_source or source_failures[source.name] >= config.source_failure_limit:
                            disabled_sources.add(source.name)
                            exhausted_sources.add(source.name)
                metrics["source_searches"] += 1
            metrics[f"{source.name}_results"] += len(results)
            for candidate in candidates_from_results(results):
                if candidate.linkedin_url in existing_urls:
                    metrics["duplicates"] += 1
                    continue
                current = candidates_by_url.get(candidate.linkedin_url)
                if current:
                    merge_candidates(current, candidate)
                else:
                    candidates_by_url[candidate.linkedin_url] = candidate

            source_index += 1
            if source_index >= len(sources):
                source_index = 0
                if page_index + 1 < page_budget and len(exhausted_sources) < len(sources):
                    page_index += 1
                else:
                    query_index += 1
                    page_index = 0
                    exhausted_sources.clear()
            metrics["candidates_found"] = len(candidates_by_url)
            searches_completed = (
                (query_index * page_budget + page_index) * len(sources) + source_index
            )
            _save_checkpoint(
                checkpoint_path,
                "search",
                query_index,
                source_index,
                0,
                candidates_by_url,
                leads,
                rejections,
                source_errors,
                metrics,
                company_evidence_cache,
                page_index=page_index,
                disabled_sources=disabled_sources,
                exhausted_sources=exhausted_sources,
                source_failures=dict(source_failures),
            )
            _lead_status(
                path,
                "running",
                "search",
                f"Collected {len(candidates_by_url)} unique candidates",
                searches_completed,
                searches_total,
                candidates_by_url,
                leads,
                rejections,
                searches_completed=searches_completed,
                searches_total=searches_total,
                current_query=query,
                current_source=source.name,
                current_page=page_index + 1,
            )
    finally:
        close_sources(sources)
    return query_index, source_index, page_index


def _run_verification_phase(
    path: Path,
    checkpoint_path: Path,
    config: ScrapeConfig,
    queries: list[str],
    query_index: int,
    source_index: int,
    validation_index: int,
    candidates_by_url: dict[str, RawCandidate],
    leads: list[VerifiedLead],
    rejections: list[RejectedCandidate],
    source_errors: list[str],
    metrics: Counter[str],
    company_evidence_cache: dict[str, str],
    existing_urls: set[str],
) -> tuple[ScrapeResult, int]:
    ranked = rank_candidates(candidates_by_url.values(), config)
    validation_index = min(validation_index, len(ranked))
    evidence_source = DdgsSource(personal_profiles_only=False)
    _lead_status(
        path,
        "running",
        "verify",
        "Applying the complete filter contract",
        validation_index,
        len(ranked),
        candidates_by_url,
        leads,
        rejections,
        validation_completed=validation_index,
        validation_total=len(ranked),
    )
    while validation_index < len(ranked) and len(leads) < config.target_count:
        if stop_requested(path):
            break
        candidate = ranked[validation_index]
        company_key = clean_spaces(candidate.company).lower()
        company_evidence = company_evidence_cache.get(company_key, "")
        needs_company_evidence = (
            bool(config.industries)
            and not any_term_in_text(candidate.evidence, config.industries)
            and company_key not in {"", "unknown"}
        )
        if needs_company_evidence and company_key not in company_evidence_cache:
            with JobHeartbeat(
                path,
                activity="Validating company and industry evidence",
                current_name=candidate.name,
                current_company=candidate.company,
            ):
                company_evidence = collect_company_evidence(
                    evidence_source,
                    candidate.company,
                    config.industries,
                )
            company_evidence_cache[company_key] = company_evidence
            if company_evidence:
                metrics["company_evidence_queries"] += 1

        lead, rejection = validate_candidate(
            candidate,
            roles=config.roles,
            locations=config.locations,
            industries=config.industries,
            company_names=config.company_names,
            existing_urls=existing_urls,
            company_evidence=company_evidence,
            business_model=config.business_model,
            require_target_company=config.require_target_company,
            minimum_confidence=config.minimum_confidence,
            minimum_sources=config.minimum_sources,
        )
        if lead:
            leads.append(lead)
            existing_urls.add(lead.linkedin_url)
            metrics["verified"] += 1
        elif rejection:
            rejections.append(rejection)
            metrics[f"rejected_{rejection.reason}"] += 1

        validation_index += 1
        metrics["candidates_found"] = len(candidates_by_url)
        metrics["rejected"] = len(rejections)
        _save_checkpoint(
            checkpoint_path,
            "verify",
            query_index,
            source_index,
            validation_index,
            candidates_by_url,
            leads,
            rejections,
            source_errors,
            metrics,
            company_evidence_cache,
        )
        _lead_status(
            path,
            "running",
            "verify",
            f"Verified {len(leads)} leads; reviewed {validation_index}/{len(ranked)} candidates",
            validation_index,
            len(ranked),
            candidates_by_url,
            leads,
            rejections,
            validation_completed=validation_index,
            validation_total=len(ranked),
            current_name=candidate.name,
            current_company=candidate.company,
        )

    return (
        ScrapeResult(
            leads=leads,
            rejections=rejections,
            metrics=dict(metrics),
            queries=queries,
            source_errors=source_errors,
        ),
        validation_index,
    )


def _pause_job(
    path: Path,
    checkpoint_path: Path,
    config: ScrapeConfig,
    queries: list[str],
    phase: str,
    query_index: int,
    source_index: int,
    validation_index: int,
    candidates_by_url: dict[str, RawCandidate],
    leads: list[VerifiedLead],
    rejections: list[RejectedCandidate],
    source_errors: list[str],
    metrics: Counter[str],
    company_evidence_cache: dict[str, str],
) -> ScrapeResult:
    metrics["candidates_found"] = len(candidates_by_url)
    metrics["rejected"] = len(rejections)
    result = ScrapeResult(
        leads=leads,
        rejections=rejections,
        metrics=dict(metrics),
        queries=queries,
        source_errors=source_errors,
    )
    _save_checkpoint(
        checkpoint_path,
        phase,
        query_index,
        source_index,
        validation_index,
        candidates_by_url,
        leads,
        rejections,
        source_errors,
        metrics,
        company_evidence_cache,
    )
    csv_path, xlsx_path = _write_exports(result, path, config, partial=True)
    update_status(
        path,
        state="paused",
        phase=phase,
        message="Paused safely after the current unit; relaunch to resume",
        candidates=len(candidates_by_url),
        matched=len(leads),
        rejected=len(rejections),
        csv_path=str(csv_path),
        xlsx_path=str(xlsx_path),
    )
    return result


def _write_exports(
    result: ScrapeResult,
    path: Path,
    config: ScrapeConfig,
    *,
    partial: bool,
) -> tuple[Path, Path]:
    suffix = "_partial" if partial else ""
    base = path / f"Verified_Leads{suffix}"
    csv_path = write_result(result, base.with_suffix(".csv"))
    xlsx_path = write_result(result, base.with_suffix(".xlsx"), config=config)
    return csv_path, xlsx_path


def _lead_status(
    path: Path,
    state: str,
    phase: str,
    message: str,
    processed: int,
    total: int,
    candidates_by_url: dict[str, RawCandidate],
    leads: list[VerifiedLead],
    rejections: list[RejectedCandidate],
    **extra: object,
) -> None:
    update_status(
        path,
        state=state,
        workflow="lead_harvest",
        job_id=path.name,
        phase=phase,
        message=message,
        processed=processed,
        total=total,
        candidates=len(candidates_by_url),
        matched=len(leads),
        rejected=len(rejections),
        **extra,
    )


def _save_checkpoint(
    path: Path,
    phase: str,
    query_index: int,
    source_index: int,
    validation_index: int,
    candidates_by_url: dict[str, RawCandidate],
    leads: list[VerifiedLead],
    rejections: list[RejectedCandidate],
    source_errors: list[str],
    metrics: Counter[str],
    company_evidence_cache: dict[str, str],
    *,
    page_index: int | None = None,
    disabled_sources: set[str] | None = None,
    exhausted_sources: set[str] | None = None,
    source_failures: dict[str, int] | None = None,
) -> None:
    previous = read_json(path, default={})
    previous = previous if isinstance(previous, dict) else {}
    write_json(
        path,
        {
            "version": CHECKPOINT_VERSION,
            "phase": phase,
            "query_index": query_index,
            "source_index": source_index,
            "page_index": int(
                previous.get("page_index") or 0 if page_index is None else page_index
            ),
            "validation_index": validation_index,
            "candidates": [_raw_candidate_to_data(item) for item in candidates_by_url.values()],
            "leads": [asdict(item) for item in leads],
            "rejections": [asdict(item) for item in rejections],
            "source_errors": source_errors,
            "metrics": dict(metrics),
            "company_evidence_cache": company_evidence_cache,
            "disabled_sources": sorted(
                disabled_sources
                if disabled_sources is not None
                else {str(item) for item in previous.get("disabled_sources") or []}
            ),
            "exhausted_sources": sorted(
                exhausted_sources
                if exhausted_sources is not None
                else {str(item) for item in previous.get("exhausted_sources") or []}
            ),
            "source_failures": (
                source_failures
                if source_failures is not None
                else dict(previous.get("source_failures") or {})
            ),
        },
    )


def _raw_candidate_to_data(candidate: RawCandidate) -> dict[str, object]:
    value = asdict(candidate)
    value["sources_seen"] = sorted(candidate.sources_seen)
    value["queries_seen"] = sorted(candidate.queries_seen)
    return value


def _raw_candidate_from_data(value: dict[str, Any]) -> RawCandidate:
    return RawCandidate(
        name=str(value.get("name") or ""),
        designation=str(value.get("designation") or ""),
        company=str(value.get("company") or ""),
        linkedin_url=str(value.get("linkedin_url") or ""),
        title=str(value.get("title") or ""),
        body=str(value.get("body") or ""),
        source=str(value.get("source") or ""),
        query=str(value.get("query") or ""),
        evidence=str(value.get("evidence") or ""),
        sources_seen={str(item) for item in value.get("sources_seen") or []},
        queries_seen={str(item) for item in value.get("queries_seen") or []},
    )


def _verified_lead_from_data(value: dict[str, Any]) -> VerifiedLead:
    return VerifiedLead(
        name=str(value.get("name") or ""),
        designation=str(value.get("designation") or ""),
        company=str(value.get("company") or ""),
        location=str(value.get("location") or ""),
        linkedin_id=str(value.get("linkedin_id") or ""),
        linkedin_url=str(value.get("linkedin_url") or ""),
        source=str(value.get("source") or ""),
        confidence=int(value.get("confidence") or 0),
        evidence=str(value.get("evidence") or ""),
    )


def _rejection_from_data(value: dict[str, Any]) -> RejectedCandidate:
    return RejectedCandidate(
        name=str(value.get("name") or ""),
        designation=str(value.get("designation") or ""),
        company=str(value.get("company") or ""),
        linkedin_url=str(value.get("linkedin_url") or ""),
        reason=str(value.get("reason") or ""),
        source=str(value.get("source") or ""),
        evidence=str(value.get("evidence") or ""),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--job-dir", type=Path, required=True)
    args = parser.parse_args()
    run_lead_job(args.job_dir)


if __name__ == "__main__":
    main()
