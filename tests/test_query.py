from speedy_scraper.config import preset_config
from speedy_scraper.models import ScrapeConfig
from speedy_scraper.query import build_queries


def test_queries_keep_every_selected_filter_class_and_cover_all_industries():
    config = preset_config("bengaluru_fintech_tech_cx")
    queries = build_queries(config)

    assert all(any(industry in query for industry in config.industries) for query in queries)
    assert all("Bengaluru" in query or "Bangalore" in query for query in queries)
    assert all("-jobs" in query and "-hiring" in query for query in queries)
    assert all(any(industry in query for query in queries) for industry in config.industries)
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


def test_strict_company_queries_never_drop_company_or_prompt_modifiers():
    config = ScrapeConfig(
        roles=["VP Marketing"],
        locations=["Singapore"],
        industries=["FinTech", "SaaS", "Payments", "InsurTech"],
        company_names=["Example Money"],
        require_target_company=True,
        include_terms=["enterprise"],
        exclude_terms=["former"],
        max_queries=100,
    )

    queries = build_queries(config)

    assert len(queries) >= 2
    assert all('"Example Money"' in query for query in queries)
    assert all("enterprise" in query and "-former" in query for query in queries)


def test_generic_roles_do_not_create_broad_queries_beside_specific_roles():
    config = ScrapeConfig(
        roles=["VP Marketing", "VP", "Senior Director"],
        locations=["Bengaluru"],
        industries=["FinTech"],
        max_queries=100,
    )

    queries = build_queries(config)

    assert queries
    assert all("Marketing" in query for query in queries)
    assert not any('(\"Senior Director\" OR VP' in query for query in queries)
