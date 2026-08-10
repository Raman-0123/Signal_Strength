"""Compact Google discovery and evidence-validation query construction."""

import math
import re

from core.signal_intelligence import (
    business_model_query_clause,
    gcc_query_clause,
    normalize_business_model,
)
from core.utils import _join, _q, normalize_text, split_csv_terms

QUERY_STRATEGY_VERSION = 11
MAX_DISCOVERY_QUERIES = 180
LINKEDIN_PROFILE_PREFIX = "site:linkedin.com/in"


_TALENT_ACQUISITION_PRIMARY = (
    "Head of Talent Acquisition",
    "Director of Talent Acquisition",
    "Talent Acquisition Director",
    "VP Talent Acquisition",
)

_TALENT_ACQUISITION_FALLBACK = (
    "Talent Acquisition Head",
    "Senior Director of Talent Acquisition",
    "Global Head of Talent Acquisition",
    "Head of Recruitment",
)


def _unique(values):
    seen = set()
    result = []
    for value in values or []:
        cleaned = str(value or "").strip()
        key = normalize_text(cleaned)
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def _or_group(values):
    values = _unique(values)
    if not values:
        return ""
    return f"({_join(values)})" if len(values) > 1 else _q(values[0])


def _is_talent_acquisition_group(roles):
    normalized = {normalize_text(role) for role in roles or []}
    return bool(normalized & {
        "head of talent acquisition",
        "talent acquisition head",
        "vp talent acquisition",
        "director of talent acquisition",
        "talent acquisition director",
    })


def role_discovery_profile(roles):
    """Return exact title groups for primary and one-shot fallback discovery."""
    roles = _unique(roles)
    if _is_talent_acquisition_group(roles):
        return (
            list(_TALENT_ACQUISITION_PRIMARY),
            _or_group(_TALENT_ACQUISITION_FALLBACK),
        )

    primary = roles[:4]
    # A fallback may use only explicit aliases belonging to the selected role
    # family. Never synthesize broad terms such as "talent" or "marketing",
    # which Google can reinterpret as dictionaries, jobs, or unrelated pages.
    alternate = roles[4:] or primary
    return primary, _or_group(alternate)


def canonical_location_terms(locations):
    """Use high-recall Google place names while retaining all aliases for validation."""
    locations = _unique(locations)
    normalized = {normalize_text(location) for location in locations}
    if normalized & {
        "bangalore", "bengaluru", "greater bengaluru area",
        "bengaluru urban", "bangalore urban",
    }:
        return [
            "Bengaluru, Karnataka, India",
            "Bangalore, Karnataka, India",
            "Greater Bengaluru Area",
        ]

    canonical = [
        location for location in locations
        if normalize_text(location) not in {"ncr"}
        and not normalize_text(location).endswith(" urban")
    ]
    return (canonical or locations)[:3]


def fallback_location_terms(locations):
    """Keep fallback discovery inside the same canonical location cluster."""
    return canonical_location_terms(locations)


def _normalize_groups(flat_values, groups):
    if groups:
        return [_unique(group) for group in groups if _unique(group)]
    values = _unique(flat_values)
    return [values] if values else []


def ensure_query_filters(query, all_locs, all_roles, all_inds=None, all_sigs=None,
                         custom_kws="", organization_kws=None,
                         business_model="Any", gcc_only=False,
                         include_context_terms=False):
    """Complete only discovery constraints in a user-edited Google query.

    Company fit, industry, signals, GCC and custom keywords deliberately remain
    out of discovery and are enforced by separate evidence queries.
    """
    value = " ".join(str(query or "").split()).strip()
    value = re.sub(
        r"site:(?:https?://)?(?:www\.)?linkedin\.com/in/?",
        LINKEDIN_PROFILE_PREFIX,
        value,
        flags=re.IGNORECASE,
    )
    lowered = value.lower()
    clauses = []
    if LINKEDIN_PROFILE_PREFIX.lower() not in lowered:
        clauses.append(LINKEDIN_PROFILE_PREFIX)

    primary_roles, _ = role_discovery_profile(all_roles)
    search_locations = canonical_location_terms(all_locs)
    for terms in (primary_roles, search_locations):
        if terms and not any(normalize_text(term) in normalize_text(value) for term in terms):
            clauses.append(_or_group(terms))
    if include_context_terms:
        organizations = split_csv_terms(organization_kws)
        custom_terms = split_csv_terms(custom_kws)
        for terms in (
            list(all_inds or [])[:4],
            custom_terms[:4],
            organizations[:6],
        ):
            if terms and not any(
                normalize_text(term) in normalize_text(value)
                for term in terms
            ):
                clauses.append(_or_group(terms))
    return " ".join(part for part in [value, *clauses] if part).strip()


def query_budget_for(count: int, all_locs=None, all_roles=None,
                     all_inds=None, all_sigs=None) -> int:
    """Scale discovery breadth to the requested net-new POC target.

    Planning assumes roughly 30 citations per distinct SERP family and a
    conservative 15% hard-gate acceptance rate, then adds 40% headroom.
    """
    target = max(1, int(count or 1))
    estimated = math.ceil((target / 0.15 / 30) * 1.4)
    return min(MAX_DISCOVERY_QUERIES, max(1, estimated))


def _chunks(values, size):
    values = _unique(values)
    return [
        values[index:index + size]
        for index in range(0, len(values), size)
        if values[index:index + size]
    ]


def _discovery_variants(role_group, location_group):
    """Create ordered, non-identical SERP families for one selection pair."""
    primary_roles, fallback_role = role_discovery_profile(role_group)
    ordered_roles = _unique([
        *primary_roles,
        *role_group,
    ])
    locations = canonical_location_terms(location_group)
    role_pairs = _chunks(ordered_roles, 2)
    all_role_pairs = [
        [ordered_roles[left], ordered_roles[right]]
        for left in range(len(ordered_roles))
        for right in range(left + 1, len(ordered_roles))
    ]
    role_singles = [[role] for role in ordered_roles]
    variants = [
        {
            "roles": primary_roles,
            "search_locations": locations,
            "variant": "grouped_primary",
            "fallback_role": fallback_role,
        },
    ]
    variants.extend({
        "roles": role_terms,
        "search_locations": locations,
        "variant": "role_pair_grouped_location",
        "fallback_role": "",
    } for role_terms in role_pairs)
    variants.extend({
        "roles": role_terms,
        "search_locations": locations,
        "variant": "single_role_grouped_location",
        "fallback_role": "",
    } for role_terms in role_singles)
    variants.extend({
        "roles": role_terms,
        "search_locations": [location],
        "variant": "role_pair_single_location",
        "fallback_role": "",
    } for role_terms in role_pairs for location in locations)
    variants.extend({
        "roles": role_terms,
        "search_locations": [location],
        "variant": "single_role_single_location",
        "fallback_role": "",
    } for role_terms in role_singles for location in locations)
    variants.extend({
        "roles": role_terms,
        "search_locations": [location],
        "variant": "role_pair_combination_single_location",
        "fallback_role": "",
    } for role_terms in all_role_pairs for location in locations)
    return variants


def build_query_plan(all_locs, all_roles, all_inds, all_sigs, custom_kws,
                     max_queries=None, organization_kws=None,
                     business_model="Any", gcc_only=False,
                     include_recovery=False, role_groups=None,
                     location_groups=None, include_context_terms=False):
    """Build grouped role/location discovery queries only.

    Selected industry, signal, keyword, business-model and GCC filters are
    recorded as required validation metadata but are not placed in discovery.
    """
    plan = []
    seen = set()
    role_groups = _normalize_groups(all_roles, role_groups)
    location_groups = _normalize_groups(all_locs, location_groups)
    organizations = split_csv_terms(organization_kws)
    custom_terms = split_csv_terms(custom_kws)
    limit = min(
        MAX_DISCOVERY_QUERIES,
        int(max_queries) if max_queries is not None else MAX_DISCOVERY_QUERIES,
    )
    required_filters = {
        "industries": list(all_inds or []),
        "signals": list(all_sigs or []),
        "custom_terms": custom_terms,
        "organizations": organizations,
        "business_model": normalize_business_model(business_model),
        "gcc_only": bool(gcc_only),
    }
    context_clauses = []
    if include_context_terms:
        for terms in (
            list(all_inds or [])[:4],
            custom_terms[:4],
            organizations[:6],
        ):
            clause = _or_group(terms)
            if clause:
                context_clauses.append(clause)

    grouped_variants = []
    for role_group in role_groups:
        for location_group in location_groups:
            grouped_variants.append((
                role_group,
                location_group,
                _discovery_variants(role_group, location_group),
            ))

    # Round-robin across selected role/location pairs so one rich alias family
    # cannot consume the whole target-derived budget.
    variant_index = 0
    while grouped_variants and len(plan) < limit:
        added_this_round = False
        for role_group, location_group, variants in grouped_variants:
            if variant_index >= len(variants):
                continue
            added_this_round = True
            variant = variants[variant_index]
            role_clause = _or_group(variant["roles"])
            search_locations = variant["search_locations"]
            location_clause = _or_group(search_locations)
            parts = [
                LINKEDIN_PROFILE_PREFIX,
                role_clause,
                location_clause,
                *context_clauses,
            ]
            query = " ".join(part for part in parts if part).strip()
            fallback_parts = [
                LINKEDIN_PROFILE_PREFIX,
                variant.get("fallback_role", ""),
                _or_group(fallback_location_terms(location_group)),
            ]
            fallback_query = " ".join(
                part for part in fallback_parts if part
            ).strip()
            if (
                not variant.get("fallback_role")
                or fallback_query == query
            ):
                fallback_query = ""
            if query in seen:
                continue
            seen.add(query)
            plan.append({
                "query": query,
                "fallback_query": fallback_query,
                "bucket": "discovery",
                "phase": "discovery",
                "roles": role_group,
                "locations": location_group,
                "search_locations": search_locations,
                "variant": variant["variant"],
                "required_filters": required_filters,
                "strategy_version": QUERY_STRATEGY_VERSION,
            })
            if len(plan) >= limit:
                return plan
        if not added_this_round:
            break
        variant_index += 1
    return plan


def build_company_validation_query(company, all_inds=None, custom_kws="",
                                   business_model="Any", gcc_only=False):
    """Build one cached company-evidence query for every selected company filter."""
    clauses = [_q(str(company).strip())]
    if all_inds:
        clauses.append(_or_group(list(all_inds)[:8]))
    custom_terms = split_csv_terms(custom_kws)
    if custom_terms:
        clauses.append(_or_group(custom_terms[:6]))
    business_clause = business_model_query_clause(business_model)
    if business_clause:
        clauses.append(business_clause)
    gcc_clause = gcc_query_clause(gcc_only)
    if gcc_clause:
        clauses.append(gcc_clause)
    return " ".join(part for part in clauses if part).strip()


def build_person_validation_query(name, company, all_sigs=None):
    """Build one cached person-attributed signal-evidence query."""
    signal_clause = _or_group(list(all_sigs or [])[:8])
    return " ".join(
        part for part in (_q(name), _q(company), signal_clause) if part
    ).strip()


def build_query_matrix(all_locs, all_roles, all_inds, all_sigs, custom_kws,
                       max_queries=None, organization_kws=None,
                       business_model="Any", gcc_only=False,
                       include_recovery=False, role_groups=None,
                       location_groups=None, include_context_terms=False):
    return [item["query"] for item in build_query_plan(
        all_locs, all_roles, all_inds, all_sigs, custom_kws,
        max_queries=max_queries, organization_kws=organization_kws,
        business_model=business_model, gcc_only=gcc_only,
        include_recovery=include_recovery,
        role_groups=role_groups,
        location_groups=location_groups,
        include_context_terms=include_context_terms,
    )]
