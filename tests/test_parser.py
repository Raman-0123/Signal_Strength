from speedy_scraper.models import SearchResult
from speedy_scraper.parser import candidates_from_results, parse_profile_fields


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
