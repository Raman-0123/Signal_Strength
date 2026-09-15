from speedy_scraper.models import RawCandidate, SearchResult
from speedy_scraper.parser import (
    candidates_from_results,
    parse_profile_fields,
    repair_candidate_fields,
)


def test_parses_role_company_with_at_symbol():
    name, designation, company = parse_profile_fields(
        "Pankaj Goel - Chief Technology Officer @LeadSquared, Ex ...",
        "Location: Bengaluru · 500+ connections",
    )
    assert name == "Pankaj Goel"
    assert designation == "Chief Technology Officer"
    assert company == "LeadSquared"


def test_splits_json_style_blended_ddgs_records():
    blob = '''
      "https://www.linkedin.com/in/nivedita-kaushik/": {
        "body": "Location: Bengaluru · 500+ connections. SaaS fintech customer success leader.",
        "company": "Revenue & Customer Success Leader",
        "designation": "Head of Customer Success",
        "name": "Nivedita Kaushik",
        "title": "Nivedita Kaushik - Head of Customer Success - Razorpay | LinkedIn"
      },
      "https://www.linkedin.com/in/parveen-kumar-88b82216/": {
        "body": "Experience: Cashfree Payments · Location: Bengaluru · payments fintech.",
        "company": "Cashfree Payments",
        "designation": "SVP",
        "name": "Parveen Kumar",
        "title": "Parveen Kumar - SVP| Head of Operations and Customer ..."
      }
    '''
    result = SearchResult(title="", body=blob, href="", source="fixture", query="q")
    candidates = candidates_from_results([result])
    assert {candidate.name for candidate in candidates} == {"Nivedita Kaushik", "Parveen Kumar"}


def test_does_not_assign_primary_identity_to_unstructured_embedded_urls():
    result = SearchResult(
        title="Asha Rao - Chief Technology Officer - Razorpay | LinkedIn",
        body=(
            "Asha Rao is CTO at Razorpay. Related profiles: "
            "https://www.linkedin.com/in/unrelated-person/"
        ),
        href="https://www.linkedin.com/in/asha-rao/",
        source="fixture",
        query="q",
    )

    candidates = candidates_from_results([result])

    assert [candidate.linkedin_url for candidate in candidates] == [
        "https://www.linkedin.com/in/asha-rao/"
    ]


def test_parses_current_role_and_company_from_structured_body():
    assert parse_profile_fields(
        "Asha Rao | LinkedIn",
        "Current: Chief Technology Officer at Razorpay · Location: Bengaluru",
    ) == ("Asha Rao", "Chief Technology Officer", "Razorpay")


def test_structured_current_fields_override_historical_cio_mention():
    assert parse_profile_fields(
        "Nerissa Yu - Program manager",
        (
            "Location: Singapore · Title: Program Manager · "
            "Company: MedTech Innovator Asia Pacific Nerissa Yu - Program manager · "
            "ASEAN CHIEF INFORMATION OFFICER ASSOCIATION. Jul 2017."
        ),
    ) == ("Nerissa Yu", "Program Manager", "MedTech Innovator Asia Pacific")


def test_structured_company_stops_before_repeated_candidate_title():
    assert parse_profile_fields(
        "Abhinav Joshi - Director, Head of Development",
        (
            "Location: Dallas-Fort Worth Metroplex · "
            "Title: Head - Digital Payments and Lending - Citi Global Wealth · "
            "Company: Citi Abhinav Joshi - Director, Head of Development LinkedIn"
        ),
    ) == (
        "Abhinav Joshi",
        "Head - Digital Payments and Lending - Citi Global Wealth",
        "Citi",
    )


def test_repair_replaces_historical_role_with_structured_current_title():
    candidate = RawCandidate(
        name="Nerissa Yu",
        designation="CHIEF INFORMATION OFFICER ASSOCIATION",
        company="Jul 2017",
        linkedin_url="https://www.linkedin.com/in/nerissa-yu/",
        title="Nerissa Yu - Program manager",
        body=(
            "Location: Singapore · Title: Program Manager · "
            "Company: MedTech Innovator Asia Pacific Nerissa Yu - Program manager · "
            "ASEAN CHIEF INFORMATION OFFICER ASSOCIATION. Jul 2017."
        ),
        source="google_browser",
        query="q",
        evidence="",
    )

    repair_candidate_fields(candidate)

    assert candidate.designation == "Program Manager"
    assert candidate.company == "MedTech Innovator Asia Pacific"


def test_parses_google_location_role_company_summary():
    assert parse_profile_fields(
        "Dr Mukesh Mehta - CTO CIO & CISO | BFSI, Trading",
        (
            "Dr Mukesh Mehta 14.7K+ followers Mumbai Metropolitan Region · "
            "Chief Technology Officer · Investec A technology executive leading "
            "large-scale organizations"
        ),
    ) == ("Dr Mukesh Mehta", "CTO CIO & CISO | BFSI, Trading", "Investec")


def test_repairs_unknown_company_from_saved_google_candidate():
    candidate = RawCandidate(
        name="Atul Garg",
        designation="CTO (IT Head) in SIDBI",
        company="Unknown",
        linkedin_url="https://www.linkedin.com/in/atul-garg/",
        title="Atul Garg - CTO (IT Head) in SIDBI",
        body=(
            "Atul Garg 1.2K+ followers Mumbai, Maharashtra, India · "
            "Chief Technology Officer · SIDBI(Small Industries Development Bank of India) "
            "Chief Technology Officer. SIDBI."
        ),
        source="google_browser",
        query="q",
        evidence="",
    )

    repair_candidate_fields(candidate)

    assert candidate.company == "SIDBI(Small Industries Development Bank of India)"


def test_repairs_period_separated_google_company():
    candidate = RawCandidate(
        name="Milind Kesarkar",
        designation="Chief Technology Officer",
        company="Unknown",
        linkedin_url="https://www.linkedin.com/in/milind-kesarkar/",
        title="Milind Kesarkar - Chief Technology Officer",
        body=(
            "Milind Kesarkar 190+ followers Chief Technology Officer . "
            "Svamaan Financial Services Pvt. Ltd. Dec 2022 - Present 3 years. "
            "Mumbai, Maharashtra, India"
        ),
        source="google_browser",
        query="q",
        evidence="",
    )

    repair_candidate_fields(candidate)

    assert candidate.company == "Svamaan Financial Services Pvt. Ltd"
