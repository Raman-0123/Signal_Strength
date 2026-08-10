"""Persistent, UI-independent lead discovery and validation engine."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any

from core.google_search import GoogleSecurityCheck
from core.query_builder import build_company_validation_query, build_person_validation_query
from core.utils import (
    any_term_matches,
    combined_text,
    company_name_variants,
    extract_profile_location,
    is_export_ready_profile,
    normalize_linkedin_url,
    normalize_text,
    parse_profile,
    person_identity_key,
)
from speedy_scraper.browser import BrowserManager
from speedy_scraper.domain import (
    JobStatus,
    LeadRecord,
    ResultKind,
    ScrapeCheckpoint,
    ScrapeProgress,
    ScrapeResult,
    SearchRequest,
)
from speedy_scraper.evaluation import CandidateEvaluator, LeadBuilder
from speedy_scraper.events import RepositoryEventSink
from speedy_scraper.providers import GoogleSearchProvider, ProviderRegistry
from speedy_scraper.reliability import (
    ProviderBlockedError,
    ProxyPool,
    RetryExecutor,
    TokenBucketRateLimiter,
)

DEFAULT_PROVIDER_BLOCK_COOLDOWN_SECONDS = 300.0


def _merge_partial_profile(
    partials: dict[str, dict[str, str]],
    name: str,
    designation: str,
    company: str,
    url: str,
    title: str,
    body: str,
) -> dict[str, str]:
    cached = partials.get(url, {})
    designation_candidates = [value for value in (designation, cached.get("designation", "")) if value]
    merged = {
        "name": name if normalize_text(name) not in {"", "unknown", "none", "nan"} else cached.get("name", name),
        "designation": max(
            designation_candidates,
            key=lambda value: len(normalize_text(value).split()),
            default="",
        ),
        "company": company if normalize_text(company) not in {"", "unknown", "none", "nan"} else cached.get("company", company),
        "title": combined_text(title, cached.get("title", ""))[:420],
        "body": combined_text(body, cached.get("body", ""))[:840],
    }
    partials[url] = merged
    return merged


def _citation_evidence(
    results: tuple[Any, ...],
    required_groups: list[list[str]] | None = None,
) -> str:
    parts: list[str] = []
    for result in results:
        text = combined_text(result.title, result.body)
        if required_groups and not all(
            any_term_matches(text, group) for group in required_groups if group
        ):
            continue
        parts.append(text)
    return combined_text(*parts)[:12000]


class ScraperEngine:
    """Run one durable lead job without reading or writing Streamlit state."""

    def __init__(self, config, repository, logger=None):
        self.config = config
        self.repository = repository
        self.logger = logger
        self.evaluator = CandidateEvaluator(config.scoring)
        self.builder = LeadBuilder()
        self.browser_manager = BrowserManager(config.browser)
        self.limiter = TokenBucketRateLimiter(config.rate_limits)
        self.retries = RetryExecutor(config.retry)
        self.proxies = ProxyPool(config.proxy)
        self._provider_registries: dict[str, ProviderRegistry] = {}

    def _sink(self, job_id: str) -> RepositoryEventSink:
        return RepositoryEventSink(self.repository, job_id, self.logger)

    @staticmethod
    def _default_query_state(item: dict[str, Any]) -> dict[str, Any]:
        return {
            "active_query": item.get("query", ""),
            "kind": "primary",
            "page": 1,
            "pending_results": [],
            "result_index": 0,
            "pending_next_page": None,
            "seen_page_fingerprints": [],
            "seen_result_urls": [],
            "fallback_started": False,
            "complete": False,
        }

    def _checkpoint(self, job) -> ScrapeCheckpoint:
        checkpoint = ScrapeCheckpoint.from_dict(job.checkpoint)
        plan = job.request.get("query_plan", [])
        if checkpoint.query_index < len(plan) and not checkpoint.query_state:
            checkpoint.query_state = self._default_query_state(plan[checkpoint.query_index])
        self.browser_manager.prepare(
            job.id,
            checkpoint.browser_state,
            scheduled=bool(job.request.get("scheduled", False)),
        )
        return checkpoint

    def _save(self, job_id: str, checkpoint: ScrapeCheckpoint) -> None:
        self.repository.save_checkpoint(job_id, checkpoint.as_dict())

    def _provider(self, job, checkpoint, sink, provider_name: str):
        registry = self._provider_registries.get(job.id)
        if registry is None:
            registry = ProviderRegistry(
                self.config,
                event_sink=sink,
                browser_state=checkpoint.browser_state,
                limiter=self.limiter,
                retries=self.retries,
                proxies=self.proxies,
            )
            self._provider_registries[job.id] = registry
        google = registry.providers.get("google")
        if isinstance(google, GoogleSearchProvider):
            # Repository reloads construct a fresh checkpoint object. Keep the
            # long-lived provider attached to the current persisted state.
            google.browser_state = checkpoint.browser_state
        return registry.get(provider_name)

    def _pause_for_provider_block(
        self,
        job,
        checkpoint: ScrapeCheckpoint,
        sink: RepositoryEventSink,
        exc: ProviderBlockedError,
        *,
        phase: str,
        query: str,
        page: int,
        linkedin_only: bool,
        max_results: int,
    ):
        retry_value = getattr(exc, "retry_after", None)
        retry_after = max(
            0.0,
            float(
                DEFAULT_PROVIDER_BLOCK_COOLDOWN_SECONDS
                if retry_value is None
                else retry_value
            ),
        )
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=retry_after)
        payload = {
            "engine": "DuckDuckGo",
            "provider": getattr(exc, "provider", "") or "ddgs",
            "phase": phase,
            "query": query,
            "page": page,
            "linkedin_only": linkedin_only,
            "max_results": max_results,
            "reason": str(exc),
            "retry_after_seconds": retry_after,
            "retry_at": retry_at.isoformat(),
            "manual_action": (
                "Wait for the cooldown, or change network/proxy, then resume "
                "or run verification check to retry the same saved request."
            ),
        }
        checkpoint.security_check = payload
        self._save(job.id, checkpoint)
        self.repository.increment_metric(job.id, "provider_block_events")
        self.repository.increment_metric(job.id, "ddgs_block_events")
        waiting = self.repository.transition(
            job.id,
            JobStatus.WAITING_VERIFICATION,
            outcome="DuckDuckGo cooldown required",
        )
        sink.emit(
            "warning",
            "provider_blocked",
            "DuckDuckGo temporarily blocked or refused requests; job paused for retry.",
            payload,
        )
        return waiting

    def _collect_evidence(
        self,
        job,
        checkpoint: ScrapeCheckpoint,
        sink: RepositoryEventSink,
        *,
        cache_kind: str,
        cache_key: str,
        query: str,
        required_groups: list[list[str]],
    ) -> str:
        cached = self.repository.get_evidence(job.id, cache_kind, cache_key)
        if cached:
            self.repository.increment_metric(job.id, "evidence_cache_hits")
            return str(cached["evidence_text"])
        provider_name = job.request.get("validation_provider") or job.request.get("discovery_provider", "ddgs")
        provider = self._provider(job, checkpoint, sink, provider_name)
        sink.emit(
            "info", "validation_query", f"Running {cache_kind.replace('_', ' ')}.",
            {"provider": provider_name, "query_hash": hashlib.sha256(query.encode()).hexdigest()[:12]},
        )
        request = SearchRequest(
            query=query, page=1, max_results=10, linkedin_only=False, job_id=job.id,
        )
        try:
            page = provider.search(request)
        except ProviderBlockedError as exc:
            setattr(exc, "query", request.query)
            setattr(exc, "page", request.page)
            setattr(exc, "linkedin_only", request.linkedin_only)
            setattr(exc, "max_results", request.max_results)
            raise
        evidence = _citation_evidence(page.results, required_groups)
        self.repository.save_evidence(
            job.id, cache_kind, cache_key, provider_name, query,
            evidence, len(page.results),
        )
        self.repository.increment_metric(job.id, "validation_queries")
        self.repository.increment_metric(job.id, "provider_attempts", page.attempts)
        return evidence

    def _validation_evidence(
        self,
        job,
        checkpoint: ScrapeCheckpoint,
        sink: RepositoryEventSink,
        candidate: dict[str, Any],
    ) -> tuple[str, str]:
        request = job.request
        company_evidence = ""
        person_evidence = ""
        company = candidate["company"]
        name = candidate["name"]
        company_key = normalize_text(company)
        company_filters = bool(
            request.get("industries")
            or request.get("custom_keywords")
            or request.get("business_model", "Any") != "Any"
            or request.get("gcc_only")
        )
        if company_filters and company_key:
            query = build_company_validation_query(
                company,
                all_inds=request.get("industries", []),
                custom_kws=", ".join(request.get("custom_keywords", [])),
                business_model=request.get("business_model", "Any"),
                gcc_only=bool(request.get("gcc_only")),
            )
            company_evidence = self._collect_evidence(
                job, checkpoint, sink,
                cache_kind="company_validation", cache_key=company_key,
                query=query, required_groups=[company_name_variants(company)],
            )
        if request.get("signals"):
            person_key = f"{normalize_text(name)}|{company_key}"
            query = build_person_validation_query(
                name, company, all_sigs=request.get("signals", []),
            )
            person_evidence = self._collect_evidence(
                job, checkpoint, sink,
                cache_kind="person_validation", cache_key=person_key,
                query=query,
                required_groups=[[name], company_name_variants(company)],
            )
        return company_evidence, person_evidence

    def _evaluate(
        self,
        job,
        candidate: dict[str, Any],
        *,
        company_evidence: str = "",
        person_evidence: str = "",
        strict_stage: bool,
    ):
        request = job.request
        return self.evaluator.evaluate(
            title=candidate["title"], body=candidate["body"],
            href=candidate["url"], name=candidate["name"],
            designation=candidate["designation"], company=candidate["company"],
            locations=request.get("locations", []), roles=request.get("roles", []),
            industries=request.get("industries", []), signals=request.get("signals", []),
            custom_terms=request.get("custom_keywords", []),
            organization_terms=request.get("organizations", []),
            business_model=request.get("business_model", "Any"),
            gcc_only=bool(request.get("gcc_only")),
            company_evidence=company_evidence,
            person_evidence=person_evidence,
            strict_stage=strict_stage,
        )

    def _build_lead(self, job, candidate, evaluation, source: str) -> LeadRecord:
        request = job.request
        location = extract_profile_location(
            candidate["body"], candidate["title"], request.get("locations", []),
            require_current_evidence=True, person_name=candidate["name"],
        )
        return self.builder.build(
            name=candidate["name"], designation=candidate["designation"],
            company=candidate["company"], linkedin_url=candidate["url"],
            location=location, query_bucket=candidate.get("query_bucket", "discovery"),
            source=source, evaluation=evaluation,
            roles=request.get("roles", []), locations=request.get("locations", []),
            industries=request.get("industries", []), signals=request.get("signals", []),
            custom_terms=request.get("custom_keywords", []),
            organization_terms=request.get("organizations", []),
        )

    def _finish_validation(self, job, checkpoint, sink) -> bool:
        pending = checkpoint.pending_validation
        if not pending:
            return False
        candidate = pending["candidate"]
        try:
            company_evidence, person_evidence = self._validation_evidence(
                job, checkpoint, sink, candidate,
            )
        except GoogleSecurityCheck as check:
            check.phase = "strict validation"
            checkpoint.security_check = check.as_dict()
            checkpoint.browser_state = checkpoint.browser_state
            self._save(job.id, checkpoint)
            self.repository.increment_metric(job.id, "captcha_events")
            self.repository.transition(
                job.id, JobStatus.WAITING_VERIFICATION,
                outcome="Waiting for manual verification",
            )
            sink.emit("warning", "captcha_required", "Google verification is required for strict validation.", check.as_dict())
            return True
        except ProviderBlockedError as exc:
            self._pause_for_provider_block(
                job,
                checkpoint,
                sink,
                exc,
                phase="strict validation",
                query=str(getattr(exc, "query", "")),
                page=int(getattr(exc, "page", 1)),
                linkedin_only=bool(getattr(exc, "linkedin_only", False)),
                max_results=int(getattr(exc, "max_results", 10)),
            )
            return True
        evaluation = self._evaluate(
            job, candidate, company_evidence=company_evidence,
            person_evidence=person_evidence, strict_stage=True,
        )
        lead = self._build_lead(job, candidate, evaluation, pending["source"])
        self.repository.update_strict_result(
            job.id, pending["lead_id"], evaluation, payload=lead.as_dict(),
        )
        checkpoint.pending_validation = None
        checkpoint.security_check = None
        checkpoint.query_state["result_index"] = int(pending["result_index"]) + 1
        checkpoint.partial_profiles.pop(candidate["url"], None)
        self._save(job.id, checkpoint)
        if evaluation.strict_qualified:
            self.repository.increment_metric(job.id, "strict_matches")
            sink.emit("info", "strict_match", f"Strict match confirmed: {candidate['name']}.")
        else:
            for key in ("industry", "signal", "custom", "business_model", "gcc"):
                if not evaluation.hits.get(key, True):
                    self.repository.increment_metric(job.id, f"rejected_{key}")
        return False

    def _process_candidate(self, job, checkpoint, sink, raw: dict[str, str], result_index: int) -> bool:
        href = raw.get("href", "")
        clean_url = normalize_linkedin_url(href)
        if not clean_url:
            self.repository.increment_metric(job.id, "rejected_non_linkedin")
            return False
        name, designation, company, parsed_url = parse_profile(
            raw.get("title", ""), href, raw.get("body", "")
        )
        clean_url = parsed_url or clean_url
        merged = _merge_partial_profile(
            checkpoint.partial_profiles, name, designation, company, clean_url,
            raw.get("title", ""), raw.get("body", ""),
        )
        candidate = {
            "name": merged["name"], "designation": merged["designation"],
            "company": merged["company"], "url": clean_url,
            "title": merged["title"], "body": merged["body"],
            "query_bucket": job.request["query_plan"][checkpoint.query_index].get("bucket", "discovery"),
        }
        identity = person_identity_key(candidate["name"], candidate["company"])
        import_ids = job.request.get("dedup_import_ids", [])
        if (
            self.repository.contains_dedup_key("url", clean_url, import_ids=import_ids)
            or (identity and self.repository.contains_dedup_key("identity", identity, import_ids=import_ids))
        ):
            self.repository.increment_metric(job.id, "duplicates")
            return False
        if not is_export_ready_profile(
            candidate["name"], candidate["company"], clean_url,
            designation=candidate["designation"],
        ):
            self.repository.increment_metric(job.id, "rejected_incomplete")
            return False
        hard_evaluation = self._evaluate(job, candidate, strict_stage=False)
        if not hard_evaluation.hard_qualified:
            for key in ("role", "location", "organization"):
                if not hard_evaluation.hits.get(key, True):
                    self.repository.increment_metric(job.id, f"rejected_{key}")
            return False
        source = job.request.get("discovery_provider", "ddgs")
        lead = self._build_lead(job, candidate, hard_evaluation, source)
        lead.strict_qualified = False
        pending_validation = {
            "candidate": candidate,
            "source": source,
            "result_index": result_index,
        }
        lead_id = self.repository.save_qualified(
            job.id,
            lead,
            checkpoint=checkpoint.as_dict(),
            pending_validation=pending_validation,
        )
        self.repository.increment_metric(job.id, "qualified_pocs")
        sink.emit("info", "qualified_poc", f"Qualified POC found: {candidate['name']} at {candidate['company']}.")
        checkpoint.pending_validation = {
            "lead_id": lead_id,
            **pending_validation,
        }
        return self._finish_validation(job, checkpoint, sink)

    def _advance_query(self, job, checkpoint, sink, reason: str) -> None:
        plan = job.request.get("query_plan", [])
        item = plan[checkpoint.query_index]
        state = checkpoint.query_state
        fallback = str(item.get("fallback_query", "")).strip()
        if (
            fallback
            and not state.get("fallback_started")
            and self.repository.get_job(job.id).qualified_count < int(job.request.get("target_count", 15))
        ):
            state.clear()
            state.update(self._default_query_state({"query": fallback}))
            state["kind"] = "fallback"
            state["fallback_started"] = True
            sink.emit("info", "fallback_started", reason, {"query_index": checkpoint.query_index})
            return
        state["complete"] = True
        checkpoint.query_index += 1
        checkpoint.query_state = (
            self._default_query_state(plan[checkpoint.query_index])
            if checkpoint.query_index < len(plan) else {}
        )

    def step(self, job_id: str) -> ScrapeProgress:
        job = self.repository.get_job(job_id)
        sink = self._sink(job_id)
        if job.status in {JobStatus.PAUSE_REQUESTED, JobStatus.CANCEL_REQUESTED}:
            transition_target = (
                JobStatus.PAUSED
                if job.status == JobStatus.PAUSE_REQUESTED
                else JobStatus.CANCELLED
            )
            self.browser_manager.close(job.checkpoint.get("browser_state", {}))
            job = self.repository.transition(
                job_id,
                transition_target,
                outcome=transition_target.value.replace("_", " ").title(),
            )
            sink.emit(
                "info", transition_target.value,
                f"Job {transition_target.value}; persisted results were retained.",
            )
            return ScrapeProgress(job, message=transition_target.value)
        if job.status == JobStatus.WAITING_VERIFICATION or job.status.terminal:
            return ScrapeProgress(job, message=job.status.value)
        if job.status == JobStatus.QUEUED:
            job = self.repository.transition(job_id, JobStatus.RUNNING, outcome="Running")
        checkpoint = self._checkpoint(job)
        plan = job.request.get("query_plan", [])
        target = int(job.request.get("target_count", 15))

        if checkpoint.pending_validation:
            paused = self._finish_validation(job, checkpoint, sink)
            return ScrapeProgress(self.repository.get_job(job_id), 1, "verification required" if paused else "validation complete")

        refreshed = self.repository.get_job(job_id)
        if refreshed.qualified_count >= target:
            self._save(job_id, checkpoint)
            finished = self.repository.transition(job_id, JobStatus.COMPLETED, outcome="Target reached")
            sink.emit("info", "job_completed", f"Target reached with {finished.qualified_count} qualified POCs.")
            return ScrapeProgress(finished, message="target reached")
        if checkpoint.query_index >= len(plan):
            finished = self.repository.transition(job_id, JobStatus.EXHAUSTED, outcome="Search exhausted")
            sink.emit("warning", "job_exhausted", f"Search exhausted at {finished.qualified_count}/{target} qualified POCs.")
            return ScrapeProgress(finished, message="search exhausted")

        state = checkpoint.query_state
        pending = list(state.get("pending_results", []))
        result_index = int(state.get("result_index", 0))
        if result_index < len(pending):
            raw = pending[result_index]
            paused = self._process_candidate(job, checkpoint, sink, raw, result_index)
            if not checkpoint.pending_validation and not paused:
                state["result_index"] = result_index + 1
                self._save(job_id, checkpoint)
            self.repository.increment_metric(job_id, "candidates_processed")
            return ScrapeProgress(self.repository.get_job(job_id), 1, "verification required" if paused else "candidate processed")

        if pending:
            state["pending_results"] = []
            state["result_index"] = 0
            next_page = state.pop("pending_next_page", None)
            provider_name = job.request.get("discovery_provider", "ddgs")
            if next_page and (provider_name == "google" or int(next_page) <= 2):
                state["page"] = int(next_page)
                self._save(job_id, checkpoint)
                return ScrapeProgress(self.repository.get_job(job_id), message="continuing pagination")
            self._advance_query(job, checkpoint, sink, "Primary pages were exhausted; starting alternate-title fallback.")
            self._save(job_id, checkpoint)
            return ScrapeProgress(self.repository.get_job(job_id), message="query advanced")

        provider_name = job.request.get("discovery_provider", "ddgs")
        provider = self._provider(job, checkpoint, sink, provider_name)
        query = state.get("active_query") or plan[checkpoint.query_index].get("query", "")
        page_number = int(state.get("page", 1))
        sink.emit(
            "info", "discovery_query", f"Running discovery query {checkpoint.query_index + 1}/{len(plan)}, page {page_number}.",
            {"provider": provider_name, "query_index": checkpoint.query_index, "page": page_number, "query_hash": hashlib.sha256(query.encode()).hexdigest()[:12]},
        )
        try:
            request = SearchRequest(
                query=query,
                page=page_number,
                max_results=10 if provider_name == "google" else 50,
                linkedin_only=True,
                job_id=job_id,
            )
            page = provider.search(request)
        except GoogleSecurityCheck as check:
            check.phase = "discovery"
            checkpoint.security_check = check.as_dict()
            self._save(job_id, checkpoint)
            self.repository.increment_metric(job_id, "captcha_events")
            waiting = self.repository.transition(
                job_id, JobStatus.WAITING_VERIFICATION,
                outcome="Waiting for manual verification",
            )
            sink.emit("warning", "captcha_required", "Google verification is required for discovery.", check.as_dict())
            return ScrapeProgress(waiting, message="verification required")
        except ProviderBlockedError as exc:
            waiting = self._pause_for_provider_block(
                job,
                checkpoint,
                sink,
                exc,
                phase="discovery",
                query=query,
                page=page_number,
                linkedin_only=True,
                max_results=50,
            )
            return ScrapeProgress(waiting, message="provider blocked")
        self.repository.increment_metric(job_id, "discovery_queries")
        self.repository.increment_metric(job_id, "provider_attempts", page.attempts)
        urls = [normalize_linkedin_url(result.href) for result in page.results]
        urls = [url for url in urls if url]
        fingerprint = hashlib.sha256("|".join(sorted(set(urls))).encode()).hexdigest() if urls else ""
        seen_fingerprints = set(state.get("seen_page_fingerprints", []))
        seen_urls = set(state.get("seen_result_urls", []))
        new_results = [result for result in page.results if normalize_linkedin_url(result.href) not in seen_urls]
        if page.results and ((fingerprint and fingerprint in seen_fingerprints) or not new_results):
            self.repository.increment_metric(job_id, "repeated_pages")
            self._advance_query(job, checkpoint, sink, "Repeated page detected; starting alternate-title fallback.")
            self._save(job_id, checkpoint)
            return ScrapeProgress(self.repository.get_job(job_id), message="repeated page stopped")
        if fingerprint:
            state.setdefault("seen_page_fingerprints", []).append(fingerprint)
        state["seen_result_urls"] = sorted(seen_urls | set(urls))
        state["pending_results"] = [result.as_dict() for result in new_results]
        state["result_index"] = 0
        state["pending_next_page"] = page.next_page
        if not new_results:
            self._advance_query(job, checkpoint, sink, "No LinkedIn profiles found; starting alternate-title fallback.")
        self._save(job_id, checkpoint)
        return ScrapeProgress(self.repository.get_job(job_id), message=f"fetched {len(new_results)} candidates")

    def run(self, job_id: str, cancel_token=None) -> ScrapeResult:
        while True:
            job = self.repository.get_job(job_id)
            if job.status.terminal or job.status in {JobStatus.PAUSED, JobStatus.WAITING_VERIFICATION}:
                break
            if cancel_token is not None and cancel_token.cancelled:
                self.repository.transition(job_id, JobStatus.CANCEL_REQUESTED)
            self.step(job_id)
        job = self.repository.get_job(job_id)
        return ScrapeResult(
            job=job,
            qualified=tuple(self.repository.list_results(job_id, ResultKind.QUALIFIED)),
            strict=tuple(self.repository.list_results(job_id, ResultKind.STRICT)),
            metrics=self.repository.metrics(job_id),
        )
