"""Application service, workflow dispatcher, and background job runner."""

from __future__ import annotations

import math
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from core.query_builder import (
    QUERY_STRATEGY_VERSION,
    build_query_plan,
    ensure_query_filters,
)
from core.utils import normalize_linkedin_url, split_csv_terms
from speedy_scraper.browser import BrowserManager
from speedy_scraper.config import Catalog, load_catalog
from speedy_scraper.domain import (
    JobRecord,
    JobStatus,
    LeadRecord,
    ScrapeCheckpoint,
    ScrapeResult,
)
from speedy_scraper.engine import ScraperEngine
from speedy_scraper.event_speakers import EventSpeakerEngine, validate_public_source_url
from speedy_scraper.events import RepositoryEventSink
from speedy_scraper.providers import LegacySearchClient, ProviderRegistry, ReliableHttpClient


@dataclass(slots=True)
class CancelToken:
    cancelled: bool = False


class ScraperOrchestrator:
    def __init__(self, config, repository, *, catalog: Catalog | None = None, logger=None):
        self.config = config
        self.repository = repository
        self.catalog = catalog or load_catalog()
        self.logger = logger
        self.browser_manager = BrowserManager(config.browser)
        self.engine = ScraperEngine(config, repository, logger)
        self.event_speaker_engine = EventSpeakerEngine(
            config,
            repository,
            logger,
            limiter=self.engine.limiter,
            retries=self.engine.retries,
            proxies=self.engine.proxies,
        )

    @staticmethod
    def _list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return split_csv_terms(value)
        return [str(item).strip() for item in value if str(item).strip()]

    def normalize_lead_request(self, request: dict[str, Any]) -> dict[str, Any]:
        for field_name in ("search_provider", "discovery_provider", "validation_provider"):
            raw_provider = request.get(field_name)
            if raw_provider is None or str(raw_provider).strip() == "":
                continue
            provider = str(raw_provider).strip().lower()
            if provider != "ddgs":
                raise ValueError(
                    "Lead jobs only support ddgs for discovery and validation."
                )
        target = max(1, min(int(request.get("target_count", 15)), 500))
        locations = self.catalog.resolve("locations", self._list(request.get("location_ids"))) if request.get("location_ids") else self._list(request.get("locations"))
        roles = self.catalog.resolve("roles", self._list(request.get("role_ids"))) if request.get("role_ids") else self._list(request.get("roles"))
        industries = self.catalog.resolve("industries", self._list(request.get("industry_ids"))) if request.get("industry_ids") else self._list(request.get("industries"))
        signals = self.catalog.resolve("signals", self._list(request.get("signal_ids"))) if request.get("signal_ids") else self._list(request.get("signals"))
        if not locations or not roles:
            raise ValueError("Lead jobs require at least one location and role.")
        if request.get("industry_ids") and set(self._list(request["industry_ids"])) == set(self.catalog.industries):
            industries = []
        custom = self._list(request.get("custom_keywords"))
        organizations = self._list(request.get("organizations") or request.get("organization_kws"))
        budget_settings = self.config.query_budget
        budget = math.ceil(
            (
                target
                / budget_settings.acceptance_rate
                / budget_settings.citations_per_query
            )
            * budget_settings.headroom
        )
        budget = max(1, min(budget, budget_settings.maximum_queries))
        generated = build_query_plan(
            locations, roles, industries, signals, ", ".join(custom),
            max_queries=budget,
            organization_kws=", ".join(organizations),
            business_model=str(request.get("business_model", "Any")),
            gcc_only=bool(request.get("gcc_only", False)),
            include_recovery=False,
            include_context_terms=True,
        )
        manual = self._list(request.get("edited_queries") or request.get("queries"))
        if manual:
            plan: list[dict[str, Any]] = []
            originals = {item["query"]: item for item in generated}
            for index, query in enumerate(manual[:budget]):
                if query in originals:
                    plan.append({**originals[query], "fallback_query": ""})
                    continue
                completed = ensure_query_filters(
                    query, locations, roles, industries, signals, ", ".join(custom),
                    organization_kws=", ".join(organizations),
                    business_model=str(request.get("business_model", "Any")),
                    gcc_only=bool(request.get("gcc_only", False)),
                    include_context_terms=True,
                )
                template = generated[index] if index < len(generated) else {}
                plan.append({
                    **template,
                    "query": completed,
                    "fallback_query": "",
                    "bucket": "custom",
                    "roles": template.get("roles", roles),
                    "locations": template.get("locations", locations),
                    "strategy_version": QUERY_STRATEGY_VERSION,
                })
        else:
            plan = generated
        browser_mode = str(request.get("browser_mode", "interactive"))
        if browser_mode not in {"interactive", "scheduled_headless"}:
            raise ValueError(f"Unsupported browser mode: {browser_mode}")
        return {
            "workflow": "lead",
            "catalog_version": self.catalog.version,
            "locations": locations,
            "roles": roles,
            "industries": industries,
            "signals": signals,
            "custom_keywords": custom,
            "organizations": organizations,
            "business_model": str(request.get("business_model", "Any")),
            "gcc_only": bool(request.get("gcc_only", False)),
            "target_count": target,
            "discovery_provider": "ddgs",
            "validation_provider": "ddgs",
            "dedup_import_ids": self._list(request.get("dedup_import_ids")),
            "browser_mode": browser_mode,
            "scheduled": bool(
                request.get("scheduled", False)
                or request.get("browser_mode") == "scheduled_headless"
            ),
            "query_plan": plan,
            "strategy_version": QUERY_STRATEGY_VERSION,
        }

    @staticmethod
    def _is_ddgs_security_check(check: dict[str, Any] | None) -> bool:
        if not check:
            return False
        engine = str(check.get("engine") or check.get("provider") or "").strip().lower()
        return engine in {"duckduckgo", "ddgs"}

    @staticmethod
    def _ddgs_retry_due(check: dict[str, Any]) -> bool:
        retry_at = str(check.get("retry_at", "")).strip()
        if not retry_at:
            return True
        try:
            due_at = datetime.fromisoformat(retry_at)
        except ValueError:
            return True
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=timezone.utc)
        return due_at.astimezone(timezone.utc) <= datetime.now(timezone.utc)

    def _requeue_ddgs_verification(
        self,
        job: JobRecord,
        *,
        outcome: str,
        event_type: str,
        message: str,
    ) -> JobRecord:
        checkpoint = ScrapeCheckpoint.from_dict(job.checkpoint)
        checkpoint.security_check = None
        self.repository.save_checkpoint(job.id, checkpoint.as_dict())
        queued = self.repository.transition(job.id, JobStatus.QUEUED, outcome=outcome)
        RepositoryEventSink(self.repository, job.id, self.logger).emit(
            "info",
            event_type,
            message,
        )
        return queued

    @staticmethod
    def normalize_event_speaker_request(request: dict[str, Any]) -> dict[str, Any]:
        source_url = validate_public_source_url(
            str(request.get("source_url", "")),
            resolve=False,
        )
        provider = str(request.get("search_provider", "ddgs")).strip().lower()
        if provider not in {"ddgs", "brave"}:
            raise ValueError("event_speakers supports ddgs or brave search providers")
        raw_enrich = request.get("enrich_missing", True)
        if isinstance(raw_enrich, str):
            normalized = raw_enrich.strip().lower()
            if normalized not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                raise ValueError("enrich_missing must be a boolean")
            enrich_missing = normalized in {"true", "1", "yes", "on"}
        else:
            enrich_missing = bool(raw_enrich)
        return {
            "workflow": "event_speakers",
            "source_url": source_url,
            "enrich_missing": enrich_missing,
            "search_provider": provider,
            "scheduled": bool(request.get("scheduled", False)),
        }

    def preview_plan(self, request: dict[str, Any]) -> dict[str, Any]:
        normalized = self.normalize_lead_request(request)
        return {
            "strategy_version": normalized["strategy_version"],
            "query_count": len(normalized["query_plan"]),
            "query_plan": normalized["query_plan"],
        }

    def create_job(
        self,
        request: dict[str, Any],
        *,
        idempotency_key: str | None = None,
    ) -> JobRecord:
        if idempotency_key:
            existing = self.repository.get_job_by_idempotency_key(idempotency_key)
            if existing:
                return existing
        workflow = str(request.get("workflow", "lead")).strip().lower()
        if workflow == "lead":
            normalized = self.normalize_lead_request(request)
        elif workflow == "event_speakers":
            normalized = self.normalize_event_speaker_request(request)
        else:
            normalized = {**request, "workflow": workflow}
        job = self.repository.create_job(
            workflow, normalized, idempotency_key=idempotency_key,
        )
        sink = RepositoryEventSink(self.repository, job.id, self.logger)
        sink.emit("info", "job_created", f"Created {workflow} job.", {"workflow": workflow})
        return job

    def pause_job(self, job_id: str) -> JobRecord:
        job = self.repository.get_job(job_id)
        if job.status in {JobStatus.QUEUED, JobStatus.WAITING_VERIFICATION}:
            return self.repository.transition(job_id, JobStatus.PAUSED, outcome="Paused")
        if job.status == JobStatus.RUNNING:
            return self.repository.transition(job_id, JobStatus.PAUSE_REQUESTED, outcome="Pause requested")
        return job

    def resume_job(self, job_id: str) -> JobRecord:
        job = self.repository.get_job(job_id)
        if (
            job.status == JobStatus.WAITING_VERIFICATION
            and self._is_ddgs_security_check(job.checkpoint.get("security_check"))
        ):
            return self._requeue_ddgs_verification(
                job,
                outcome="DuckDuckGo retry queued",
                event_type="ddgs_manual_retry",
                message="Manual DuckDuckGo retry requested; job requeued.",
            )
        if job.status not in {JobStatus.PAUSED, JobStatus.FAILED}:
            return job
        return self.repository.transition(job_id, JobStatus.QUEUED, outcome="Queued")

    def cancel_job(self, job_id: str) -> JobRecord:
        job = self.repository.get_job(job_id)
        self.browser_manager.close(job.checkpoint.get("browser_state", {}))
        if job.status == JobStatus.RUNNING:
            return self.repository.transition(job_id, JobStatus.CANCEL_REQUESTED, outcome="Cancellation requested")
        if not job.status.terminal:
            return self.repository.transition(job_id, JobStatus.CANCELLED, outcome="Cancelled")
        return job

    def retry_job(self, job_id: str) -> JobRecord:
        job = self.repository.get_job(job_id)
        if (
            job.status == JobStatus.WAITING_VERIFICATION
            and self._is_ddgs_security_check(job.checkpoint.get("security_check"))
        ):
            return self._requeue_ddgs_verification(
                job,
                outcome="DuckDuckGo retry queued",
                event_type="ddgs_manual_retry",
                message="Manual DuckDuckGo retry requested; job requeued.",
            )
        if job.status not in {JobStatus.FAILED, JobStatus.EXHAUSTED, JobStatus.PAUSED}:
            return job
        return self.repository.transition(job_id, JobStatus.QUEUED, outcome="Retry queued")

    def poll_verification(self, job_id: str) -> JobRecord:
        job = self.repository.get_job(job_id)
        if job.status != JobStatus.WAITING_VERIFICATION:
            return job
        checkpoint = ScrapeCheckpoint.from_dict(job.checkpoint)
        check = checkpoint.security_check
        if self._is_ddgs_security_check(check):
            if check is None or not self._ddgs_retry_due(check):
                return job
            return self._requeue_ddgs_verification(
                job,
                outcome="DuckDuckGo cooldown elapsed",
                event_type="ddgs_cooldown_elapsed",
                message="DuckDuckGo cooldown elapsed; job requeued.",
            )
        if self.browser_manager.verification_resolved(checkpoint.browser_state, checkpoint.security_check):
            checkpoint.security_check = None
            self.repository.save_checkpoint(job_id, checkpoint.as_dict())
            job = self.repository.transition(job_id, JobStatus.QUEUED, outcome="Verification completed")
            RepositoryEventSink(self.repository, job_id, self.logger).emit(
                "info", "verification_completed", "Manual Google verification completed; job requeued.",
            )
        elif (
            not self.browser_manager.is_running(checkpoint.browser_state)
            and self.browser_manager.display_available()
        ):
            checkpoint.security_check = None
            self.repository.save_checkpoint(job_id, checkpoint.as_dict())
            job = self.repository.transition(
                job_id, JobStatus.QUEUED,
                outcome="Reopening the saved verification request",
            )
            RepositoryEventSink(self.repository, job_id, self.logger).emit(
                "warning", "verification_browser_recovered",
                "The browser process was lost; requeued only the saved challenged request.",
            )
        return job

    def run_job(self, job_id: str) -> ScrapeResult | JobRecord:
        job = self.repository.get_job(job_id)
        if job.workflow == "lead":
            return self.engine.run(job_id)
        sink = RepositoryEventSink(self.repository, job_id, self.logger)
        try:
            if job.status == JobStatus.QUEUED:
                self.repository.transition(job_id, JobStatus.RUNNING, outcome="Running")
            request = job.request
            provider_name = str(request.get("search_provider", "ddgs")).lower()
            if provider_name not in {"ddgs", "brave", "local"}:
                raise ValueError(
                    "Non-lead workflows support ddgs, brave, or local search providers."
                )
            if job.workflow == "event_speakers":
                return self.event_speaker_engine.run(job_id)
            registry = ProviderRegistry(
                self.config,
                event_sink=sink,
                limiter=self.engine.limiter,
                retries=self.engine.retries,
                proxies=self.engine.proxies,
            )
            search_client = LegacySearchClient(registry.get(provider_name), job_id)
            if job.workflow == "company":
                from core.company_intel import fetch_company_profile, scrape_company_employees

                company = str(request.get("company_name", "")).strip()
                profile = fetch_company_profile(company, search_client)
                employees = scrape_company_employees(
                    company, str(request.get("location", "")),
                    list(request.get("roles", [])), int(request.get("target_count", 20)),
                    set(), sink, search_client,
                )
                self.repository.save_artifact(job_id, "company_profile", profile)
                self.repository.save_artifact(job_id, "company_employees", employees)
            elif job.workflow == "competitor":
                from core.competitor_intel import (
                    fetch_company_summary_batch,
                    scrape_competitor_event_attendees,
                )

                leads = scrape_competitor_event_attendees(
                    list(request.get("competitors", [])), list(request.get("roles", [])),
                    list(request.get("locations", [])), list(request.get("event_keywords", [])),
                    int(request.get("target_count", 50)), set(), sink, search_client,
                )
                summaries = fetch_company_summary_batch(
                    [item.get("Company", "") for item in leads], sink, search_client,
                )
                self.repository.save_artifact(job_id, "competitor_leads", leads)
                self.repository.save_artifact(job_id, "company_summaries", summaries)
            elif job.workflow == "public_contact":
                from public_contact_finder.finder import PublicContactFinder

                http_client = ReliableHttpClient(
                    self.engine.limiter,
                    self.engine.retries,
                    self.engine.proxies,
                    sink,
                )
                finder = PublicContactFinder(
                    str(request.get("domain", "")),
                    max_pages=int(request.get("max_pages", 20)),
                    request_delay=self.config.rate_limits["public_contact"].minimum_interval_seconds,
                    status=lambda message: sink.emit("info", "contact_status", message),
                    search_client=search_client,
                    request_get=lambda url, **kwargs: http_client.get(
                        url, job_id=job_id, **kwargs,
                    ),
                )
                contacts = [item.to_dict() for item in finder.find(str(request.get("leader_name", "")))]
                self.repository.save_artifact(job_id, "public_contacts", contacts)
            elif job.workflow == "reconcile":
                rows = list(request.get("rows", []))
                mapping = dict(request.get("mapping", {}))
                reconciled = [
                    {target: row.get(source, "") if source else "" for target, source in mapping.items()}
                    for row in rows
                ]
                self.repository.save_artifact(job_id, "reconciled_rows", reconciled)
            else:
                raise ValueError(f"Unsupported workflow: {job.workflow}")
            current = self.repository.get_job(job_id)
            if current.status == JobStatus.PAUSE_REQUESTED:
                return self.repository.transition(job_id, JobStatus.PAUSED, outcome="Paused")
            if current.status == JobStatus.CANCEL_REQUESTED:
                return self.repository.transition(job_id, JobStatus.CANCELLED, outcome="Cancelled")
            return self.repository.transition(job_id, JobStatus.COMPLETED, outcome="Completed")
        except Exception as exc:
            sink.emit("error", "job_failed", f"{type(exc).__name__}: {exc}")
            current = self.repository.get_job(job_id)
            if current.status == JobStatus.CANCEL_REQUESTED:
                return self.repository.transition(job_id, JobStatus.CANCELLED, outcome="Cancelled")
            if current.status == JobStatus.PAUSE_REQUESTED:
                return self.repository.transition(job_id, JobStatus.PAUSED, outcome="Paused")
            return self.repository.transition(
                job_id, JobStatus.FAILED, outcome="Failed",
                error_code=type(exc).__name__, error_message=str(exc),
            )

    def import_legacy_session(self, qualified: list[dict[str, Any]], strict: list[dict[str, Any]]) -> JobRecord | None:
        if not qualified and not strict:
            return None
        marker = "legacy-streamlit-session"
        existing = [job for job in self.repository.list_jobs(limit=1000) if job.request.get("migration_marker") == marker]
        if existing:
            return existing[0]
        job = self.repository.create_job("lead", {
            "workflow": "lead", "migration_marker": marker, "target_count": len(qualified),
            "query_plan": [], "roles": [], "locations": [],
        })
        strict_urls = {normalize_linkedin_url(item.get("LinkedIn_URL", "")) for item in strict}
        for item in qualified:
            url = normalize_linkedin_url(item.get("LinkedIn_URL", ""))
            if not url:
                continue
            record = LeadRecord(
                name=str(item.get("Full_Name", "")), designation=str(item.get("Designation", "")),
                company=str(item.get("Company", "")), linkedin_url=url,
                verified_location=str(item.get("Location_Evidence", item.get("Location", ""))),
                score=int(item.get("Lead_Score", 50)), source="legacy_session",
                strict_qualified=url in strict_urls, evaluation={"legacy": dict(item)},
            )
            self.repository.save_qualified(job.id, record)
        return self.repository.transition(job.id, JobStatus.COMPLETED, outcome="Imported legacy Streamlit session")


class JobRunner:
    """Single-node runner with two generic workers and one Google lane."""

    def __init__(self, orchestrator: ScraperOrchestrator):
        self.orchestrator = orchestrator
        self.repository = orchestrator.repository
        self.config = orchestrator.config
        self.runner_id = f"runner-{uuid4()}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, self.config.scheduler.max_workers),
            thread_name_prefix="speedy-job",
        )
        self._google_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="speedy-google",
        )
        self._futures: dict[str, Future[Any]] = {}
        self._google_future: tuple[str, Future[Any]] | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self.repository.migrate()
        self.repository.recover_stale_jobs()
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="speedy-runner", daemon=True)
        self._thread.start()

    def _uses_google(self, job: JobRecord) -> bool:
        return job.workflow == "lead" and "google" in {
            job.request.get("discovery_provider"), job.request.get("validation_provider"),
        }

    def _execute(self, job: JobRecord) -> None:
        heartbeat_stop = threading.Event()

        def heartbeat_loop() -> None:
            while not heartbeat_stop.wait(60.0):
                self.repository.heartbeat(job.id, self.runner_id, lease_seconds=180)

        heartbeat = threading.Thread(target=heartbeat_loop, daemon=True)
        heartbeat.start()
        try:
            while not self._stop.is_set():
                self.orchestrator.run_job(job.id)
                refreshed = self.repository.get_job(job.id)
                if not self._uses_google(refreshed) or refreshed.status != JobStatus.WAITING_VERIFICATION:
                    return
                # A waiting Google job owns the only Google execution lane.
                # Other provider workflows continue on the generic pool.
                while (
                    not self._stop.wait(self.config.browser.captcha_poll_seconds)
                    and self.repository.get_job(job.id).status == JobStatus.WAITING_VERIFICATION
                ):
                    self.orchestrator.poll_verification(job.id)
                if self.repository.get_job(job.id).status != JobStatus.QUEUED:
                    return
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1.0)

    def _loop(self) -> None:
        last_verification_poll = 0.0
        last_cleanup = 0.0
        while not self._stop.is_set():
            for job_id, future in list(self._futures.items()):
                if future.done():
                    self._futures.pop(job_id, None)
                    try:
                        future.result()
                    except Exception as exc:
                        try:
                            self.repository.transition(
                                job_id, JobStatus.FAILED, outcome="Failed",
                                error_code=type(exc).__name__, error_message=str(exc),
                            )
                        except Exception:
                            pass
            if self._google_future and self._google_future[1].done():
                job_id, future = self._google_future
                self._google_future = None
                try:
                    future.result()
                except Exception as exc:
                    try:
                        self.repository.transition(
                            job_id, JobStatus.FAILED, outcome="Failed",
                            error_code=type(exc).__name__, error_message=str(exc),
                        )
                    except Exception:
                        pass
            if self._google_future is None:
                google_job = self.repository.claim_next_job(
                    self.runner_id, lease_seconds=180, lane="google",
                )
                if google_job:
                    self._google_future = (
                        google_job.id,
                        self._google_executor.submit(self._execute, google_job),
                    )
            capacity = max(0, self.config.scheduler.max_workers - len(self._futures))
            for _ in range(capacity):
                job = self.repository.claim_next_job(
                    self.runner_id, lease_seconds=180, lane="non_google",
                )
                if not job:
                    break
                self._futures[job.id] = self._executor.submit(self._execute, job)
            now = time.monotonic()
            if now - last_verification_poll >= self.config.browser.captcha_poll_seconds:
                for job in self.repository.list_jobs(limit=100, status=JobStatus.WAITING_VERIFICATION):
                    try:
                        self.orchestrator.poll_verification(job.id)
                    except Exception:
                        pass
                last_verification_poll = now
            if now - last_cleanup >= self.config.scheduler.cleanup_interval_seconds:
                self.repository.cleanup()
                self.repository.recover_stale_jobs()
                last_cleanup = now
            self._stop.wait(0.25)

    def stop(self, *, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
        for job in self.repository.list_jobs(limit=1000):
            if not self._uses_google(job):
                continue
            checkpoint = ScrapeCheckpoint.from_dict(job.checkpoint)
            self.orchestrator.browser_manager.close(checkpoint.browser_state)
            self.repository.save_checkpoint(job.id, checkpoint.as_dict())
        self._executor.shutdown(wait=False, cancel_futures=False)
        self._google_executor.shutdown(wait=False, cancel_futures=False)
