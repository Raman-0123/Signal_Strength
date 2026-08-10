import unittest

from core.query_builder import (
    MAX_DISCOVERY_QUERIES,
    build_company_validation_query,
    build_query_plan,
    ensure_query_filters,
    query_budget_for,
)
from core.utils import (
    any_term_matches,
    check_location_in_snippet,
    check_role_in_title,
    company_name_variants,
    is_export_ready_profile,
    normalize_linkedin_url,
    person_identity_key,
)
from lead_generator_cli import INDUSTRIES, INDUSTRY_LABELS, LOCATIONS, ROLES
from legacy.engines_streamlit import _evaluate_candidate


class FilteringTests(unittest.TestCase):
    def setUp(self):
        self.parameters = {
            "all_locs": ["Delhi", "Gurgaon"],
            "all_roles": ["CMO", "Marketing Head"],
            "all_inds": ["FinTech"],
            "all_sigs": ["keynote speaker"],
            "custom_terms": ["Series A"],
            "organization_terms": ["Acme Corp", "Beta Labs"],
        }

    def evaluate(self, title, body):
        return _evaluate_candidate(title, body, "", **self.parameters)

    def test_categories_are_and_values_inside_category_are_or(self):
        accepted, hits, matches, _ = self.evaluate(
            "Jane Doe - Chief Marketing Officer at Acme Corp - LinkedIn",
            "Delhi · FinTech · Series A · keynote speaker",
        )
        self.assertTrue(accepted)
        self.assertTrue(all(hits.values()))
        self.assertEqual(matches["Role"], ["CMO"])
        self.assertEqual(matches["Organisation"], ["Acme Corp"])

    def test_wrong_organisation_is_rejected_even_if_custom_matches(self):
        accepted, hits, _, _ = self.evaluate(
            "Jane Doe - Chief Marketing Officer at Wrong Company - LinkedIn",
            "Delhi · FinTech · Series A · keynote speaker",
        )
        self.assertFalse(accepted)
        self.assertFalse(hits["organization"])
        self.assertTrue(hits["custom"])

    def test_historical_snippet_cannot_override_current_company_or_role(self):
        accepted, hits, matches, _ = _evaluate_candidate(
            "Jane Doe - Sales Manager at Wrong Company - LinkedIn",
            "Delhi · Former Chief Marketing Officer at Acme Corp · FinTech",
            "",
            ["Delhi"], ["CMO"], ["FinTech"], [], [], ["Acme Corp"],
            evidence_policy="search",
            current_designation="Sales Manager",
            current_company="Wrong Company",
        )

        self.assertFalse(accepted)
        self.assertFalse(hits["role"])
        self.assertFalse(hits["organization"])
        self.assertEqual(matches["Role"], [])
        self.assertEqual(matches["Organisation"], [])

    def test_missing_selected_industry_is_rejected(self):
        accepted, hits, _, _ = self.evaluate(
            "Jane Doe - Chief Marketing Officer at Acme Corp - LinkedIn",
            "Delhi · Series A · keynote speaker",
        )
        self.assertFalse(accepted)
        self.assertFalse(hits["industry"])

    def test_discovery_query_keeps_fit_filters_out_of_google_profile_search(self):
        plan = build_query_plan(
            self.parameters["all_locs"], self.parameters["all_roles"],
            self.parameters["all_inds"], self.parameters["all_sigs"],
            "Series A", max_queries=10, organization_kws="Acme Corp, Beta Labs",
        )
        self.assertEqual(plan[0]["bucket"], "discovery")
        self.assertIn("CMO", plan[0]["query"])
        self.assertIn("Delhi", plan[0]["query"])
        for value in ["Acme Corp", "Beta Labs", "FinTech", "keynote speaker", "Series A"]:
            self.assertNotIn(value, plan[0]["query"])
        self.assertEqual(
            plan[0]["required_filters"]["business_model"],
            "Any",
        )

    def test_ddgs_context_mode_adds_industry_and_org_discovery_terms(self):
        plan = build_query_plan(
            ["Bengaluru"], ["CTO"], ["FinTech", "Payments"],
            ["keynote speaker"], "Series A", max_queries=3,
            organization_kws="Acme Corp",
            business_model="B2B only",
            include_context_terms=True,
        )
        self.assertIn("FinTech", plan[0]["query"])
        self.assertIn("Payments", plan[0]["query"])
        self.assertIn("Series A", plan[0]["query"])
        self.assertIn("Acme Corp", plan[0]["query"])
        self.assertNotIn("keynote speaker", plan[0]["query"])
        self.assertNotIn('"B2B"', plan[0]["query"])

        completed = ensure_query_filters(
            'site:linkedin.com/in "CTO" "Bengaluru"',
            ["Bengaluru"], ["CTO"], ["FinTech"], [],
            organization_kws="Acme Corp",
            include_context_terms=True,
        )
        self.assertIn("FinTech", completed)
        self.assertIn("Acme Corp", completed)

    def test_b2b_is_a_first_class_query_and_acceptance_filter(self):
        plan = build_query_plan(
            ["Delhi"], ["CMO"], [], [], "",
            max_queries=2, business_model="B2B only",
        )
        self.assertNotIn('"B2B"', plan[0]["query"])
        self.assertEqual(
            plan[0]["required_filters"]["business_model"],
            "B2B",
        )
        validation_query = build_company_validation_query(
            "Acme", business_model="B2B only",
        )
        self.assertIn('"B2B"', validation_query)

        accepted, hits, _, _ = _evaluate_candidate(
            "Jane Doe - CMO at Acme - LinkedIn",
            "Delhi · Acme builds enterprise software for business customers.",
            "", ["Delhi"], ["CMO"], [], [], [], [],
            business_model="B2B only", person_name="Jane Doe",
        )
        self.assertTrue(accepted)
        self.assertTrue(hits["business_model"])

    def test_recovery_mode_no_longer_creates_recovery_queries(self):
        plan = build_query_plan(
            ["Bengaluru"], ["Head of Talent Acquisition"], [], [], "",
            max_queries=10, business_model="B2B only",
            include_recovery=True,
        )
        self.assertTrue(plan)
        self.assertTrue(all('"B2B"' not in item["query"] for item in plan))
        self.assertFalse(any(
            item["bucket"] == "recovery_role_location" for item in plan
        ))

    def test_consumer_only_profile_is_rejected_by_b2b_filter(self):
        accepted, hits, _, _ = _evaluate_candidate(
            "Jane Doe - CMO at Acme - LinkedIn",
            "Delhi · Acme is a direct-to-consumer retail brand and consumer app.",
            "", ["Delhi"], ["CMO"], [], [], [], [],
            business_model="B2B only", person_name="Jane Doe",
        )
        self.assertFalse(accepted)
        self.assertFalse(hits["business_model"])

    def test_search_policy_rejects_sparse_snippets_without_selected_location(self):
        accepted, hits, _, _ = _evaluate_candidate(
            "Jane Doe - CTO at Acme - LinkedIn",
            "Technology leader building cloud platforms.",
            "", ["Bangalore"], ["CTO"], ["FinTech"], ["GCC Roundtable"],
            ["Series A"], [], business_model="B2B only",
            person_name="Jane Doe", evidence_policy="search",
        )
        self.assertFalse(accepted)
        self.assertTrue(hits["role"])
        self.assertFalse(hits["location"])
        self.assertFalse(hits["business_model"])

    def test_search_policy_does_not_use_discovery_snippet_for_fit_filters(self):
        accepted, hits, _, _ = _evaluate_candidate(
            "Jane Doe - CTO at Acme - LinkedIn",
            (
                "Location: Bangalore · FinTech · Series A · Jane Doe was a "
                "keynote speaker at a technology conference. Acme builds "
                "enterprise software for business customers."
            ),
            "", ["Bangalore"], ["CTO"], ["FinTech"], ["keynote speaker"],
            ["Series A"], [], business_model="B2B only",
            person_name="Jane Doe", evidence_policy="search",
        )
        self.assertFalse(accepted)
        self.assertTrue(hits["role"])
        self.assertTrue(hits["location"])
        self.assertFalse(hits["industry"])
        self.assertFalse(hits["signal"])
        self.assertFalse(hits["custom"])
        self.assertFalse(hits["business_model"])

    def test_two_stage_evidence_satisfies_company_and_person_filters(self):
        accepted, hits, _, _ = _evaluate_candidate(
            "Jane Doe - CTO at Acme - LinkedIn",
            "Location: Bangalore",
            "",
            ["Bangalore"], ["CTO"], ["FinTech"], ["keynote speaker"],
            ["Series A"], [], business_model="B2B only",
            person_name="Jane Doe", evidence_policy="search",
            current_designation="CTO", current_company="Acme",
            company_evidence=(
                "Acme is a Series A FinTech B2B enterprise software company "
                "serving business customers."
            ),
            person_evidence=(
                "Jane Doe was a keynote speaker at the Acme technology conference."
            ),
        )

        self.assertTrue(accepted)
        self.assertTrue(all(hits.values()))

    def test_discovery_plan_is_capped_and_validation_metadata_is_complete(self):
        plan = build_query_plan(
            ["Delhi"], ["CMO"], ["FinTech"], ["keynote speaker"],
            "Series A", max_queries=20, organization_kws="Acme Corp",
            business_model="B2B only", gcc_only=True,
        )

        self.assertLessEqual(len(plan), MAX_DISCOVERY_QUERIES)
        for item in plan:
            for value in ("Delhi", "CMO"):
                self.assertIn(value, item["query"])
            for value in (
                "Acme Corp", "FinTech", "keynote speaker", "Series A",
                '"B2B"', '"GCC"',
            ):
                self.assertNotIn(value, item["query"])
            self.assertEqual(item["required_filters"]["industries"], ["FinTech"])
            self.assertEqual(item["required_filters"]["signals"], ["keynote speaker"])
            self.assertEqual(item["required_filters"]["custom_terms"], ["Series A"])
            self.assertEqual(item["required_filters"]["business_model"], "B2B")
            self.assertTrue(item["required_filters"]["gcc_only"])

    def test_user_edited_query_is_completed_with_missing_filters(self):
        query = ensure_query_filters(
            'site:linkedin.com/in "CMO"',
            ["Delhi"], ["CMO"], ["FinTech"], ["keynote speaker"],
            "Series A", organization_kws="Acme Corp",
            business_model="B2B only", gcc_only=True,
        )

        for value in ("CMO", "Delhi"):
            self.assertIn(value, query)
        for value in (
            "Acme Corp", "FinTech", "keynote speaker", "Series A",
            '"B2B"', '"GCC"',
        ):
            self.assertNotIn(value, query)

    def test_talent_acquisition_bangalore_scales_to_target_with_grouped_base(self):
        budget = query_budget_for(
            50, LOCATIONS["6"], ROLES["22"], [], [],
        )
        plan = build_query_plan(
            LOCATIONS["6"], ROLES["22"], [], [], "",
            max_queries=budget,
            business_model="B2B only",
            role_groups=[ROLES["22"]],
            location_groups=[LOCATIONS["6"]],
        )

        self.assertEqual(budget, 16)
        self.assertEqual(len(plan), 16)
        self.assertEqual(len({item["query"] for item in plan}), 16)
        query = plan[0]["query"]
        for value in (
            "Head of Talent Acquisition",
            "Director of Talent Acquisition",
            "Talent Acquisition Director",
            "VP Talent Acquisition",
            "Bengaluru, Karnataka, India",
            "Bangalore, Karnataka, India",
            "Greater Bengaluru Area",
        ):
            self.assertIn(value, query)
        self.assertNotIn("Bengaluru Urban", query)
        self.assertNotIn('"B2B"', query)
        self.assertEqual(plan[0]["search_locations"], [
            "Bengaluru, Karnataka, India",
            "Bangalore, Karnataka, India",
            "Greater Bengaluru Area",
        ])
        self.assertIn("Bengaluru Urban", plan[0]["locations"])
        self.assertIn("Bangalore Urban", plan[0]["locations"])
        for item in plan:
            self.assertTrue(item["query"].startswith("site:linkedin.com/in "))
            self.assertTrue(any(
                location in item["query"]
                for location in (
                    "Bengaluru, Karnataka, India",
                    "Bangalore, Karnataka, India",
                    "Greater Bengaluru Area",
                )
            ))
            self.assertNotIn('"B2B"', item["query"])
        self.assertEqual(
            plan[0]["fallback_query"],
            (
                'site:linkedin.com/in '
                '("Talent Acquisition Head" OR '
                '"Senior Director of Talent Acquisition" OR '
                '"Global Head of Talent Acquisition" OR '
                '"Head of Recruitment") '
                '("Bengaluru, Karnataka, India" OR '
                '"Bangalore, Karnataka, India" OR '
                '"Greater Bengaluru Area")'
            ),
        )

    def test_edited_https_site_prefix_is_canonicalized(self):
        query = ensure_query_filters(
            'site:https://www.linkedin.com/in/ "Head of Talent Acquisition"',
            LOCATIONS["6"],
            ROLES["22"],
        )

        self.assertTrue(query.startswith("site:linkedin.com/in "))
        self.assertNotIn("https://", query)
        self.assertIn('"Bengaluru, Karnataka, India"', query)
        self.assertIn('"Bangalore, Karnataka, India"', query)

    def test_search_policy_rejects_unstructured_location_mentions(self):
        accepted, hits, _, _ = _evaluate_candidate(
            "Jane Doe - Head of Talent Acquisition at Acme - LinkedIn",
            "Hiring for a role in Bangalore. Current profile details unavailable.",
            "", ["Bangalore"], ["Head of Talent Acquisition"], [], [], [], [],
            evidence_policy="search", current_designation="Head of Talent Acquisition",
            current_company="Acme",
        )
        self.assertFalse(accepted)
        self.assertFalse(hits["location"])

    def test_concatenated_next_profile_location_is_rejected(self):
        accepted, hits, _, _ = _evaluate_candidate(
            "Jane Doe - Head of Talent Acquisition at Acme - LinkedIn",
            (
                "Head of Talent Acquisition. · Experience: Other Co · "
                "Location: Bengaluru · 500+ connections on LinkedIn. "
                "View Priya Singh’s profile on LinkedIn."
            ),
            "", ["Bengaluru"], ["Head of Talent Acquisition"], [], [], [], [],
            person_name="Jane Doe", evidence_policy="search",
            current_designation="Head of Talent Acquisition",
            current_company="Acme",
        )
        self.assertFalse(accepted)
        self.assertFalse(hits["location"])

    def test_ncr_company_name_does_not_confirm_delhi_ncr_location(self):
        self.assertFalse(check_location_in_snippet(
            "Vice President of Product Management at NCR Voyix.",
            "Ed Jantz - Vice President of Product Management at NCR Voyix - LinkedIn",
            "",
            ["NCR"],
        ))

    def test_structured_ncr_location_confirms_location(self):
        self.assertTrue(check_location_in_snippet(
            "Vice President · Location: NCR · 500+ connections.",
            "Jane Doe - Vice President - LinkedIn",
            "",
            ["NCR"],
        ))

    def test_google_leading_profile_location_is_current_evidence(self):
        self.assertTrue(check_location_in_snippet(
            (
                "Bengaluru, Karnataka, India · Head of Talent Acquisition · "
                "Acme Jane Doe. Head of Talent Acquisition at Acme."
            ),
            "Jane Doe - Head of Talent Acquisition",
            "",
            ["Bengaluru", "Bangalore"],
            require_current_evidence=True,
            person_name="Jane Doe",
        ))

    def test_gcc_focus_moves_to_company_validation_query(self):
        plan = build_query_plan(
            ["Bangalore"], ["CTO"], [], [], "",
            max_queries=3, gcc_only=True,
        )
        self.assertGreaterEqual(len(plan), 1)
        self.assertTrue(all(
            '"GCC"' not in item["query"] for item in plan
        ))
        self.assertTrue(all(
            item["required_filters"]["gcc_only"] for item in plan
        ))
        self.assertIn(
            '"GCC"',
            build_company_validation_query("Acme", gcc_only=True),
        )

    def test_discrete_it_bfsi_and_banking_industries_are_available(self):
        labels = set(INDUSTRY_LABELS.values())
        self.assertIn("IT / Information Technology", labels)
        self.assertIn("IT Services", labels)
        self.assertIn("BFSI", labels)
        self.assertIn("Banking", labels)
        self.assertIn("Insurance / InsurTech", labels)
        self.assertIn("NBFC / Lending", labels)
        self.assertIn("GCC / GIC / Captive Centre", labels)
        self.assertIn("BFSI", INDUSTRIES["6"])
        self.assertIn("Banking", INDUSTRIES["7"])

    def test_linkedin_urls_are_case_insensitively_deduplicated(self):
        first = normalize_linkedin_url("https://linkedin.com/in/Jane-Doe?trk=abc")
        second = normalize_linkedin_url("https://www.linkedin.com/in/jane-doe/")
        self.assertEqual(first, second)

    def test_person_company_identity_ignores_case_and_punctuation(self):
        first = person_identity_key("Jane D. Doe", "Acme, Inc.")
        second = person_identity_key("JANE D DOE", "ACME INC")
        self.assertEqual(first, second)

    def test_person_identity_requires_a_real_name_and_company(self):
        self.assertEqual(person_identity_key("Unknown", "Acme"), "")
        self.assertEqual(person_identity_key("Jane Doe", "Unknown"), "")

    def test_final_profile_requires_name_company_and_linkedin(self):
        self.assertTrue(is_export_ready_profile(
            "Jane Doe", "Acme", "https://linkedin.com/in/jane-doe",
        ))
        self.assertFalse(is_export_ready_profile(
            "Jane Doe", "Unknown", "https://linkedin.com/in/jane-doe",
        ))
        self.assertFalse(is_export_ready_profile(
            "Jane Doe", "Talent Acquisition at",
            "https://linkedin.com/in/jane-doe",
        ))
        self.assertFalse(is_export_ready_profile(
            "Jane Doe", "LinkedIn",
            "https://linkedin.com/in/jane-doe",
            designation="Head of Talent Acquisition",
        ))
        self.assertFalse(is_export_ready_profile(
            "Jane Doe 123", "Acme",
            "https://linkedin.com/in/jane-doe",
            designation="Head of Talent Acquisition",
        ))

    def test_role_matching_tolerates_connector_and_order_variants(self):
        self.assertTrue(check_role_in_title(
            "Global Head, Talent Acquisition", "",
            ["Head of Talent Acquisition"],
        ))
        self.assertTrue(check_role_in_title(
            "Vice President, Talent Acquisition", "",
            ["VP Talent Acquisition"],
        ))
        self.assertFalse(check_role_in_title(
            "Talent Acquisition Specialist", "",
            ["Head of Talent Acquisition"],
        ))

    def test_domain_brand_does_not_match_different_one_word_company(self):
        variants = company_name_variants("Apollo.io")

        self.assertTrue(any_term_matches("Apollo.io", variants))
        self.assertTrue(any_term_matches("Apollo IO", variants))
        self.assertFalse(any_term_matches("Apollo", variants))


if __name__ == "__main__":
    unittest.main()
