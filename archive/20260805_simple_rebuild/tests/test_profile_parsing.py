import unittest

from core.utils import parse_profile

PROFILE_URL = "https://www.linkedin.com/in/jane-doe/"


class ProfileParsingTests(unittest.TestCase):
    def test_role_at_company_is_split_into_separate_fields(self):
        name, designation, company, url = parse_profile(
            "Jane Doe - Chief Marketing Officer at Acme Corp | LinkedIn",
            PROFILE_URL,
        )

        self.assertEqual(name, "Jane Doe")
        self.assertEqual(designation, "Chief Marketing Officer")
        self.assertEqual(company, "Acme Corp")
        self.assertEqual(url, PROFILE_URL)

    def test_three_part_linkedin_title_is_parsed(self):
        name, designation, company, _ = parse_profile(
            "Jane Doe - VP Marketing - Acme Corp | LinkedIn",
            PROFILE_URL,
        )

        self.assertEqual(name, "Jane Doe")
        self.assertEqual(designation, "VP Marketing")
        self.assertEqual(company, "Acme Corp")

    def test_explicit_role_company_fragment_beats_generic_title(self):
        name, designation, company, _ = parse_profile(
            "Jane Doe - Talent Leader - Head of Talent Acquisition at Acme | LinkedIn",
            PROFILE_URL,
        )

        self.assertEqual(name, "Jane Doe")
        self.assertEqual(designation, "Head of Talent Acquisition")
        self.assertEqual(company, "Acme")

    def test_historical_experience_is_not_used_as_current_company(self):
        _, designation, company, _ = parse_profile(
            "Jane Doe - Chief Marketing Officer | LinkedIn",
            PROFILE_URL,
            "Experience: Old Employer · Education: Example University",
        )

        self.assertEqual(designation, "Chief Marketing Officer")
        self.assertEqual(company, "Unknown")

    def test_explicit_current_fields_can_fill_missing_title_data(self):
        _, designation, company, _ = parse_profile(
            "Jane Doe | LinkedIn",
            PROFILE_URL,
            "Current: Chief Marketing Officer at Acme Corp · Location: Delhi",
        )

        self.assertEqual(designation, "Chief Marketing Officer")
        self.assertEqual(company, "Acme Corp")

    def test_location_only_title_is_not_mistaken_for_company(self):
        _, designation, company, _ = parse_profile(
            "Jane Doe - New Delhi, Delhi, India | Professional Profile",
            PROFILE_URL,
        )

        self.assertEqual(designation, "")
        self.assertEqual(company, "Unknown")

    def test_current_designation_is_recovered_from_profile_body(self):
        _, designation, company, _ = parse_profile(
            "Mohan J. - Epsilon | LinkedIn",
            PROFILE_URL,
            (
                "As the Sr Director of Talent Acquisition at Epsilon, I lead "
                "high-performing teams. · Experience: Epsilon · "
                "Location: Bengaluru · 500+ connections on LinkedIn."
            ),
        )

        self.assertEqual(designation, "Sr Director of Talent Acquisition")
        self.assertEqual(company, "Epsilon")

    def test_structured_experience_repairs_truncated_company(self):
        _, designation, company, _ = parse_profile(
            "Sabrina - Head of Talent Acquisition - Seeking out the ... | LinkedIn",
            PROFILE_URL,
            (
                "· Experience: SmartQ · Education: Example University · "
                "Location: Bengaluru · 500+ connections on LinkedIn."
            ),
        )

        self.assertEqual(designation, "Head of Talent Acquisition")
        self.assertEqual(company, "SmartQ")

    def test_pipe_headline_recovers_role_and_company(self):
        _, designation, company, _ = parse_profile(
            "Joyce V. - Talent Leader | Head of Talent Acquisition at CloudSEK | LinkedIn",
            PROFILE_URL,
            "",
        )

        self.assertEqual(designation, "Head of Talent Acquisition")
        self.assertEqual(company, "CloudSEK")

    def test_google_leading_card_fields_recover_current_role_and_company(self):
        name, designation, company, _ = parse_profile(
            "Shalini Sethi, Dutta - Hewlett Packard Enterprise",
            PROFILE_URL,
            (
                "United States · Head of Talent Acquisition · "
                "Hewlett Packard Enterprise Experience · "
                "Director, Global Talent Acquisition"
            ),
        )

        self.assertEqual(name, "Shalini Sethi, Dutta")
        self.assertEqual(designation, "Head of Talent Acquisition")
        self.assertEqual(company, "Hewlett Packard Enterprise")

    def test_google_company_tagline_is_not_sent_as_the_employer(self):
        name, designation, company, _ = parse_profile(
            "Rajatha A - Head of Talent Acquisition | LinkedIn",
            PROFILE_URL,
            (
                "Bengaluru, Karnataka, India · Head of Talent Acquisition · "
                "Alstom Recruiting the Future, Today"
            ),
        )

        self.assertEqual(name, "Rajatha A")
        self.assertEqual(designation, "Head of Talent Acquisition")
        self.assertEqual(company, "Alstom")

    def test_google_pipe_title_and_repeated_name_recover_identity_fields(self):
        name, designation, company, _ = parse_profile(
            (
                "Poornima Patil | Director of Talent Acquisition | "
                "Scaling AI & ..."
            ),
            PROFILE_URL,
            (
                "San Francisco Bay Area · AI / ML Executive Hiring- USA · "
                "Gemburg Poornima Patil | Director of Talent Acquisition | "
                "Scaling AI & Engineering Teams"
            ),
        )

        self.assertEqual(name, "Poornima Patil")
        self.assertEqual(designation, "Director of Talent Acquisition")
        self.assertEqual(company, "Gemburg")


if __name__ == "__main__":
    unittest.main()
