import sys
import types
import unittest
from unittest.mock import patch

import legacy.engines_streamlit as engines
from core.utils import person_identity_key


class FakeDDGS:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def text(self, _query, max_results=25, **_kwargs):
        return [
            {
                "href": "https://linkedin.com/in/jane-new-url",
                "title": "Jane Doe - CMO at Acme | LinkedIn",
                "body": "Location: Delhi",
            },
            {
                "href": "https://linkedin.com/in/bob-one",
                "title": "Bob Singh - CMO at Beta | LinkedIn",
                "body": "Location: Delhi",
            },
            {
                "href": "https://linkedin.com/in/bob-second-url",
                "title": "BOB SINGH - CMO at BETA | LinkedIn",
                "body": "Location: Delhi",
            },
            {
                "href": "https://linkedin.com/in/carol-one",
                "title": "Carol Shah - CMO at Gamma | LinkedIn",
                "body": "Location: Delhi",
            },
        ][:max_results]


class FakePagedDDGS:
    pages_called = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def text(self, _query, max_results=50, page=1, **_kwargs):
        self.pages_called.append(page)
        profiles = {
            1: ("Missing Company", "Unknown"),
            2: ("Asha Rao", "Acme"),
            3: ("Ben Shah", "Beta"),
            4: ("Cara Singh", "Gamma"),
            5: ("Dev Patel", "Delta"),
        }
        name, company = profiles[page]
        slug = name.lower().replace(" ", "-")
        return [{
            "href": f"https://linkedin.com/in/{slug}",
            "title": (
                f"{name} - Head of Talent Acquisition at {company} | LinkedIn"
                if company != "Unknown"
                else f"{name} - Head of Talent Acquisition | LinkedIn"
            ),
            "body": "Location: Bengaluru",
        }][:max_results]


class FakeComplementaryDDGS:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def text(self, _query, max_results=50, page=1, **_kwargs):
        if page == 1:
            result = {
                "href": "https://linkedin.com/in/neha-one",
                "title": "Neha Rao - Head of Talent Acquisition | LinkedIn",
                "body": "Location: Bengaluru",
            }
        else:
            result = {
                "href": "https://linkedin.com/in/neha-one",
                "title": "Neha Rao - Acme | LinkedIn",
                "body": "",
            }
        return [result][:max_results]


class FakeSessionState(dict):
    def __getattr__(self, name):
        return self[name]


class HarvestDeduplicationTests(unittest.TestCase):
    def test_duplicates_do_not_consume_requested_poc_count(self):
        fake_ddgs_module = types.SimpleNamespace(DDGS=FakeDDGS)
        fake_streamlit = types.SimpleNamespace(
            session_state=FakeSessionState(tab1_leads=[])
        )
        existing_people = {person_identity_key("Jane Doe", "Acme")}
        query_plan = [{"query": "q", "bucket": "test"}]

        with patch.dict(sys.modules, {"ddgs": fake_ddgs_module}), \
                patch.object(engines, "st", fake_streamlit), \
                patch.object(engines.time, "sleep", return_value=None):
            leads, _, done = engines.harvest_query_batch(
                ["Delhi"], ["CMO"], [], [], "",
                query_plan, 0, 1, set(), types.SimpleNamespace(write=lambda *_: None),
                max_new_leads=2, existing_people=existing_people,
            )

        self.assertTrue(done)
        self.assertEqual([lead["Full_Name"] for lead in leads], ["Bob Singh", "Carol Shah"])
        self.assertEqual(len(fake_streamlit.session_state["tab1_leads"]), 2)

    def test_pagination_continues_until_export_ready_target(self):
        FakePagedDDGS.pages_called = []
        fake_ddgs_module = types.SimpleNamespace(DDGS=FakePagedDDGS)
        fake_streamlit = types.SimpleNamespace(
            session_state=FakeSessionState(tab1_leads=[])
        )
        query_plan = [{
            "query": 'site:linkedin.com/in "Head of Talent Acquisition" Bengaluru',
            "bucket": "discovery",
        }]

        with patch.dict(sys.modules, {"ddgs": fake_ddgs_module}), \
                patch.object(engines, "st", fake_streamlit), \
                patch.object(engines.time, "sleep", return_value=None):
            leads, _, done = engines.harvest_query_batch(
                ["Bengaluru"], ["Head of Talent Acquisition"], [], [], "",
                query_plan, 0, 1, set(),
                types.SimpleNamespace(write=lambda *_: None),
                max_new_leads=2, existing_people=set(),
            )

        self.assertTrue(done)
        self.assertEqual(
            [lead["Full_Name"] for lead in leads],
            ["Asha Rao"],
        )
        self.assertEqual(FakePagedDDGS.pages_called, [1, 2])

    def test_complementary_snippets_are_merged_by_profile_url(self):
        fake_ddgs_module = types.SimpleNamespace(DDGS=FakeComplementaryDDGS)
        fake_streamlit = types.SimpleNamespace(
            session_state=FakeSessionState(tab1_leads=[])
        )
        partial_profiles = {}
        query_plan = [{
            "query": 'site:linkedin.com/in "Head of Talent Acquisition" Bengaluru',
            "bucket": "discovery",
        }]

        with patch.dict(sys.modules, {"ddgs": fake_ddgs_module}), \
                patch.object(engines, "st", fake_streamlit), \
                patch.object(engines.time, "sleep", return_value=None):
            leads, _, done = engines.harvest_query_batch(
                ["Bengaluru"], ["Head of Talent Acquisition"], [], [], "",
                query_plan, 0, 1, set(),
                types.SimpleNamespace(write=lambda *_: None),
                max_new_leads=1, existing_people=set(),
                partial_profiles=partial_profiles,
            )

        self.assertTrue(done)
        self.assertEqual(len(leads), 1)
        self.assertEqual(leads[0]["Full_Name"], "Neha Rao")
        self.assertEqual(leads[0]["Company"], "Acme")
        self.assertEqual(partial_profiles, {})


if __name__ == "__main__":
    unittest.main()
