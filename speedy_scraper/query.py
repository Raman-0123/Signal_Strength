from __future__ import annotations

from speedy_scraper.models import ScrapeConfig
from speedy_scraper.taxonomy import location_query_groups, role_query_groups
from speedy_scraper.text import normalize_text, or_group, quote_term, unique_terms

LINKEDIN_SITE = "site:linkedin.com/in"


def build_queries(config: ScrapeConfig) -> list[str]:
    role_groups = role_query_groups(config.roles)
    location_groups = location_query_groups(config.locations)
    industries = unique_terms(config.industries)
    company_names = unique_terms(config.company_names)
    industry_groups = _chunks(industries, 2) or [[]]
    business_clause = _business_model_clause(config.business_model)
    modifiers = _query_modifiers(config)
    precise: list[str] = []
    contextual: list[str] = []
    coverage: list[str] = []

    combinations = [
        (company_index + role_index + location_index, company_index, role_index, location_index)
        for company_index in range(len(company_names))
        for role_index in range(len(role_groups))
        for location_index in range(max(1, len(location_groups)))
    ]
    for _weight, company_index, role_index, location_index in sorted(combinations):
        role_clause = or_group(role_groups[role_index])
        company_clause = quote_term(company_names[company_index])
        location_clause = (
            or_group(location_groups[location_index]) if location_groups else ""
        )
        precise.append(_join(LINKEDIN_SITE, role_clause, company_clause, location_clause, modifiers))

    for role_index, role_group in enumerate(role_groups):
        role_clause = or_group(role_group)
        for location_group in location_groups or [[]]:
            location_clause = or_group(location_group)
            industry_clause = or_group(industry_groups[role_index % len(industry_groups)])
            contextual.append(
                _join(LINKEDIN_SITE, role_clause, location_clause, industry_clause, modifiers)
            )
            if business_clause:
                contextual.append(
                    _join(LINKEDIN_SITE, role_clause, location_clause, business_clause, modifiers)
                )
            coverage.append(_join(LINKEDIN_SITE, role_clause, location_clause, modifiers))
        for company_index, company in enumerate(company_names):
            industry_clause = or_group(industry_groups[company_index % len(industry_groups)])
            contextual.append(
                _join(LINKEDIN_SITE, role_clause, quote_term(company), industry_clause, modifiers)
            )

    for role in unique_terms(config.roles):
        for location_group in location_groups:
            for location in location_group:
                coverage.append(_join(LINKEDIN_SITE, quote_term(role), quote_term(location), modifiers))

    if config.require_target_company:
        ordered = _weave([(precise, 3), (contextual, 1), (coverage, 1)])
    else:
        ordered = _weave([(contextual, 2), (precise, 1), (coverage, 1)])
    return _dedupe(ordered)[: config.max_queries]


def _chunks(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


def _business_model_clause(value: str) -> str:
    key = normalize_text(value)
    if key in {"b2b", "b2b only"}:
        return or_group(["B2B", "enterprise"])
    if key in {"b2c", "b2c only"}:
        return or_group(["B2C", "consumer"])
    return ""


def _query_modifiers(config: ScrapeConfig) -> str:
    include = " ".join(quote_term(term) for term in unique_terms(config.include_terms))
    exclude = " ".join(f"-{quote_term(term)}" for term in unique_terms(config.exclude_terms))
    if normalize_text(config.query_mode) in {"exact", "exact strict", "exact / strict", "strict"}:
        include = " ".join(part for part in (include, '"current"') if part)
    return " ".join(part for part in (include, exclude) if part)


def _join(*parts: str) -> str:
    return " ".join(part for part in parts if part)


def _weave(weighted_stages: list[tuple[list[str], int]]) -> list[str]:
    indexes = [0] * len(weighted_stages)
    result: list[str] = []
    while any(indexes[index] < len(stage) for index, (stage, _weight) in enumerate(weighted_stages)):
        for index, (stage, weight) in enumerate(weighted_stages):
            end = min(len(stage), indexes[index] + max(1, weight))
            result.extend(stage[indexes[index] : end])
            indexes[index] = end
    return result


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = " ".join(value.split())
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result
