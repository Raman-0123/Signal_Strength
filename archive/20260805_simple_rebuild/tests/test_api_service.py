import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from speedy_scraper.api import app_factory, create_app
from speedy_scraper.domain import JobStatus


class ApiServiceTests(unittest.TestCase):
    def _settings(self, directory: str) -> Path:
        path = Path(directory) / "api.yaml"
        path.write_text(
            "storage:\n"
            f"  database_path: {Path(directory) / 'api.db'}\n"
            "scheduler:\n  cleanup_interval_seconds: 3600\n",
            encoding="utf-8",
        )
        return path

    def test_auth_idempotency_status_shapes_and_schedule_crud(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SPEEDY_SCRAPER_API_KEY": "test-key"}, clear=False,
        ):
            app = create_app(self._settings(directory))
            with TestClient(app) as client:
                self.assertEqual(client.get("/health/live").status_code, 200)
                denied = client.get("/api/v1/jobs")
                self.assertEqual(denied.status_code, 401)
                self.assertEqual(denied.json()["code"], "invalid_api_key")
                headers = {"X-API-Key": "test-key", "Idempotency-Key": "same-request"}
                payload = {
                    "workflow": "reconcile",
                    "rows": [{"old": "value"}],
                    "mapping": {"new": "old"},
                }
                first = client.post("/api/v1/jobs", json=payload, headers=headers)
                second = client.post("/api/v1/jobs", json=payload, headers=headers)
                self.assertEqual(first.status_code, 202)
                self.assertEqual(first.json()["id"], second.json()["id"])

                invalid = client.post(
                    "/api/v1/jobs",
                    json={"workflow": "lead", "target_count": 0},
                    headers={"X-API-Key": "test-key"},
                )
                self.assertEqual(invalid.status_code, 422)
                self.assertEqual(invalid.json()["code"], "validation_error")

                schedule = client.post(
                    "/api/v1/schedules",
                    json={
                        "name": "Nightly net-new",
                        "workflow": "reconcile",
                        "trigger": {"type": "interval", "hours": 24},
                        "request": {"rows": [], "mapping": {}},
                        "timezone": "Asia/Kolkata",
                        "enabled": False,
                    },
                    headers={"X-API-Key": "test-key"},
                )
                self.assertEqual(schedule.status_code, 201)
                schedule_id = schedule.json()["id"]
                listed = client.get(
                    "/api/v1/schedules", headers={"X-API-Key": "test-key"},
                ).json()["items"]
                self.assertEqual([item["id"] for item in listed], [schedule_id])
                deleted = client.delete(
                    f"/api/v1/schedules/{schedule_id}",
                    headers={"X-API-Key": "test-key"},
                )
                self.assertEqual(deleted.status_code, 204)

    def test_full_job_result_dedup_metrics_and_schedule_surface(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"SPEEDY_SCRAPER_API_KEY": "surface-key"}, clear=False,
        ):
            settings = self._settings(directory)
            app = create_app(settings)
            headers = {"X-API-Key": "surface-key"}
            with TestClient(app) as client:
                services = app.state.services
                services.runner.stop()
                services.scheduler.stop()
                self.assertEqual(client.get("/health/ready").status_code, 200)
                self.assertTrue(client.get("/api/v1/catalog", headers=headers).json()["roles"])
                speaker_job = client.post(
                    "/api/v1/jobs",
                    json={
                        "workflow": "event_speakers",
                        "source_url": "https://globalfintechfest.com/speakers",
                        "enrich_missing": False,
                        "search_provider": "ddgs",
                    },
                    headers=headers,
                )
                self.assertEqual(speaker_job.status_code, 202)
                self.assertEqual(speaker_job.json()["request"]["workflow"], "event_speakers")
                preview = client.post(
                    "/api/v1/plans/preview",
                    json={
                        "workflow": "lead", "locations": ["Delhi"], "roles": ["CMO"],
                        "edited_queries": ["edited query"],
                    },
                    headers=headers,
                )
                self.assertEqual(preview.status_code, 200)
                self.assertEqual(preview.json()["query_count"], 1)

                created = client.post(
                    "/api/v1/jobs",
                    json={"workflow": "lead", "locations": ["Delhi"], "roles": ["CMO"]},
                    headers=headers,
                )
                self.assertEqual(created.status_code, 202)
                job_id = created.json()["id"]
                self.assertEqual(created.json()["request"]["discovery_provider"], "ddgs")
                self.assertEqual(created.json()["request"]["validation_provider"], "ddgs")
                rejected_google = client.post(
                    "/api/v1/jobs",
                    json={
                        "workflow": "lead",
                        "locations": ["Delhi"],
                        "roles": ["CMO"],
                        "search_provider": "google",
                    },
                    headers=headers,
                )
                self.assertEqual(rejected_google.status_code, 422)
                self.assertIn("only support ddgs", rejected_google.json()["message"])
                self.assertEqual(client.get(f"/api/v1/jobs/{job_id}", headers=headers).status_code, 200)
                self.assertEqual(
                    client.get(f"/api/v1/jobs/{job_id}/results", headers=headers).json()["items"],
                    [],
                )
                self.assertTrue(
                    client.get(f"/api/v1/jobs/{job_id}/events", headers=headers).json()["items"],
                )
                services.repository.increment_metric(job_id, "queries", 2)
                self.assertEqual(
                    client.get(f"/api/v1/jobs/{job_id}/metrics", headers=headers).json()["queries"],
                    2,
                )
                self.assertIn(
                    "speedy_scraper_events_total",
                    client.get("/metrics", headers=headers).text,
                )
                self.assertEqual(
                    client.get("/api/v1/metrics", headers=headers).json()["queries"],
                    2,
                )
                exported = client.get(
                    f"/api/v1/jobs/{job_id}/exports/csv", headers=headers,
                )
                self.assertEqual(exported.status_code, 200)
                self.assertEqual(exported.text.strip(), "Name,Designation,Company,Location")
                self.assertEqual(
                    client.post(f"/api/v1/jobs/{job_id}/pause", headers=headers).json()["status"],
                    "paused",
                )
                self.assertEqual(
                    client.post(f"/api/v1/jobs/{job_id}/resume", headers=headers).json()["status"],
                    "queued",
                )
                self.assertEqual(
                    client.post(
                        f"/api/v1/jobs/{job_id}/verification/check", headers=headers,
                    ).json()["status"],
                    "queued",
                )
                self.assertEqual(
                    client.post(f"/api/v1/jobs/{job_id}/cancel", headers=headers).json()["status"],
                    "cancelled",
                )

                failed = services.repository.create_job("reconcile", {"workflow": "reconcile"})
                services.repository.transition(failed.id, JobStatus.RUNNING)
                services.repository.transition(failed.id, JobStatus.FAILED)
                self.assertEqual(
                    client.post(f"/api/v1/jobs/{failed.id}/retry", headers=headers).json()["status"],
                    "queued",
                )

                dedup = client.post(
                    "/api/v1/dedup-imports",
                    files={
                        "file": (
                            "prior.csv",
                            b"Name,Company,LinkedIn\nJane,Acme,https://linkedin.com/in/jane\n",
                            "text/csv",
                        ),
                    },
                    headers=headers,
                )
                self.assertEqual(dedup.status_code, 201)
                import_id = dedup.json()["id"]
                self.assertEqual(
                    client.get("/api/v1/dedup-imports", headers=headers).json()["items"][0]["id"],
                    import_id,
                )
                self.assertEqual(
                    client.delete(f"/api/v1/dedup-imports/{import_id}", headers=headers).status_code,
                    204,
                )

                artifact_job = services.repository.create_job("reconcile", {"workflow": "reconcile"})
                services.repository.save_artifact(artifact_job.id, "rows", [{"value": "ok"}])
                self.assertEqual(
                    client.get(
                        f"/api/v1/jobs/{artifact_job.id}/results", headers=headers,
                    ).json()["items"][0]["payload"],
                    [{"value": "ok"}],
                )
                self.assertIn(
                    "ok",
                    client.get(
                        f"/api/v1/jobs/{artifact_job.id}/exports/json", headers=headers,
                    ).text,
                )

                schedule = client.post(
                    "/api/v1/schedules",
                    json={
                        "name": "On demand", "workflow": "reconcile",
                        "trigger": {"type": "interval", "hours": 1},
                        "request": {}, "enabled": False,
                    },
                    headers=headers,
                ).json()
                changed = client.patch(
                    f"/api/v1/schedules/{schedule['id']}",
                    json={"enabled": True}, headers=headers,
                )
                self.assertTrue(changed.json()["enabled"])
                self.assertEqual(
                    client.post(
                        f"/api/v1/schedules/{schedule['id']}/run-now", headers=headers,
                    ).status_code,
                    202,
                )
                self.assertEqual(
                    client.delete("/api/v1/schedules/missing", headers=headers).status_code,
                    404,
                )
                self.assertEqual(
                    client.get("/api/v1/jobs/missing", headers=headers).json()["code"],
                    "not_found",
                )

            with patch.dict(os.environ, {"SPEEDY_SCRAPER_CONFIG": str(settings)}):
                self.assertEqual(app_factory().title, "Speedy-Scraper API")


if __name__ == "__main__":
    unittest.main()
