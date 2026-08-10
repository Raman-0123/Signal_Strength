from pathlib import Path

from speedy_scraper.exports import leads_frame, write_result
from speedy_scraper.models import ScrapeConfig, SearchResult
from speedy_scraper.pipeline import LeadScraper


class FakeSource:
    name = "fake"

    def search(self, query, *, max_results, headless=True):
        return [
            SearchResult(
                title="Asha Rao - Chief Technology Officer - Razorpay | LinkedIn",
                body="Location: Bengaluru · Razorpay is a payments fintech platform for businesses.",
                href="https://www.linkedin.com/in/asha-rao/",
                source="fake",
                query=query,
            )
        ]


def test_pipeline_counts_only_verified(monkeypatch):
    monkeypatch.setattr("speedy_scraper.pipeline.build_sources", lambda names: [FakeSource()])
    monkeypatch.setattr("speedy_scraper.pipeline.collect_company_evidence", lambda *args, **kwargs: "Razorpay payments fintech b2b")
    result = LeadScraper().run(
        ScrapeConfig(
            roles=["CTO", "Chief Technology Officer"],
            locations=["Bengaluru"],
            industries=["FinTech", "Payments"],
            company_names=["Razorpay"],
            target_count=1,
            sources=["fake"],
            max_queries=1,
        )
    )
    assert len(result.leads) == 1
    assert result.metrics["verified"] == 1


def test_export_has_verified_lead_columns(tmp_path: Path):
    monkeypatch_path = tmp_path / "leads.xlsx"
    result = LeadScraper()
    assert result
    from speedy_scraper.models import ScrapeResult, VerifiedLead

    scrape_result = ScrapeResult(
        leads=[
            VerifiedLead(
                name="Asha Rao",
                designation="Chief Technology Officer",
                company="Razorpay",
                location="Bengaluru",
                linkedin_id="asha-rao",
                linkedin_url="https://www.linkedin.com/in/asha-rao/",
                source="fixture",
                confidence=96,
                evidence="fixture",
            )
        ],
        rejections=[],
        metrics={"verified": 1},
        queries=["q"],
    )
    saved = write_result(scrape_result, monkeypatch_path)
    assert saved.exists()
    assert list(leads_frame(scrape_result.leads).columns) == [
        "Name",
        "Designation",
        "Company",
        "Location",
        "LinkedIn ID",
        "LinkedIn URL",
        "Source",
        "Confidence",
        "Evidence",
    ]

