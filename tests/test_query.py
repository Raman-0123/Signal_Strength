from speedy_scraper.config import preset_config
from speedy_scraper.models import ScrapeConfig
from speedy_scraper.query import build_queries


def test_queries_include_broad_and_contextual_forms():
    config = preset_config("bengaluru_fintech_tech_cx")
    queries = build_queries(config)
    assert any("FinTech" not in query and "Payments" not in query for query in queries)
    assert any("FinTech" in query or "Payments" in query for query in queries)
    assert any("FinTech" in query for query in queries)
    assert config.company_names == []


def test_queries_expand_taxonomies_quote_phrases_and_use_all_filter_classes():
    config = ScrapeConfig(
        roles=["CEO", "Co-founder"],
        locations=["Mumbai"],
        industries=["FinTech", "Digital Payments"],
        company_names=["Example Money"],
        business_model="B2B only",
        max_queries=100,
    )

    queries = build_queries(config)

    assert any('"Chief Executive Officer"' in query for query in queries)
    assert any('"Mumbai, Maharashtra, India"' in query for query in queries)
    assert any('"Example Money"' in query for query in queries)
    assert any('"Digital Payments"' in query for query in queries)
    assert any("B2B" in query for query in queries)
    assert all(query.startswith("site:linkedin.com/in ") for query in queries)
