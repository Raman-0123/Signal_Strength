"""Local and HTTP gateways consumed by Streamlit and other clients."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import requests

from speedy_scraper.config import load_catalog, load_config
from speedy_scraper.dedup import parse_dedup_content
from speedy_scraper.domain import ResultKind
from speedy_scraper.events import configure_logging
from speedy_scraper.exports import export_artifacts, export_leads
from speedy_scraper.orchestrator import JobRunner, ScraperOrchestrator
from speedy_scraper.repository import LeadRepository


class LocalGateway:
    def __init__(self, config_path: str | Path | None = None, *, start_runner: bool = True):
        self.config = load_config(config_path)
        self.repository = LeadRepository(self.config.storage)
        self.repository.migrate()
        self.orchestrator = ScraperOrchestrator(
            self.config, self.repository, catalog=load_catalog(),
            logger=configure_logging(self.config.data_dir, self.config.log_level),
        )
        self.runner = JobRunner(self.orchestrator)
        if start_runner:
            self.runner.start()

    def catalog(self) -> dict[str, Any]:
        catalog = self.orchestrator.catalog
        return {
            "version": catalog.version, "locations": catalog.locations,
            "roles": catalog.roles, "industries": catalog.industries,
            "signals": catalog.signals, "role_labels": catalog.role_labels,
            "industry_labels": catalog.industry_labels,
        }

    def preview(self, request: dict[str, Any]) -> dict[str, Any]:
        return self.orchestrator.preview_plan(request)

    def create_job(self, request: dict[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        return self.orchestrator.create_job(request, idempotency_key=idempotency_key).as_dict()

    def list_jobs(self, limit: int = 100) -> list[dict[str, Any]]:
        return [job.as_dict() for job in self.repository.list_jobs(limit=limit)]

    def get_job(self, job_id: str) -> dict[str, Any]:
        return self.repository.get_job(job_id).as_dict()

    def pause(self, job_id: str) -> dict[str, Any]:
        return self.orchestrator.pause_job(job_id).as_dict()

    def resume(self, job_id: str) -> dict[str, Any]:
        return self.orchestrator.resume_job(job_id).as_dict()

    def cancel(self, job_id: str) -> dict[str, Any]:
        return self.orchestrator.cancel_job(job_id).as_dict()

    def retry(self, job_id: str) -> dict[str, Any]:
        return self.orchestrator.retry_job(job_id).as_dict()

    def check_verification(self, job_id: str) -> dict[str, Any]:
        return self.orchestrator.poll_verification(job_id).as_dict()

    def results(self, job_id: str, kind: str = "qualified", detail: str = "export") -> list[dict[str, Any]]:
        job = self.repository.get_job(job_id)
        if job.workflow != "lead":
            return self.repository.list_artifacts(job_id)
        records = self.repository.list_results(job_id, ResultKind(kind))
        return [record.as_export_row() if detail == "export" else record.as_dict() for record in records]

    def events(self, job_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        return self.repository.list_events(job_id, after_id=after_id)

    def export(self, job_id: str, fmt: str, kind: str = "qualified") -> tuple[bytes, str, str]:
        job = self.repository.get_job(job_id)
        if job.workflow == "lead":
            return export_leads(self.repository, job_id, fmt, ResultKind(kind))
        return export_artifacts(self.repository, job_id, fmt)

    def import_legacy(self, qualified, strict):
        job = self.orchestrator.import_legacy_session(qualified, strict)
        return job.as_dict() if job else None

    def import_dedup_file(self, name: str, content: bytes) -> dict[str, Any]:
        keys, sheet_count = parse_dedup_content(name, content)
        import_id = self.repository.create_dedup_import(name, keys, sheet_count)
        return {
            "id": import_id, "name": name,
            "sheet_count": sheet_count, "key_count": len(keys),
        }

    def wait(self, job_id: str, *, timeout: float = 900.0, interval: float = 0.25) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            job = self.get_job(job_id)
            if job["status"] in {
                "completed", "exhausted", "failed", "paused", "cancelled",
                "waiting_verification",
            }:
                return job
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for job {job_id}.")
            time.sleep(interval)


class ApiGateway:
    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        if api_key:
            self.session.headers["X-API-Key"] = api_key

    def _request(self, method: str, path: str, **kwargs):
        response = self.session.request(method, self.base_url + path, timeout=60, **kwargs)
        if response.status_code >= 400:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise RuntimeError(f"API {response.status_code}: {detail}")
        return response

    def catalog(self):
        return self._request("GET", "/api/v1/catalog").json()

    def preview(self, request):
        return self._request("POST", "/api/v1/plans/preview", json=request).json()

    def create_job(self, request, idempotency_key=None):
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        return self._request("POST", "/api/v1/jobs", json=request, headers=headers).json()

    def list_jobs(self, limit=100):
        return self._request("GET", "/api/v1/jobs", params={"limit": limit}).json()["items"]

    def get_job(self, job_id):
        return self._request("GET", f"/api/v1/jobs/{job_id}").json()

    def pause(self, job_id):
        return self._request("POST", f"/api/v1/jobs/{job_id}/pause").json()

    def resume(self, job_id):
        return self._request("POST", f"/api/v1/jobs/{job_id}/resume").json()

    def cancel(self, job_id):
        return self._request("POST", f"/api/v1/jobs/{job_id}/cancel").json()

    def retry(self, job_id):
        return self._request("POST", f"/api/v1/jobs/{job_id}/retry").json()

    def check_verification(self, job_id):
        return self._request("POST", f"/api/v1/jobs/{job_id}/verification/check").json()

    def results(self, job_id, kind="qualified", detail="export"):
        return self._request(
            "GET", f"/api/v1/jobs/{job_id}/results",
            params={"kind": kind, "detail": detail},
        ).json()["items"]

    def events(self, job_id, after_id=0):
        return self._request(
            "GET", f"/api/v1/jobs/{job_id}/events", params={"after_id": after_id},
        ).json()["items"]

    def export(self, job_id, fmt, kind="qualified"):
        response = self._request(
            "GET", f"/api/v1/jobs/{job_id}/exports/{fmt}", params={"kind": kind},
        )
        disposition = response.headers.get("Content-Disposition", "")
        filename = disposition.split("filename=", 1)[-1].strip('"') if "filename=" in disposition else f"{job_id}.{fmt}"
        return response.content, response.headers.get("content-type", "application/octet-stream"), filename

    def import_legacy(self, qualified, strict):
        return None

    def import_dedup_file(self, name, content):
        response = self._request(
            "POST",
            "/api/v1/dedup-imports",
            files={"file": (name, content, "application/octet-stream")},
        )
        return response.json()

    def wait(self, job_id, *, timeout=900.0, interval=0.5):
        deadline = time.monotonic() + timeout
        while True:
            job = self.get_job(job_id)
            if job["status"] in {
                "completed", "exhausted", "failed", "paused", "cancelled",
                "waiting_verification",
            }:
                return job
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for job {job_id}.")
            time.sleep(interval)


def create_gateway(*, start_runner: bool = True):
    if url := os.environ.get("SPEEDY_SCRAPER_API_URL", "").strip():
        return ApiGateway(url, os.environ.get("SPEEDY_SCRAPER_API_KEY", ""))
    return LocalGateway(start_runner=start_runner)
