from speedy_scraper.linkedin import linkedin_id, normalize_linkedin_url


def test_normalizes_standard_and_mobile_linkedin_profiles():
    assert normalize_linkedin_url("linkedin.com/in/Test-Person/?trk=public") == "https://www.linkedin.com/in/test-person/"
    assert (
        normalize_linkedin_url("https://www.linkedin.com/mwlite/profile/in/Legacy-123")
        == "https://www.linkedin.com/in/legacy-123/"
    )
    assert linkedin_id("https://www.linkedin.com/in/test-person/") == "test-person"


def test_rejects_non_personal_linkedin_links():
    assert normalize_linkedin_url("https://www.linkedin.com/company/razorpay/") == ""
    assert normalize_linkedin_url("https://example.com/in/person") == ""


def test_malformed_ip_like_values_fail_closed_without_raising():
    assert normalize_linkedin_url("[2001:db8::1]") == ""
    assert normalize_linkedin_url("https://[2001:db8::1") == ""
    assert normalize_linkedin_url("192.0.2.10") == ""
    assert normalize_linkedin_url("not a URL") == ""
