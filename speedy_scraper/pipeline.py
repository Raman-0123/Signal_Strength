from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pandas as pd

from speedy_scraper.linkedin import normalize_linkedin_url
from speedy_scraper.models import RawCandidate, ScrapeConfig, ScrapeResult
from speedy_scraper.parser import candidates_from_results, merge_candidates
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
from speedy_scraper.taxonomy import canonical_location_from_text
from speedy_scraper.text import any_term_in_text, clean_spaces, or_group
from speedy_scraper.validator import role_match_strength, validate_candidate

ProgressCallback = Callable[[dict[str, object]], None]


class LeadScraper:
    def run(self, config: ScrapeConfig, progress: ProgressCallback | None = None) -> ScrapeResult:
        if config.minimum_sources > len(independent_source_families(config.sources)):
            raise ValueError("Minimum evidence sources exceed the selected source count")
        existing_urls = load_existing_urls(config.existing_files)
        queries = build_queries(config)
        metrics: Counter[str] = Counter()
        source_errors: list[str] = []
        captcha_required = False
        disabled_sources: set[str] = set()
        source_failures: Counter[str] = Counter()
        candidates_by_url: dict[str, RawCandidate] = {}
        sources = configure_google_challenge_wait(
            build_sources(config.sources),
            config.google_manual_challenge_seconds,
        )
        pool_target = max(config.target_count * config.candidate_pool_multiplier, config.target_count + 25)

        self._emit(progress, "started", target=config.target_count, queries=len(queries), sources=len(sources))
        try:
            for query_index, query in enumerate(queries, start=1):
                if len(candidates_by_url) >= pool_target:
                    break
                exhausted_sources: set[str] = set()
                page_budget = min(
                    config.max_pages_per_query,
                    max(1, (config.max_results_per_query + 9) // 10),
                )
                for page in range(1, page_budget + 1):
                    for source in sources:
                        if source.name in disabled_sources or source.name in exhausted_sources:
                            continue
                        self._emit(
                            progress,
                            "searching",
                            query_index=query_index,
                            query_count=len(queries),
                            page=page,
                            source=source.name,
                            candidates=len(candidates_by_url),
                        )
                        try:
                            source_limit = (
                                config.max_results_per_query if source.name == "ddgs" else 10
                            )
                            batch = search_source_page(
                                source,
                                query,
                                page=page,
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
                            if exc.challenge:
                                captcha_required = True
                            if (
                                exc.disable_source
                                or source_failures[source.name] >= config.source_failure_limit
                            ):
                                disabled_sources.add(source.name)
                                exhausted_sources.add(source.name)
                            continue
                        metrics["source_searches"] += 1
                        metrics[f"{source.name}_results"] += len(results)
                        for candidate in candidates_from_results(results):
                            if candidate.linkedin_url in existing_urls:
                                metrics["duplicates"] += 1
                                continue
                            if candidate.linkedin_url in candidates_by_url:
                                merge_candidates(candidates_by_url[candidate.linkedin_url], candidate)
                            else:
                                candidates_by_url[candidate.linkedin_url] = candidate
                        if captcha_required:
                            self._emit(
                                progress,
                                "captcha_required",
                                source=source.name,
                                message="A search source was challenged; Google browser manual recovery is available.",
                            )
                        self._emit(progress, "candidates", candidates=len(candidates_by_url))
                    if len(exhausted_sources) >= len(sources):
                        break
        finally:
            close_sources(sources)

        evidence_source = DdgsSource(personal_profiles_only=False)
        company_evidence_cache: dict[str, str] = {}
        leads = []
        rejections = []
        for candidate in rank_candidates(candidates_by_url.values(), config):
            company_key = clean_spaces(candidate.company).lower()
            company_evidence = ""
            needs_company_evidence = (
                bool(config.industries)
                and not any_term_in_text(candidate.company, config.company_names)
                and not any_term_in_text(candidate.evidence, config.industries)
            )
            if needs_company_evidence and company_key and company_key != "unknown":
                company_evidence = company_evidence_cache.get(company_key, "")
                if company_key not in company_evidence_cache:
                    company_evidence = collect_company_evidence(
                        evidence_source,
                        candidate.company,
                        config.industries,
                        progress=progress,
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
                self._emit(progress, "verified", verified=len(leads), target=config.target_count)
                if len(leads) >= config.target_count:
                    break
            elif rejection:
                rejections.append(rejection)
                metrics[f"rejected_{rejection.reason}"] += 1

        metrics["candidates_found"] = len(candidates_by_url)
        metrics["rejected"] = len(rejections)
        self._emit(progress, "finished", verified=len(leads), rejected=len(rejections))
        return ScrapeResult(
            leads=leads,
            rejections=rejections,
            metrics=dict(metrics),
            queries=queries,
            source_errors=source_errors,
        )

    @staticmethod
    def _emit(progress: ProgressCallback | None, event: str, **payload: object) -> None:
        if progress:
            progress({"event": event, **payload})


def rank_candidates(candidates, config: ScrapeConfig) -> list[RawCandidate]:
    return sorted(
        candidates,
        key=lambda candidate: (
            _score_raw_candidate(candidate, config),
            len(candidate.sources_seen),
            len(candidate.evidence),
        ),
        reverse=True,
    )


def collect_company_evidence(
    source: DdgsSource,
    company: str,
    industries: list[str],
    *,
    progress: ProgressCallback | None = None,
) -> str:
    if not industries:
        return ""
    query = f'"{clean_spaces(company)}" {or_group(industries[:8])}'
    try:
        results = source.search(query, max_results=6)
    except Exception:
        return ""
    text = clean_spaces(" ".join(f"{item.title} {item.body}" for item in results))
    if progress:
        progress({"event": "company_evidence", "company": company, "found": bool(text)})
    return text[:1600]


def load_existing_urls(paths: list[Path]) -> set[str]:
    urls: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        try:
            frames = _load_existing_frames(path)
        except Exception:
            continue
        for frame in frames:
            for row in frame.itertuples(index=False, name=None):
                for value in row:
                    url = normalize_linkedin_url(value if isinstance(value, str) else str(value) if value is not None else None)
                    if url:
                        urls.add(url)
    return urls


def load_existing_people_keys(paths: list[Path]) -> set[str]:
    """Load stable name/company identities from prior exports for cross-run dedupe."""
    keys: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        try:
            frames = _load_existing_frames(path)
        except Exception:
            continue
        for frame in frames:
            columns = {str(column).strip().lower(): column for column in frame.columns}
            name_column = next((column for key, column in columns.items() if key in {"name", "person", "full name", "speaker"}), None)
            company_column = next((column for key, column in columns.items() if key in {"company", "organization", "organisation", "employer"}), None)
            if name_column is None:
                continue
            for _, row in frame.iterrows():
                raw_name = row.get(name_column)
                raw_company = row.get(company_column) if company_column is not None else ""
                name = "" if pd.isna(raw_name) else clean_spaces(str(raw_name or ""))
                company = "" if pd.isna(raw_company) else clean_spaces(str(raw_company or ""))
                if name:
                    keys.add(f"{name.lower()}|{company.lower()}")
    return keys


def _load_existing_frames(path: Path) -> list[pd.DataFrame]:
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        workbook = pd.read_excel(path, sheet_name=None)
        if isinstance(workbook, dict):
            return [frame for frame in workbook.values() if not frame.empty]
        return [workbook] if not workbook.empty else []
    frame = pd.read_csv(path)
    return [frame] if not frame.empty else []


def _score_raw_candidate(candidate: RawCandidate, config: ScrapeConfig) -> int:
    text = f"{candidate.designation} {candidate.company} {candidate.evidence}"
    score = 0
    for role in config.roles:
        if role_match_strength(candidate.designation, role):
            score += 5
    if canonical_location_from_text(text, config.locations):
        score += 4
    for industry in config.industries:
        if any_term_in_text(text, [industry]):
            score += 3
    if candidate.company.lower() != "unknown":
        score += 2
    return score
