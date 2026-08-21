from __future__ import annotations

from itertools import product

from speedy_scraper.models import ScrapeConfig
from speedy_scraper.taxonomy import location_query_groups, resolve_role, role_query_groups
from speedy_scraper.text import normalize_text, or_group, quote_term, unique_terms

LINKEDIN_SITE = "site:linkedin.com/in"
DEFAULT_EXCLUDE_TERMS = ("jobs", "hiring", "recruiter", "recruitment", "careers")


def build_queries(config: ScrapeConfig) -> list[str]:
    """Build focused queries in which every selected filter class is represented.

    Search engines routinely relax long queries. Keeping each industry chunk small and
    repeating the role/location context produces a wider but still relevant plan. The
    previous planner emitted coverage queries without industries and only used the first
    few industry chunks, which allowed an early candidate pool to be filled with noise.
    """
    specific_roles = [
        role
        for role in config.roles
        if resolve_role(role).function not in {"generic_vp", "generic_director"}
    ]
    role_groups = role_query_groups(specific_roles or config.roles) or [[]]
    location_groups = location_query_groups(config.locations) or [[]]
    industry_groups = _chunks(unique_terms(config.industries), 3) or [[]]
    companies = unique_terms(config.company_names)
    business_clause = _business_model_clause(config.business_model)
    modifiers = _query_modifiers(config)

    discovery = _query_matrix(
        role_groups=role_groups,
        location_groups=location_groups,
        industry_groups=industry_groups,
        companies=[""],
        business_clause=business_clause,
        modifiers=modifiers,
    )
    company_scoped = _query_matrix(
        role_groups=role_groups,
        location_groups=location_groups,
        industry_groups=industry_groups,
        companies=companies,
        business_clause=business_clause,
        modifiers=modifiers,
    )

    if config.require_target_company and company_scoped:
        ordered = company_scoped
    elif company_scoped:
        # Treat optional companies as strong search hints while retaining discovery.
        ordered = _weave([(company_scoped, 2), (discovery, 1)])
    else:
        ordered = discovery
    return _dedupe(ordered)[: config.max_queries]


def _query_matrix(
    *,
    role_groups: list[list[str]],
    location_groups: list[list[str]],
    industry_groups: list[list[str]],
    companies: list[str],
    business_clause: str,
    modifiers: str,
) -> list[str]:
    if not companies:
        return []
    dimensions = (companies, role_groups, location_groups, industry_groups)
    indexed = product(*(range(len(values)) for values in dimensions))
    # Diagonal ordering gives every dimension early coverage when max_queries is small.
    combinations = sorted(indexed, key=lambda indexes: (sum(indexes), max(indexes), indexes))
    queries: list[str] = []
    for company_index, role_index, location_index, industry_index in combinations:
        queries.append(
            _join(
                LINKEDIN_SITE,
                quote_term(companies[company_index]),
                or_group(role_groups[role_index]),
                or_group(location_groups[location_index]),
                or_group(industry_groups[industry_index]),
                business_clause,
                modifiers,
            )
        )
    return queries


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
    excludes = unique_terms([*DEFAULT_EXCLUDE_TERMS, *config.exclude_terms])
    exclude = " ".join(f"-{quote_term(term)}" for term in excludes)
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
