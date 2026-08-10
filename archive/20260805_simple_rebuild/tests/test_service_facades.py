import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from speedy_scraper.domain import JobStatus
from speedy_scraper.gateway import ApiGateway, LocalGateway, create_gateway


class _Response:
    def __init__(self, payload=None, *, status=200, content=b"payload", headers=None, text=""):
        self._payload = payload if payload is not None else {}
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class _Contact:
    def to_dict(self):
        return {"phone": "+91 12345"}


class ServiceFacadeTests(unittest.TestCase):
    def _settings(self, directory):
        path = Path(directory) / "settings.yaml"
        path.write_text(
            f"data_dir: {Path(directory) / 'data'}\n"
            f"storage:\n  database_path: {Path(directory) / 'service.db'}\n"
            "scheduler:\n  max_workers: 1\n",
            encoding="utf-8",
        )
        return path

    def test_local_gateway_jobs_results_controls_imports_and_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway = LocalGateway(self._settings(directory), start_runner=False)
            catalog = gateway.catalog()
            self.assertIn("locations", catalog)
            preview = gateway.preview({
                "workflow": "lead", "locations": ["Delhi"], "roles": ["CMO"],
                "edited_queries": ["my edited query"],
            })
            self.assertEqual(preview["query_count"], 1)
            self.assertEqual(preview["query_plan"][0]["fallback_query"], "")

            reconcile = gateway.create_job({
                "workflow": "reconcile", "rows": [{"old": "value"}],
                "mapping": {"new": "old"},
            }, idempotency_key="reconcile-once")
            self.assertEqual(
                gateway.create_job({"workflow": "reconcile"}, idempotency_key="reconcile-once")["id"],
                reconcile["id"],
            )
            gateway.orchestrator.run_job(reconcile["id"])
            self.assertEqual(gateway.wait(reconcile["id"], timeout=0)["status"], "completed")
            self.assertEqual(gateway.results(reconcile["id"])[0]["payload"], [{"new": "value"}])
            self.assertEqual(gateway.export(reconcile["id"], "json")[1], "application/json")
            self.assertTrue(gateway.events(reconcile["id"]))
            self.assertEqual(gateway.get_job(reconcile["id"])["id"], reconcile["id"])
            self.assertTrue(gateway.list_jobs())

            control = gateway.create_job({"workflow": "reconcile"})
            self.assertEqual(gateway.pause(control["id"])["status"], "paused")
            self.assertEqual(gateway.resume(control["id"])["status"], "queued")
            self.assertEqual(gateway.check_verification(control["id"])["status"], "queued")
            self.assertEqual(gateway.cancel(control["id"])["status"], "cancelled")

            failed = gateway.repository.create_job("unsupported", {"workflow": "unsupported"})
            self.assertEqual(gateway.orchestrator.run_job(failed.id).status, JobStatus.FAILED)
            self.assertEqual(gateway.retry(failed.id)["status"], "queued")

            legacy = gateway.import_legacy([{
                "Full_Name": "Legacy Lead", "Designation": "CEO", "Company": "Old Co",
                "LinkedIn_URL": "https://linkedin.com/in/legacy", "Location": "Pune",
            }], [])
            self.assertEqual(legacy["status"], "completed")
            self.assertEqual(gateway.import_legacy([], []), None)
            imported = gateway.import_dedup_file(
                "prior.csv", b"Name,Company,LinkedIn\nJane,Acme,https://linkedin.com/in/jane\n",
            )
            self.assertEqual(imported["key_count"], 2)

            queued = gateway.create_job({"workflow": "reconcile"})
            with self.assertRaises(TimeoutError):
                gateway.wait(queued["id"], timeout=0, interval=0)

    def test_non_lead_workflows_and_requested_terminal_states(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway = LocalGateway(self._settings(directory), start_runner=False)

            company = gateway.create_job({
                "workflow": "company", "company_name": "Acme", "location": "Delhi",
                "roles": ["CMO"], "target_count": 1, "search_provider": "local",
            })
            with (
                patch("core.company_intel.fetch_company_profile", return_value={"name": "Acme"}),
                patch("core.company_intel.scrape_company_employees", return_value=[{"Name": "Jane"}]),
            ):
                self.assertEqual(gateway.orchestrator.run_job(company["id"]).status, JobStatus.COMPLETED)
            self.assertEqual(len(gateway.results(company["id"])), 2)

            competitor = gateway.create_job({
                "workflow": "competitor", "competitors": ["Rival"], "roles": ["VP"],
                "locations": ["Mumbai"], "event_keywords": ["summit"],
                "search_provider": "local",
            })
            with (
                patch("core.competitor_intel.scrape_competitor_event_attendees", return_value=[{"Company": "Acme"}]),
                patch("core.competitor_intel.fetch_company_summary_batch", return_value={"Acme": {}}),
            ):
                self.assertEqual(gateway.orchestrator.run_job(competitor["id"]).status, JobStatus.COMPLETED)

            contact = gateway.create_job({
                "workflow": "public_contact", "domain": "example.com", "leader_name": "Jane",
                "search_provider": "local",
            })
            with patch("public_contact_finder.finder.PublicContactFinder") as finder:
                finder.return_value.find.return_value = [_Contact()]
                self.assertEqual(gateway.orchestrator.run_job(contact["id"]).status, JobStatus.COMPLETED)

            paused = gateway.repository.create_job("reconcile", {"workflow": "reconcile", "rows": [], "mapping": {}})
            gateway.repository.transition(paused.id, JobStatus.RUNNING)
            gateway.pause(paused.id)
            self.assertEqual(gateway.orchestrator.run_job(paused.id).status, JobStatus.PAUSED)

            cancelled = gateway.repository.create_job("reconcile", {"workflow": "reconcile", "rows": [], "mapping": {}})
            gateway.repository.transition(cancelled.id, JobStatus.RUNNING)
            gateway.cancel(cancelled.id)
            self.assertEqual(gateway.orchestrator.run_job(cancelled.id).status, JobStatus.CANCELLED)

            invalid = gateway.create_job({"workflow": "company", "search_provider": "google"})
            self.assertEqual(gateway.orchestrator.run_job(invalid["id"]).status, JobStatus.FAILED)

    def test_api_gateway_routes_errors_exports_and_factory_selection(self):
        gateway = ApiGateway("http://localhost:8000/", "secret")
        self.assertEqual(gateway.base_url, "http://localhost:8000")
        self.assertEqual(gateway.session.headers["X-API-Key"], "secret")
        response = _Response({"items": [{"id": "job"}]})
        gateway._request = Mock(return_value=response)
        self.assertEqual(gateway.catalog(), {"items": [{"id": "job"}]})
        self.assertEqual(gateway.preview({"workflow": "lead"}), response.json())
        self.assertEqual(gateway.create_job({}, "key"), response.json())
        self.assertEqual(gateway.list_jobs(), [{"id": "job"}])
        self.assertEqual(gateway.get_job("job"), response.json())
        for method in (gateway.pause, gateway.resume, gateway.cancel, gateway.retry, gateway.check_verification):
            self.assertEqual(method("job"), response.json())
        self.assertEqual(gateway.results("job"), [{"id": "job"}])
        self.assertEqual(gateway.events("job"), [{"id": "job"}])
        self.assertIsNone(gateway.import_legacy([], []))
        self.assertEqual(gateway.import_dedup_file("a.csv", b"x"), response.json())

        gateway._request.return_value = _Response(
            content=b"csv", headers={
                "Content-Disposition": 'attachment; filename="leads.csv"',
                "content-type": "text/csv",
            },
        )
        self.assertEqual(gateway.export("job", "csv"), (b"csv", "text/csv", "leads.csv"))

        real = ApiGateway("http://localhost")
        real.session.request = Mock(return_value=_Response({"message": "bad"}, status=500))
        with self.assertRaisesRegex(RuntimeError, "API 500"):
            real._request("GET", "/bad")
        real.session.request = Mock(return_value=_Response(ValueError(), status=500, text="plain error"))
        with self.assertRaisesRegex(RuntimeError, "plain error"):
            real._request("GET", "/bad")

        waiting = ApiGateway("http://localhost")
        waiting.get_job = Mock(return_value={"status": "completed"})
        self.assertEqual(waiting.wait("job", timeout=0)["status"], "completed")
        waiting.get_job = Mock(return_value={"status": "queued"})
        with self.assertRaises(TimeoutError):
            waiting.wait("job", timeout=0, interval=0)

        with (
            patch.dict("os.environ", {"SPEEDY_SCRAPER_API_URL": "http://remote", "SPEEDY_SCRAPER_API_KEY": "k"}),
            patch("speedy_scraper.gateway.ApiGateway", return_value="remote") as remote,
        ):
            self.assertEqual(create_gateway(), "remote")
            remote.assert_called_once_with("http://remote", "k")
        with (
            patch.dict("os.environ", {"SPEEDY_SCRAPER_API_URL": ""}),
            patch("speedy_scraper.gateway.LocalGateway", return_value="local"),
        ):
            self.assertEqual(create_gateway(start_runner=False), "local")


if __name__ == "__main__":
    unittest.main()
