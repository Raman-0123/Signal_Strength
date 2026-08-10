"""Versioned FastAPI service for jobs, schedules, results, and exports."""

from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from speedy_scraper.config import load_catalog, load_config
from speedy_scraper.dedup import parse_dedup_content
from speedy_scraper.domain import JobStatus, ResultKind
from speedy_scraper.events import configure_logging
from speedy_scraper.exports import export_artifacts, export_leads
from speedy_scraper.orchestrator import JobRunner, ScraperOrchestrator
from speedy_scraper.repository import LeadRepository
from speedy_scraper.scheduler import SchedulerService


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="allow")
    workflow: Literal[
        "lead", "company", "competitor", "public_contact", "reconcile", "event_speakers",
    ] = "lead"
    source_url: str = ""
    enrich_missing: bool = True
    search_provider: str = "ddgs"
    location_ids: list[str] = Field(default_factory=list)
    role_ids: list[str] = Field(default_factory=list)
    industry_ids: list[str] = Field(default_factory=list)
    signal_ids: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)
    industries: list[str] = Field(default_factory=list)
    signals: list[str] = Field(default_factory=list)
    custom_keywords: list[str] | str = Field(default_factory=list)
    organizations: list[str] | str = Field(default_factory=list)
    business_model: str = "Any"
    gcc_only: bool = False
    target_count: int = Field(default=15, ge=1, le=500)
    discovery_provider: str = "ddgs"
    validation_provider: str = ""
    edited_queries: list[str] = Field(default_factory=list)
    dedup_import_ids: list[str] = Field(default_factory=list)
    browser_mode: Literal["interactive", "scheduled_headless"] = "interactive"


class ScheduleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = None
    name: str
    workflow: str = "lead"
    trigger: dict[str, Any]
    request: dict[str, Any]
    timezone: str = "Asia/Kolkata"
    enabled: bool = True


class SchedulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    trigger: dict[str, Any] | None = None
    request: dict[str, Any] | None = None
    timezone: str | None = None
    enabled: bool | None = None


class ServiceContainer:
    def __init__(self, config_path: str | Path | None = None):
        self.config = load_config(config_path)
        if self.config.api_host not in {"127.0.0.1", "localhost", "::1"} and not self.config.api_key:
            raise RuntimeError("SPEEDY_SCRAPER_API_KEY is required for non-loopback API binding.")
        self.logger = configure_logging(self.config.data_dir, self.config.log_level)
        self.repository = LeadRepository(self.config.storage)
        self.repository.migrate()
        self.orchestrator = ScraperOrchestrator(
            self.config, self.repository, catalog=load_catalog(), logger=self.logger,
        )
        self.runner = JobRunner(self.orchestrator)
        self.scheduler = SchedulerService(self.orchestrator)

    def start(self) -> None:
        self.runner.start()
        self.scheduler.start()

    def stop(self) -> None:
        self.scheduler.stop()
        self.runner.stop()


def _problem(status_code: int, code: str, message: str, details: Any = None):
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "details": details},
    )


def create_app(config_path: str | Path | None = None) -> FastAPI:
    container = ServiceContainer(config_path)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        container.start()
        yield
        container.stop()

    app = FastAPI(
        title="Speedy-Scraper API",
        version="2.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.services = container

    def authenticate(x_api_key: str | None = Header(default=None, alias="X-API-Key")) -> None:
        required = container.config.api_key
        if required and not secrets.compare_digest(x_api_key or "", required):
            _problem(status.HTTP_401_UNAUTHORIZED, "invalid_api_key", "A valid X-API-Key header is required.")

    protected = Depends(authenticate)

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(_request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and {"code", "message"} <= set(detail):
            payload = {**detail, "details": detail.get("details")}
        else:
            payload = {"code": "http_error", "message": str(detail), "details": None}
        return JSONResponse(status_code=exc.status_code, content=payload, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": "Request validation failed.",
                "details": exc.errors(),
            },
        )

    @app.exception_handler(KeyError)
    async def key_error_handler(_request, exc: KeyError):
        return JSONResponse(
            status_code=404,
            content={"code": "not_found", "message": str(exc), "details": None},
        )

    @app.exception_handler(ValueError)
    async def value_error_handler(_request, exc: ValueError):
        return JSONResponse(
            status_code=422,
            content={"code": "invalid_request", "message": str(exc), "details": None},
        )

    @app.get("/health/live")
    def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    def ready():
        try:
            container.repository.connect().close()
        except Exception as exc:
            _problem(503, "database_unavailable", "SQLite is unavailable.", type(exc).__name__)
        return {"status": "ready", "database": str(container.repository.path)}

    @app.get("/metrics")
    def prometheus_metrics(_auth=protected):
        lines = [
            "# HELP speedy_scraper_events_total Persisted scraper metric totals.",
            "# TYPE speedy_scraper_events_total counter",
        ]
        for name, value in sorted(container.repository.metrics().items()):
            safe = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)
            lines.append(f'speedy_scraper_events_total{{metric="{safe}"}} {value}')
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    @app.get("/api/v1/metrics")
    def aggregate_metrics(_auth=protected):
        return container.repository.metrics()

    @app.get("/api/v1/catalog")
    def catalog(_auth=protected):
        value = container.orchestrator.catalog
        return {
            "version": value.version,
            "locations": value.locations,
            "roles": value.roles,
            "industries": value.industries,
            "signals": value.signals,
            "role_labels": value.role_labels,
            "industry_labels": value.industry_labels,
        }

    @app.post("/api/v1/plans/preview")
    def preview(request: JobCreateRequest, _auth=protected):
        return container.orchestrator.preview_plan(request.model_dump(exclude_none=True))

    @app.post("/api/v1/jobs", status_code=202)
    def create_job(
        request: JobCreateRequest,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        _auth=protected,
    ):
        return container.orchestrator.create_job(
            request.model_dump(exclude_none=True), idempotency_key=idempotency_key,
        ).as_dict()

    @app.get("/api/v1/jobs")
    def list_jobs(
        limit: int = Query(default=100, ge=1, le=1000),
        job_status: JobStatus | None = Query(default=None, alias="status"),
        _auth=protected,
    ):
        return {"items": [job.as_dict() for job in container.repository.list_jobs(limit=limit, status=job_status)]}

    @app.get("/api/v1/jobs/{job_id}")
    def get_job(job_id: str, _auth=protected):
        return container.repository.get_job(job_id).as_dict()

    @app.post("/api/v1/jobs/{job_id}/pause")
    def pause_job(job_id: str, _auth=protected):
        return container.orchestrator.pause_job(job_id).as_dict()

    @app.post("/api/v1/jobs/{job_id}/resume")
    def resume_job(job_id: str, _auth=protected):
        return container.orchestrator.resume_job(job_id).as_dict()

    @app.post("/api/v1/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, _auth=protected):
        return container.orchestrator.cancel_job(job_id).as_dict()

    @app.post("/api/v1/jobs/{job_id}/retry")
    def retry_job(job_id: str, _auth=protected):
        return container.orchestrator.retry_job(job_id).as_dict()

    @app.post("/api/v1/jobs/{job_id}/verification/check")
    def verification_check(job_id: str, _auth=protected):
        return container.orchestrator.poll_verification(job_id).as_dict()

    @app.get("/api/v1/jobs/{job_id}/results")
    def results(
        job_id: str,
        kind: ResultKind = ResultKind.QUALIFIED,
        detail: Literal["export", "full"] = "export",
        _auth=protected,
    ):
        job = container.repository.get_job(job_id)
        if job.workflow != "lead":
            return {"items": container.repository.list_artifacts(job_id)}
        records = container.repository.list_results(job_id, kind)
        return {"items": [record.as_export_row() if detail == "export" else record.as_dict() for record in records]}

    @app.get("/api/v1/jobs/{job_id}/events")
    def events(job_id: str, after_id: int = 0, limit: int = 500, _auth=protected):
        container.repository.get_job(job_id)
        return {"items": container.repository.list_events(job_id, after_id=after_id, limit=limit)}

    @app.get("/api/v1/jobs/{job_id}/metrics")
    def job_metrics(job_id: str, _auth=protected):
        container.repository.get_job(job_id)
        return container.repository.metrics(job_id)

    @app.get("/api/v1/jobs/{job_id}/exports/{fmt}")
    def export(job_id: str, fmt: Literal["csv", "xlsx", "json"], kind: ResultKind = ResultKind.QUALIFIED, _auth=protected):
        job = container.repository.get_job(job_id)
        if job.workflow == "lead":
            content, media_type, filename = export_leads(container.repository, job_id, fmt, kind)
        else:
            content, media_type, filename = export_artifacts(container.repository, job_id, fmt)
        return Response(
            content,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/api/v1/dedup-imports", status_code=201)
    async def create_dedup_import(file: UploadFile = File(...), _auth=protected):
        content = await file.read()
        name = file.filename or "dedup-import"
        try:
            keys, sheet_count = parse_dedup_content(name, content)
        except Exception as exc:
            _problem(422, "invalid_import", f"Could not parse {name}.", type(exc).__name__)
        import_id = container.repository.create_dedup_import(name, keys, sheet_count)
        return {"id": import_id, "name": name, "sheet_count": sheet_count, "key_count": len(keys)}

    @app.get("/api/v1/dedup-imports")
    def list_dedup_imports(_auth=protected):
        return {"items": container.repository.list_dedup_imports()}

    @app.delete("/api/v1/dedup-imports/{import_id}", status_code=204)
    def delete_dedup_import(import_id: str, _auth=protected):
        if not container.repository.delete_dedup_import(import_id):
            _problem(404, "not_found", f"Unknown dedup import: {import_id}")
        return Response(status_code=204)

    @app.get("/api/v1/schedules")
    def list_schedules(_auth=protected):
        return {"items": [item.as_dict() for item in container.repository.list_schedules()]}

    @app.post("/api/v1/schedules", status_code=201)
    def create_schedule(request: ScheduleRequest, _auth=protected):
        return container.scheduler.create(request.model_dump(exclude_none=True)).as_dict()

    @app.patch("/api/v1/schedules/{schedule_id}")
    def update_schedule(schedule_id: str, request: SchedulePatch, _auth=protected):
        return container.scheduler.update(schedule_id, request.model_dump(exclude_none=True)).as_dict()

    @app.delete("/api/v1/schedules/{schedule_id}", status_code=204)
    def delete_schedule(schedule_id: str, _auth=protected):
        if not container.scheduler.delete(schedule_id):
            _problem(404, "not_found", f"Unknown schedule: {schedule_id}")
        return Response(status_code=204)

    @app.post("/api/v1/schedules/{schedule_id}/run-now", status_code=202)
    def run_schedule(schedule_id: str, _auth=protected):
        return container.scheduler.run_now(schedule_id).as_dict()

    return app


def app_factory() -> FastAPI:
    """Uvicorn factory that honors the CLI-provided settings path."""
    return create_app(os.environ.get("SPEEDY_SCRAPER_CONFIG") or None)
