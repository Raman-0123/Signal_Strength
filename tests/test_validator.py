from speedy_scraper.models import RawCandidate
from speedy_scraper.validator import (
    company_matches,
    role_match_strength,
    role_matches,
    validate_candidate,
)


def _candidate(**overrides):
    data = {
        "name": "Rohit Chatter",
        "designation": "Chief Data and AI Officer",
        "company": "Angel One",
        "linkedin_url": "https://www.linkedin.com/in/rohit-chatter-525b62/",
        "title": "Rohit Chatter - Chief Data and AI Officer - Angel One",
        "body": "Location: Bengaluru · Angel One is a fintech stock broking and wealth platform.",
        "source": "fixture",
        "query": "q",
        "evidence": "Location: Bengaluru · fintech payments",
    }
    data.update(overrides)
    candidate = RawCandidate(**data)
    candidate.sources_seen = {"fixture"}
    return candidate


def test_validates_clean_fintech_bengaluru_role():
    lead, rejection = validate_candidate(
        _candidate(),
        roles=["CDO", "Chief Data Officer"],
        locations=["Bengaluru", "Bangalore"],
        industries=["FinTech", "Payments"],
        company_names=["Angel One"],
        existing_urls=set(),
    )
    assert rejection is None
    assert lead is not None
    assert lead.location == "Bengaluru"


def test_rejects_malformed_name_and_long_designation():
    lead, rejection = validate_candidate(
        _candidate(
            name="New CIO",
            designation="Additionally responsible for driving growth engagement talent team management and technical learning and development initiatives across CDO",
        ),
        roles=["CIO"],
        locations=["Bengaluru"],
        industries=["FinTech"],
        company_names=[],
        existing_urls=set(),
    )
    assert lead is None
    assert rejection is not None
    assert rejection.reason == "incomplete"


def test_role_matching_preserves_requested_function_and_seniority():
    assert role_matches("VP, Customer Success", ["VP Customer Success"])
    assert not role_matches("Talent Partner | GTM, CX, Biz Roles", ["CX"])
    assert not role_matches("Customer Experience Manager", ["VP Customer Experience"])
    assert not role_matches("Chief Digital Officer", ["Chief Data Officer"])
    assert role_matches("Chief Digital Officer", ["CDO"])
    assert role_matches("Co-Founder & CEO", ["Co-founder"])
    assert role_matches("Chief Executive Officer", ["CEO"])


def test_company_matching_ignores_legal_suffixes_but_not_mentions():
    assert company_matches("PayU Payments Private Limited", "PayU India")
    assert company_matches("MUFG", "MUFG Bank Singapore")
    assert not company_matches("ABC", "ABC Solutions")
    assert not company_matches("Booking.com", "Razorpay")


def test_generic_senior_titles_match_function_variants_without_downgrading_directors():
    assert role_match_strength("Senior Director of Customer Experience", "Senior Director")
    assert role_matches("Senior Director Technology", ["Senior Director"])
    assert not role_matches("Director of Customer Experience", ["Senior Director"])


def test_location_taxonomy_accepts_alias_and_returns_canonical_location():
    lead, rejection = validate_candidate(
        _candidate(body="Location: Bangalore, Karnataka, India · fintech payments"),
        roles=["Chief Data Officer"],
        locations=["Bengaluru"],
        industries=["FinTech"],
        company_names=["Angel One"],
        existing_urls=set(),
    )

    assert rejection is None
    assert lead is not None
    assert lead.location == "Bengaluru"


def test_strict_filter_contract_enforces_target_company_and_source_count():
    lead, rejection = validate_candidate(
        _candidate(company="Booking.com", title="Rohit Chatter - Chief Data Officer - Booking.com"),
        roles=["Chief Data Officer"],
        locations=["Bengaluru"],
        industries=["FinTech"],
        company_names=["Angel One"],
        existing_urls=set(),
        require_target_company=True,
        minimum_confidence=85,
    )
    assert lead is None
    assert rejection is not None
    assert rejection.reason == "target_company"

    candidate = _candidate()
    lead, rejection = validate_candidate(
        candidate,
        roles=["Chief Data Officer"],
        locations=["Bengaluru"],
        industries=["FinTech"],
        company_names=["Angel One"],
        existing_urls=set(),
        require_target_company=True,
        minimum_sources=2,
    )
    assert lead is None
    assert rejection is not None
    assert rejection.reason == "source_count"
