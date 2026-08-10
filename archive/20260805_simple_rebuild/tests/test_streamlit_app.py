import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from core.query_builder import QUERY_STRATEGY_VERSION
from lead_generator_cli import INDUSTRIES, INDUSTRY_LABELS
from speedy_scraper.config import load_catalog


class _FakeGateway:
    created_payloads = []

    def __init__(self):
        catalog = load_catalog()
        self.created_jobs = []
        self._catalog = {
            "version": catalog.version,
            "locations": catalog.locations,
            "roles": catalog.roles,
            "industries": catalog.industries,
            "signals": catalog.signals,
            "role_labels": catalog.role_labels,
            "industry_labels": catalog.industry_labels,
        }

    def catalog(self):
        return self._catalog

    def list_jobs(self, limit=100):
        return []

    def import_legacy(self, *_args):
        return None

    def import_dedup_file(self, name, content):
        return {"id": f"dedup-{name}", "name": name, "sheet_count": 1, "key_count": 0}

    def create_job(self, request):
        self.created_jobs.append(request)
        type(self).created_payloads.append(request)
        job = {
            "id": "job",
            "status": "queued",
            "request": {**request, "query_plan": [{"query": "q"}]},
            "checkpoint": {},
            "qualified_count": 0,
            "strict_count": 0,
            "outcome": "Queued",
        }
        return job

    def get_job(self, job_id):
        return {
            "id": job_id,
            "status": "waiting_verification",
            "request": {"query_plan": []},
            "checkpoint": {},
            "qualified_count": 0,
            "strict_count": 0,
            "outcome": "Waiting",
        }

    def check_verification(self, job_id):
        return self.get_job(job_id)

    def results(self, *_args):
        return []

    def events(self, *_args):
        return []

    def resume(self, job_id):
        return {"id": job_id, "status": "queued"}

    def cancel(self, job_id):
        return {"id": job_id, "status": "cancelled"}

    def pause(self, job_id):
        return {"id": job_id, "status": "paused"}


class StreamlitAppTests(unittest.TestCase):
    def app(self):
        self.gateway = _FakeGateway()
        with patch("speedy_scraper.gateway.LocalGateway", return_value=self.gateway):
            return AppTest.from_file("app.py", default_timeout=30).run()

    def test_talent_acquisition_bangalore_preview_scales_with_target(self):
        app = self.app()
        app.multiselect[4].set_value(["6 - Bangalore"])
        app.multiselect[5].set_value(["22 - Talent Acquisition Leadership"])
        app.selectbox[0].set_value("B2B only")
        app.run()

        self.assertEqual(list(app.exception), [])
        discovery = next(
            area for area in app.text_area
            if area.label == "Discovery Queries (one per line)"
        )
        queries = discovery.value.splitlines()
        self.assertEqual(len(queries), 5)
        self.assertEqual(
            queries[0],
            (
                'site:linkedin.com/in '
                '("Head of Talent Acquisition" OR '
                '"Director of Talent Acquisition" OR '
                '"Talent Acquisition Director" OR "VP Talent Acquisition") '
                '("Bengaluru, Karnataka, India" OR '
                '"Bangalore, Karnataka, India" OR '
                '"Greater Bengaluru Area")'
            ),
        )
        self.assertNotIn("Bengaluru Urban", discovery.value)
        self.assertNotIn("B2B", discovery.value)
        self.assertEqual(len(set(queries)), len(queries))

        target = next(
            number for number in app.number_input
            if number.label == "Target Net-New POCs"
        )
        target.set_value(50)
        app.run()
        discovery = next(
            area for area in app.text_area
            if area.label == "Discovery Queries (one per line)"
        )
        self.assertEqual(len(discovery.value.splitlines()), 16)

    def test_discovery_text_replaces_stale_query_when_filters_change(self):
        app = self.app()
        app.multiselect[4].set_value(["6 - Bangalore"])
        app.multiselect[5].set_value(["22 - Talent Acquisition Leadership"])
        app.run()

        discovery = next(
            area for area in app.text_area
            if area.label == "Discovery Queries (one per line)"
        )
        self.assertIn("Talent Acquisition", discovery.value)
        self.assertIn("Bengaluru", discovery.value)

        app.multiselect[4].set_value(["1 - Delhi NCR"])
        app.multiselect[5].set_value(["5 - CMO"])
        app.run()
        discovery = next(
            area for area in app.text_area
            if area.label == "Discovery Queries (one per line)"
        )
        self.assertIn("CMO", discovery.value)
        self.assertIn("Delhi", discovery.value)
        self.assertNotIn("Talent Acquisition", discovery.value)
        self.assertNotIn("Bengaluru", discovery.value)

    def test_selecting_every_industry_means_any_industry(self):
        app = self.app()
        app.multiselect[4].set_value(["6 - Bangalore"])
        app.multiselect[5].set_value(["22 - Talent Acquisition Leadership"])
        industry_options = [
            f"{key} - {INDUSTRY_LABELS.get(key, values[0])}"
            for key, values in INDUSTRIES.items()
        ]
        app.multiselect[6].set_value(industry_options)
        app.run()

        self.assertEqual(list(app.exception), [])
        self.assertTrue(any(
            "All industries selected = Any industry" in warning.value
            for warning in app.warning
        ))
        self.assertTrue(any(
            "Industry: Any" in info.value
            for info in app.info
        ))
        discovery = next(
            area for area in app.text_area
            if area.label == "Discovery Queries (one per line)"
        )
        self.assertNotIn("Information Technology", discovery.value)
        self.assertNotIn("Banking", discovery.value)

    def test_lead_ui_is_ddgs_only(self):
        app = self.app()

        self.assertEqual(list(app.exception), [])
        self.assertNotIn("Start Google Lead Harvest", [button.label for button in app.button])
        self.assertIn("Start Lead Harvest", [button.label for button in app.button])
        self.assertNotIn("Engine", [selectbox.label for selectbox in app.selectbox])
        self.assertFalse(any(
            "Google (slower" in str(option)
            for selectbox in app.selectbox
            for option in getattr(selectbox, "options", [])
        ))

    def test_customer_success_role_is_available(self):
        app = self.app()

        self.assertEqual(list(app.exception), [])
        role_options = app.multiselect[5].options
        self.assertIn("23 - Customer Success / CX Leadership", role_options)

    def test_start_harvest_submits_ddgs_providers(self):
        _FakeGateway.created_payloads = []
        app = self.app()
        app.multiselect[4].set_value(["1 - Delhi NCR"])
        app.multiselect[5].set_value(["5 - CMO"])
        app.run()
        start = next(button for button in app.button if button.label == "Start Lead Harvest")
        start.click()
        app.run()

        self.assertEqual(list(app.exception), [])
        self.assertTrue(_FakeGateway.created_payloads)
        payload = _FakeGateway.created_payloads[-1]
        self.assertEqual(payload["discovery_provider"], "ddgs")
        self.assertEqual(payload["validation_provider"], "ddgs")

    def test_old_query_state_is_cleared_while_leads_and_four_columns_remain(self):
        app = self.app()
        app.session_state["tab1_leads"] = [{
            "Full_Name": "Jane Doe",
            "Designation": "CMO",
            "Company": "Acme",
            "LinkedIn_URL": "https://www.linkedin.com/in/jane-doe/",
            "Location_Verified": "Confirmed",
            "Location_Evidence": "Delhi",
        }]
        app.session_state["harvest_query_plan"] = [{
            "query": "old query",
            "bucket": "recovery_role_location",
        }]
        app.session_state["harvest_query_idx"] = 91
        app.session_state["harvest_query_strategy_version"] = (
            QUERY_STRATEGY_VERSION - 1
        )
        app.session_state["is_harvesting"] = True
        app.session_state["harvest_company_evidence_cache"] = {"old": {}}
        app.session_state["harvest_person_evidence_cache"] = {"old": {}}
        app.run()

        self.assertEqual(list(app.exception), [])
        self.assertEqual(
            app.session_state["harvest_query_strategy_version"],
            QUERY_STRATEGY_VERSION,
        )
        self.assertEqual(app.session_state["harvest_query_plan"], [])
        self.assertEqual(app.session_state["harvest_query_idx"], 0)
        self.assertFalse(app.session_state["is_harvesting"])
        self.assertEqual(app.session_state["harvest_company_evidence_cache"], {})
        self.assertEqual(app.session_state["harvest_person_evidence_cache"], {})
        self.assertEqual(
            [lead["Full_Name"] for lead in app.session_state["tab1_leads"]],
            ["Jane Doe"],
        )
        self.assertTrue(any(
            "obsolete discovery plan was stopped and cleared" in warning.value
            for warning in app.warning
        ))
        four_field_tables = [
            frame.value for frame in app.dataframe
            if list(frame.value.columns)
            == ["Name", "Designation", "Company", "Location"]
        ]
        self.assertGreaterEqual(len(four_field_tables), 1)
        strict_table = next(
            table for table in four_field_tables if len(table) == 1
        )
        self.assertEqual(strict_table.iloc[0].to_dict(), {
            "Name": "Jane Doe",
            "Designation": "CMO",
            "Company": "Acme",
            "Location": "Delhi",
        })

    def test_legacy_google_verification_uses_compact_auto_polling_notice(self):
        app = self.app()
        app.session_state["harvest_security_check"] = {
            "engine": "Google",
            "query": 'site:linkedin.com/in "CMO" "Delhi"',
            "phase": "discovery",
            "page": 1,
            "linkedin_only": True,
            "max_results": 10,
        }
        app.session_state["harvest_google_browser"] = {}
        app.session_state["is_harvesting"] = False
        app.run()

        self.assertEqual(list(app.exception), [])
        self.assertTrue(any(
            "disappears automatically" in warning.value
            for warning in app.warning
        ))
        labels = [button.label for button in app.button]
        self.assertNotIn("I solved it — resume same query", labels)
        self.assertIn("Cancel verification and keep found leads", labels)

    def test_ddgs_provider_block_shows_retry_recovery(self):
        app = self.app()
        app.session_state["harvest_security_check"] = {
            "engine": "DuckDuckGo",
            "query": 'site:linkedin.com/in "CMO" "Delhi"',
            "phase": "discovery",
            "page": 1,
            "retry_at": "2026-08-05T07:00:00+00:00",
        }
        app.session_state["active_job_id"] = "job"
        app.session_state["is_harvesting"] = False
        app.run()

        self.assertEqual(list(app.exception), [])
        self.assertTrue(any(
            "DuckDuckGo temporarily blocked" in warning.value
            for warning in app.warning
        ))
        labels = [button.label for button in app.button]
        self.assertIn("Retry DuckDuckGo now", labels)
        self.assertIn("Cancel retry and keep found leads", labels)


if __name__ == "__main__":
    unittest.main()
