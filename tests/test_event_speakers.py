import json

import pytest

from speedy_scraper.event_speakers import (
    EventSpeaker,
    EventSpeakerError,
    choose_speaker_match,
    extract_event_speakers,
    extract_people_records,
    speaker_queries,
    speakers_frame,
    validate_public_source_url,
)
from speedy_scraper.models import SearchResult


def _flight_html(payload: dict) -> str:
    text = json.dumps({"allSpeakersData": payload})
    split = len(text) // 2
    chunk_one = json.dumps([1, text[:split]])
    chunk_two = json.dumps([1, text[split:]])
    return f"<script>self.__next_f.push({chunk_one})</script><script>self.__next_f.push({chunk_two})</script>"


def test_extracts_active_gff_speakers_from_split_flight_payload():
    html = _flight_html(
        {
            "data": [
                {
                    "speakerId": "1",
                    "fullName": "Asha Rao",
                    "desgination": "Chief Technology Officer",
                    "companyName": "Razorpay",
                    "country": {"country": "India"},
                    "isActive": True,
                    "linkedinProfile": "https://www.linkedin.com/in/asha-rao/?trk=public",
                },
                {
                    "speakerId": "2",
                    "fullName": "Inactive Person",
                    "companyName": "Hidden",
                    "isActive": False,
                },
                {
                    "speakerId": "1",
                    "fullName": "Asha Rao",
                    "companyName": "Razorpay",
                    "isActive": True,
                },
            ]
        }
    )
    speakers = extract_event_speakers(html, "https://globalfintechfest.com/speakers")
    assert len(speakers) == 1
    assert speakers[0].designation == "Chief Technology Officer"
    assert speakers[0].country == "India"
    assert speakers[0].linkedin_url == "https://www.linkedin.com/in/asha-rao/"
    assert speakers[0].match_status == "provided"


def test_extracts_generic_speakers_marker_not_only_gff_name():
    text = json.dumps(
        {
            "speakers": [
                {
                    "id": "p1",
                    "name": "Meera Iyer",
                    "jobTitle": "Chief Information Officer",
                    "organization": {"name": "PhonePe"},
                    "location": "India",
                    "linkedInUrl": "https://linkedin.com/in/meera-iyer/",
                }
            ]
        }
    )
    speakers = extract_people_records(text, "https://example.com/speakers")
    assert len(speakers) == 1
    assert speakers[0].name == "Meera Iyer"
    assert speakers[0].company == "PhonePe"
    assert speakers[0].linkedin_url == "https://www.linkedin.com/in/meera-iyer/"


def test_extracts_json_ld_person_records():
    html = """
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "Person",
      "name": "Kabir Shah",
      "jobTitle": "Chief Digital Officer",
      "worksFor": {"@type": "Organization", "name": "CRED"},
      "sameAs": ["https://www.linkedin.com/in/kabir-shah/"],
      "address": {"addressCountry": "India"}
    }
    </script>
    """
    speakers = extract_people_records(html, "https://example.com/team")
    assert len(speakers) == 1
    assert speakers[0].designation == "Chief Digital Officer"
    assert speakers[0].company == "CRED"
    assert speakers[0].country == "India"


def test_extracts_visible_linkedin_profile_links_from_generic_html():
    html = """
    <section class="speaker-card">
      <h3>Nisha Menon - VP Customer Success - Razorpay</h3>
      <p>Fintech leader based in Bengaluru.</p>
      <a href="https://www.linkedin.com/in/nisha-menon/?trk=abc">LinkedIn</a>
    </section>
    """
    speakers = extract_people_records(html, "https://example.com/people")
    assert len(speakers) == 1
    assert speakers[0].name == "Nisha Menon"
    assert speakers[0].designation == "VP Customer Success"
    assert speakers[0].company == "Razorpay"


def test_speaker_queries_start_with_company_and_designation_scoped_profile_search():
    speaker = EventSpeaker(
        speaker_id="1",
        name="Asha Rao",
        designation="Chief Technology Officer",
        company="Razorpay",
        country="India",
        linkedin_url="",
        match_status="not_found",
        confidence=0.0,
        match_evidence="",
        source_url="https://example.com/speakers",
    )

    queries = speaker_queries(speaker)

    assert queries[0].startswith('site:linkedin.com/in "Asha Rao" "Razorpay"')
    assert '"Chief Technology Officer"' in queries[0]
    assert '-"jobs"' in queries[0]
    assert '-"recruiter"' in queries[0]


def test_chooses_confident_public_search_match():
    speaker = EventSpeaker(
        speaker_id="1",
        name="Asha Rao",
        designation="Chief Technology Officer",
        company="Razorpay",
        country="India",
        linkedin_url="",
        match_status="not_found",
        confidence=0.0,
        match_evidence="",
        source_url="https://globalfintechfest.com/speakers",
    )
    decision = choose_speaker_match(
        speaker,
        [
            SearchResult(
                title="Asha Rao - Chief Technology Officer - Razorpay | LinkedIn",
                body="Location: Bengaluru · Razorpay payments fintech.",
                href="https://www.linkedin.com/in/asha-rao/",
                source="fixture",
                query="q",
            )
        ],
    )
    assert decision.match_status == "matched"
    assert decision.linkedin_url == "https://www.linkedin.com/in/asha-rao/"


def test_ambiguous_match_keeps_linkedin_blank():
    speaker = EventSpeaker(
        speaker_id="1",
        name="Asha Rao",
        designation="Chief Technology Officer",
        company="Razorpay",
        country="India",
        linkedin_url="",
        match_status="not_found",
        confidence=0.0,
        match_evidence="",
        source_url="https://globalfintechfest.com/speakers",
    )
    decision = choose_speaker_match(
        speaker,
        [
            SearchResult(
                title="Asha Rao - LinkedIn",
                body="Public profile.",
                href="https://www.linkedin.com/in/asha-one/",
                source="fixture",
                query="q",
            ),
            SearchResult(
                title="Asha Rao - LinkedIn",
                body="Public profile.",
                href="https://www.linkedin.com/in/asha-two/",
                source="fixture",
                query="q",
            ),
        ],
    )
    assert decision.match_status == "ambiguous"
    assert decision.linkedin_url == ""


def test_does_not_auto_match_context_found_only_in_blended_body():
    speaker = EventSpeaker(
        speaker_id="1",
        name="Asha Rao",
        designation="Chief Technology Officer",
        company="Razorpay",
        country="India",
        linkedin_url="",
        match_status="not_found",
        confidence=0.0,
        match_evidence="",
        source_url="https://example.com/speakers",
    )

    decision = choose_speaker_match(
        speaker,
        [
            SearchResult(
                title="Asha Rao - Independent Advisor | LinkedIn",
                body="Related results mention a Chief Technology Officer at Razorpay.",
                href="https://www.linkedin.com/in/asha-advisor/",
                source="fixture",
                query="q",
            )
        ],
    )

    assert decision.match_status == "ambiguous"
    assert decision.linkedin_url == ""


def test_rejects_private_source_urls():
    with pytest.raises(EventSpeakerError):
        validate_public_source_url("http://127.0.0.1/speakers")


def test_speaker_export_schema():
    frame = speakers_frame(
        [
            EventSpeaker(
                speaker_id="1",
                name="Asha Rao",
                designation="CTO",
                company="Razorpay",
                country="India",
                linkedin_url="https://www.linkedin.com/in/asha-rao/",
                match_status="provided",
                confidence=1.0,
                match_evidence="event page",
                source_url="https://globalfintechfest.com/speakers",
            )
        ]
    )
    assert list(frame.columns) == [
        "Name",
        "Designation",
        "Company",
        "Country",
        "LinkedIn ID",
        "LinkedIn URL",
        "Match Status",
        "Confidence",
        "Match Evidence",
        "Source URL",
    ]
    assert frame.iloc[0]["LinkedIn ID"] == "asha-rao"
