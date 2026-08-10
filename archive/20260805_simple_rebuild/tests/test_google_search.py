import types
import unittest
from pathlib import Path
from unittest.mock import patch

import core.google_search as google_search
import legacy.engines_streamlit as engines
from core.google_search import (
    GoogleSecurityCheck,
    _expected_results_page,
    _google_next_page_number,
    _google_search_params,
    _is_security_check,
    _normalize_google_items,
    google_security_check_resolved,
    google_text_search,
)


class FakeSessionState(dict):
    def __getattr__(self, name):
        return self[name]


class GoogleSearchTests(unittest.TestCase):
    def test_google_citation_cards_are_normalized_and_deduplicated(self):
        items = [
            {
                "href": (
                    "https://www.google.com/url?"
                    "q=https%3A%2F%2Fwww.linkedin.com%2Fin%2Fjane-doe%2F"
                ),
                "title": " Jane Doe - CMO at Acme | LinkedIn ",
                "body": " Location: Delhi ",
            },
            {
                "href": "https://www.linkedin.com/in/jane-doe/",
                "title": "Jane duplicate",
                "body": "Location: Delhi",
            },
            {
                "href": "https://example.com/not-a-profile",
                "title": "Ignore me",
                "body": "",
            },
        ]

        results = _normalize_google_items(items, 10)

        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0]["href"],
            "https://www.linkedin.com/in/jane-doe/",
        )
        self.assertEqual(results[0]["body"], "Location: Delhi")

    def test_fake_linkedin_substring_on_external_host_is_rejected(self):
        items = [{
            "href": (
                "https://example.com/redirect?"
                "next=https://www.linkedin.com/in/jane-doe/"
            ),
            "title": "Not a LinkedIn profile",
            "body": "Irrelevant result",
        }]

        self.assertEqual(_normalize_google_items(items, 10), [])

    def test_saved_google_challenge_page_is_detected(self):
        body = Path("tests/fixtures/debug_google.html").read_text(errors="ignore")
        self.assertTrue(_is_security_check(
            "https://www.google.com/sorry/index", "", body,
        ))

    def test_google_pagination_uses_nonoverlapping_ten_result_offsets(self):
        self.assertEqual(_google_search_params("q", 1, 50)["start"], 0)
        self.assertEqual(_google_search_params("q", 2, 50)["start"], 10)
        self.assertEqual(_google_search_params("q", 5, 50)["start"], 40)
        self.assertEqual(_google_search_params("q", 2, 50)["num"], 10)
        self.assertEqual(_google_search_params("q", 1, 10)["tbs"], "li:1")
        self.assertEqual(_google_search_params("q", 1, 10)["nfpr"], "1")

    def test_google_next_link_drives_the_following_page(self):
        class FakeNext:
            first = None

            def __init__(self):
                self.first = self

            def count(self):
                return 1

            def get_attribute(self, name):
                self.assert_name = name
                return "/search?q=profiles&start=20"

        class FakePage:
            def locator(self, selector):
                return FakeNext() if selector == "a#pnnext" else None

        self.assertEqual(_google_next_page_number(FakePage(), 2), 3)

    def test_solved_page_must_match_query_and_offset(self):
        query = 'site:linkedin.com/in "CMO" "Delhi"'
        encoded = "site%3Alinkedin.com%2Fin+%22CMO%22+%22Delhi%22"
        self.assertTrue(_expected_results_page(
            f"https://www.google.com/search?q={encoded}&start=10",
            query,
            2,
        ))
        self.assertFalse(_expected_results_page(
            f"https://www.google.com/search?q={encoded}&start=0",
            query,
            2,
        ))
        self.assertFalse(_expected_results_page(
            "https://www.google.com/search?q=another+query&start=10",
            query,
            2,
        ))

    def test_solved_captcha_results_are_consumed_without_navigation(self):
        query = 'site:linkedin.com/in "CMO" "Delhi"'
        expected = [{
            "href": "https://www.linkedin.com/in/jane-doe/",
            "title": "Jane Doe - CMO at Acme",
            "body": "Location: Delhi",
        }]
        browser_state = {
            "resume_result": {
                "query": query,
                "page": 2,
                "linkedin_only": True,
                "results": expected,
            },
        }

        with patch.object(
            google_search,
            "_ensure_browser",
            side_effect=AssertionError("must not navigate after CAPTCHA"),
        ):
            results = google_text_search(
                query,
                page=2,
                max_results=10,
                browser_state=browser_state,
            )

        self.assertEqual(results, expected)
        self.assertNotIn("resume_result", browser_state)

    def test_security_state_records_exact_page_and_result_mode(self):
        check = GoogleSecurityCheck(
            "company evidence",
            "https://www.google.com/sorry/",
            page=2,
            linkedin_only=False,
            max_results=10,
        ).as_dict()

        self.assertEqual(check["page"], 2)
        self.assertFalse(check["linkedin_only"])
        self.assertEqual(check["max_results"], 10)

        later_check = GoogleSecurityCheck(
            "same discovery", "https://www.google.com/sorry/", page=7,
        ).as_dict()
        self.assertEqual(later_check["page"], 7)

    def test_manual_solution_is_stable_before_it_is_cached(self):
        query = 'site:linkedin.com/in "CMO" "Delhi"'
        encoded = "site%3Alinkedin.com%2Fin+%22CMO%22+%22Delhi%22"
        result = {
            "href": "https://www.linkedin.com/in/jane-doe/",
            "title": "Jane Doe - CMO at Acme",
            "body": "Location: Delhi",
        }

        class FakeLocator:
            def inner_text(self, timeout=None):
                return "Google results"

        class FakePage:
            url = f"https://www.google.com/search?q={encoded}&start=0"

            def title(self):
                return "Google Search"

            def locator(self, _):
                return FakeLocator()

            def evaluate(self, script):
                return "complete" if script == "document.readyState" else [result]

        fake_browser = types.SimpleNamespace(
            contexts=[types.SimpleNamespace(pages=[FakePage()])]
        )
        fake_playwright = types.SimpleNamespace(
            chromium=types.SimpleNamespace(
                connect_over_cdp=lambda *_, **__: fake_browser
            )
        )

        class FakeManager:
            def __enter__(self):
                return fake_playwright

            def __exit__(self, *_):
                return False

        browser_state = {"port": 9222}
        check = {
            "query": query,
            "page": 1,
            "linkedin_only": True,
            "max_results": 10,
        }
        with patch.object(google_search, "_cdp_ready", return_value=True), \
                patch.object(
                    google_search,
                    "sync_playwright",
                    return_value=FakeManager(),
                ):
            self.assertFalse(
                google_security_check_resolved(browser_state, check)
            )
            self.assertTrue(
                google_security_check_resolved(browser_state, check)
            )

        self.assertEqual(
            browser_state["resume_result"]["results"],
            [result],
        )

    def test_google_provider_feeds_the_existing_strict_lead_pipeline(self):
        fake_streamlit = types.SimpleNamespace(
            session_state=FakeSessionState(tab1_leads=[])
        )
        google_result = [{
            "href": "https://www.linkedin.com/in/jane-doe/",
            "title": "Jane Doe - CMO at Acme | LinkedIn",
            "body": "Location: Delhi",
        }]
        query_plan = [{"query": 'site:linkedin.com/in "CMO" "Delhi"', "bucket": "custom"}]

        with patch.object(engines, "st", fake_streamlit), \
                patch.object(engines, "google_text_search", return_value=google_result), \
                patch.object(engines.time, "sleep", return_value=None):
            leads, next_idx, done = engines.harvest_query_batch(
                ["Delhi"], ["CMO"], [], [], "",
                query_plan, 0, 1, set(),
                types.SimpleNamespace(write=lambda *_: None),
                max_new_leads=1,
                search_provider="google",
                browser_state={},
            )

        self.assertTrue(done)
        self.assertEqual(next_idx, 1)
        self.assertEqual([lead["Full_Name"] for lead in leads], ["Jane Doe"])

    def test_google_security_check_bubbles_without_advancing_query(self):
        fake_streamlit = types.SimpleNamespace(
            session_state=FakeSessionState(tab1_leads=[])
        )
        query_plan = [{"query": "same query", "bucket": "custom"}]

        with patch.object(engines, "st", fake_streamlit), \
                patch.object(
                    engines,
                    "google_text_search",
                    side_effect=GoogleSecurityCheck(
                        "same query", "https://google.com/sorry/"
                    ),
                ):
            with self.assertRaises(GoogleSecurityCheck) as raised:
                engines.harvest_query_batch(
                    ["Delhi"], ["CMO"], [], [], "",
                    query_plan, 0, 1, set(),
                    types.SimpleNamespace(write=lambda *_: None),
                    search_provider="google",
                    browser_state={},
                )

        self.assertEqual(raised.exception.query, "same query")
        self.assertEqual(fake_streamlit.session_state["tab1_leads"], [])

    def test_zero_result_primary_relaxes_once_without_opening_page_two(self):
        fake_streamlit = types.SimpleNamespace(
            session_state=FakeSessionState(tab1_leads=[])
        )
        query_plan = [{
            "query": "primary query",
            "fallback_query": "fallback query",
            "bucket": "discovery",
        }]

        with patch.object(engines, "st", fake_streamlit), \
                patch.object(
                    engines, "google_text_search", return_value=[],
                ) as search:
            leads, next_idx, done = engines.harvest_query_batch(
                ["Delhi"], ["CMO"], [], [], "",
                query_plan, 0, 1, set(),
                types.SimpleNamespace(write=lambda *_: None),
                search_provider="google",
                browser_state={},
            )

        self.assertEqual(leads, [])
        self.assertEqual(next_idx, 1)
        self.assertTrue(done)
        self.assertEqual(
            [(call.args[0], call.kwargs["page"]) for call in search.call_args_list],
            [("primary query", 1), ("fallback query", 1)],
        )

    def test_nonempty_low_yield_primary_follows_pages_then_fallback_once(self):
        fake_streamlit = types.SimpleNamespace(
            session_state=FakeSessionState(tab1_leads=[])
        )
        foreign_result = [{
            "href": "https://www.linkedin.com/in/foreign-profile/",
            "title": "Foreign Person - Head of Talent Acquisition at Foreign Co",
            "body": (
                "New York City Metropolitan Area · Head of Talent Acquisition · "
                "Foreign Co"
            ),
        }]
        browser_state = {}

        def fake_google(query, **kwargs):
            current_page = kwargs["page"]
            next_pages = {1: 2, 2: 3, 3: None}
            browser_state["last_search_next_page"] = next_pages[current_page]
            page_result = dict(foreign_result[0])
            page_result["href"] = (
                f"https://www.linkedin.com/in/foreign-profile-{current_page}/"
            )
            page_result["title"] = (
                f"Foreign Person {current_page} - "
                "Head of Talent Acquisition at Foreign Co"
            )
            return [page_result]

        with patch.object(engines, "st", fake_streamlit), \
                patch.object(
                    engines, "google_text_search", side_effect=fake_google,
                ) as search, \
                patch.object(engines.time, "sleep", return_value=None):
            leads, _, _ = engines.harvest_query_batch(
                ["Bengaluru", "Bangalore"],
                ["Head of Talent Acquisition"],
                [], [], "",
                [{
                    "query": "primary query",
                    "fallback_query": "fallback query",
                    "bucket": "discovery",
                }],
                0, 1, set(), types.SimpleNamespace(write=lambda *_: None),
                max_new_leads=2,
                search_provider="google",
                browser_state=browser_state,
            )

        self.assertEqual(leads, [])
        self.assertEqual(
            [(call.args[0], call.kwargs["page"]) for call in search.call_args_list],
            [
                ("primary query", 1),
                ("primary query", 2),
                ("primary query", 3),
                ("fallback query", 1),
            ],
        )

    def test_repeated_google_result_page_stops_without_looping(self):
        fake_streamlit = types.SimpleNamespace(
            session_state=FakeSessionState(tab1_leads=[])
        )
        repeated_result = [{
            "href": "https://www.linkedin.com/in/repeated-profile/",
            "title": "Repeated Person - CMO at Acme",
            "body": "Location: New York",
        }]
        browser_state = {}

        def fake_google(query, **kwargs):
            browser_state["last_search_next_page"] = kwargs["page"] + 1
            return repeated_result

        with patch.object(engines, "st", fake_streamlit), \
                patch.object(
                    engines, "google_text_search", side_effect=fake_google,
                ) as search, \
                patch.object(engines.time, "sleep", return_value=None):
            leads, _, done = engines.harvest_query_batch(
                ["Delhi"], ["CMO"], [], [], "",
                [{"query": "primary", "fallback_query": "", "bucket": "discovery"}],
                0, 1, set(), types.SimpleNamespace(write=lambda *_: None),
                search_provider="google",
                browser_state=browser_state,
            )

        self.assertEqual(leads, [])
        self.assertTrue(done)
        self.assertEqual(
            [call.kwargs["page"] for call in search.call_args_list],
            [1, 2],
        )

    def test_company_validation_is_cached_for_candidates_at_same_company(self):
        fake_streamlit = types.SimpleNamespace(
            session_state=FakeSessionState(tab1_leads=[])
        )
        discovery_results = [
            {
                "href": "https://www.linkedin.com/in/jane-doe/",
                "title": "Jane Doe - CMO at Acme | LinkedIn",
                "body": "Location: Delhi",
            },
            {
                "href": "https://www.linkedin.com/in/john-doe/",
                "title": "John Doe - CMO at Acme | LinkedIn",
                "body": "Location: Delhi",
            },
        ]

        def fake_google(query, **kwargs):
            if kwargs.get("linkedin_only", True):
                return discovery_results
            return [{
                "href": "https://acme.example/about",
                "title": "Acme enterprise platform",
                "body": "B2B software serving business customers.",
            }]

        company_cache = {}
        with patch.object(engines, "st", fake_streamlit), \
                patch.object(engines, "google_text_search", side_effect=fake_google), \
                patch.object(engines.time, "sleep", return_value=None):
            leads, _, _ = engines.harvest_query_batch(
                ["Delhi"], ["CMO"], [], [], "",
                [{
                    "query": "profile discovery",
                    "fallback_query": "",
                    "bucket": "discovery",
                }],
                0, 1, set(), types.SimpleNamespace(write=lambda *_: None),
                max_new_leads=2,
                business_model="B2B only",
                search_provider="google",
                browser_state={},
                company_evidence_cache=company_cache,
            )

        self.assertEqual(len(leads), 2)
        self.assertEqual(len(company_cache), 1)
        self.assertTrue(all(
            lead["Business_Model_Verified"] == "Confirmed" for lead in leads
        ))

    def test_qualified_poc_target_stops_even_when_strict_validation_fails(self):
        fake_streamlit = types.SimpleNamespace(
            session_state=FakeSessionState(
                tab1_leads=[],
                tab1_all_pocs=[],
            )
        )
        discovery_results = [
            {
                "href": "https://www.linkedin.com/in/jane-doe/",
                "title": "Jane Doe - CMO at Acme | LinkedIn",
                "body": "Location: Delhi",
            },
            {
                "href": "https://www.linkedin.com/in/john-doe/",
                "title": "John Doe - CMO at Beta | LinkedIn",
                "body": "Location: Delhi",
            },
        ]

        def fake_google(query, **kwargs):
            if kwargs.get("linkedin_only", True):
                return discovery_results
            return [{
                "href": "https://example.com/about",
                "title": "Consumer brand",
                "body": "Direct-to-consumer retail products.",
            }]

        with patch.object(engines, "st", fake_streamlit), \
                patch.object(
                    engines, "google_text_search", side_effect=fake_google,
                ), \
                patch.object(engines.time, "sleep", return_value=None):
            leads, _, done = engines.harvest_query_batch(
                ["Delhi"], ["CMO"], [], [], "",
                [{
                    "query": "profile discovery",
                    "fallback_query": "",
                    "bucket": "discovery",
                }],
                0, 1, set(), types.SimpleNamespace(write=lambda *_: None),
                max_new_pocs=1,
                business_model="B2B only",
                search_provider="google",
                browser_state={},
            )

        self.assertEqual(leads, [])
        self.assertTrue(done)
        self.assertEqual(
            [
                poc["Full_Name"]
                for poc in fake_streamlit.session_state["tab1_all_pocs"]
            ],
            ["Jane Doe"],
        )

    def test_unattributed_person_signal_evidence_is_rejected_and_cached(self):
        fake_streamlit = types.SimpleNamespace(
            session_state=FakeSessionState(tab1_leads=[])
        )
        discovery_result = [{
            "href": "https://www.linkedin.com/in/jane-doe/",
            "title": "Jane Doe - CMO at Acme | LinkedIn",
            "body": "Location: Delhi",
        }]

        def fake_google(query, **kwargs):
            if kwargs.get("linkedin_only", True):
                return discovery_result
            return [{
                "href": "https://events.example/speakers",
                "title": "Jane Doe was a keynote speaker at OtherCo Summit",
                "body": "Jane Doe joined the conference keynote.",
            }]

        person_cache = {}
        with patch.object(engines, "st", fake_streamlit), \
                patch.object(
                    engines, "google_text_search", side_effect=fake_google,
                ) as search, \
                patch.object(engines.time, "sleep", return_value=None):
            leads, _, _ = engines.harvest_query_batch(
                ["Delhi"], ["CMO"], [], ["keynote speaker"], "",
                [{"query": "profile discovery", "bucket": "discovery"}],
                0, 1, set(), types.SimpleNamespace(write=lambda *_: None),
                search_provider="google",
                browser_state={},
                person_evidence_cache=person_cache,
            )

        self.assertEqual(leads, [])
        self.assertEqual(
            [
                poc["Full_Name"]
                for poc in fake_streamlit.session_state["tab1_all_pocs"]
            ],
            ["Jane Doe"],
        )
        self.assertEqual(len(person_cache), 1)
        self.assertEqual(next(iter(person_cache.values()))["text"], "")
        validation_calls = [
            call for call in search.call_args_list
            if call.kwargs.get("linkedin_only") is False
        ]
        self.assertEqual(len(validation_calls), 1)

    def test_validation_captcha_preserves_prior_leads_cache_and_phase(self):
        fake_streamlit = types.SimpleNamespace(
            session_state=FakeSessionState(tab1_leads=[])
        )
        discovery_results = [
            {
                "href": "https://www.linkedin.com/in/jane-doe/",
                "title": "Jane Doe - CMO at Acme | LinkedIn",
                "body": "Location: Delhi",
            },
            {
                "href": "https://www.linkedin.com/in/john-doe/",
                "title": "John Doe - CMO at Beta | LinkedIn",
                "body": "Location: Delhi",
            },
        ]

        resolved = {"value": False}
        discovery_calls = {"count": 0}

        def fake_google(query, **kwargs):
            if kwargs.get("linkedin_only", True):
                discovery_calls["count"] += 1
                return discovery_results
            if "Acme" in query:
                return [{
                    "href": "https://acme.example/about",
                    "title": "Acme enterprise platform",
                    "body": "B2B software serving business customers.",
                }]
            if resolved["value"]:
                return [{
                    "href": "https://beta.example/about",
                    "title": "Beta enterprise platform",
                    "body": "Beta is a B2B company serving business customers.",
                }]
            raise GoogleSecurityCheck(
                query,
                "https://www.google.com/sorry/",
            )

        company_cache = {}
        existing_urls = set()
        browser_state = {}
        with patch.object(engines, "st", fake_streamlit), \
                patch.object(engines, "google_text_search", side_effect=fake_google), \
                patch.object(engines.time, "sleep", return_value=None):
            with self.assertRaises(GoogleSecurityCheck) as raised:
                engines.harvest_query_batch(
                    ["Delhi"], ["CMO"], [], [], "",
                    [{"query": "profile discovery", "bucket": "discovery"}],
                    0, 1, existing_urls,
                    types.SimpleNamespace(write=lambda *_: None),
                    max_new_leads=2,
                    max_new_pocs=2,
                    business_model="B2B only",
                    search_provider="google",
                    browser_state=browser_state,
                    company_evidence_cache=company_cache,
                )

            prior_names = [
                lead["Full_Name"]
                for lead in fake_streamlit.session_state["tab1_leads"]
            ]
            resolved["value"] = True
            browser_state["resume_result"] = {
                "linkedin_only": False,
            }
            resumed_leads, _, done = engines.harvest_query_batch(
                ["Delhi"], ["CMO"], [], [], "",
                [{"query": "profile discovery", "bucket": "discovery"}],
                0, 1, existing_urls,
                types.SimpleNamespace(write=lambda *_: None),
                max_new_leads=2,
                max_new_pocs=0,
                business_model="B2B only",
                search_provider="google",
                browser_state=browser_state,
                company_evidence_cache=company_cache,
            )

        self.assertEqual(raised.exception.phase, "company validation")
        self.assertEqual(prior_names, ["Jane Doe"])
        self.assertEqual(
            [
                lead["Full_Name"]
                for lead in fake_streamlit.session_state["tab1_leads"]
            ],
            ["Jane Doe", "John Doe"],
        )
        self.assertEqual(len(company_cache), 2)
        self.assertIn("https://www.linkedin.com/in/jane-doe/", existing_urls)
        self.assertEqual(discovery_calls["count"], 1)
        self.assertTrue(done)
        self.assertEqual(
            [lead["Full_Name"] for lead in resumed_leads],
            ["John Doe"],
        )


if __name__ == "__main__":
    unittest.main()
