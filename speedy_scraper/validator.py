from __future__ import annotations

import re

from speedy_scraper.linkedin import linkedin_id, normalize_linkedin_url
from speedy_scraper.models import RawCandidate, RejectedCandidate, VerifiedLead
from speedy_scraper.sources import independent_source_families
from speedy_scraper.taxonomy import canonical_location_from_text, role_definition_matches
from speedy_scraper.text import any_term_in_text, clean_spaces, normalize_text, term_in_text

_INVALID_NAMES = {
    "new cio",
    "new cdo",
    "new cto",
    "linkedin member",
    "linkedin profile",
    "professional profile",
    "unknown",
}

_BAD_COMPANY_MARKERS = (
    "500 connections",
    "500+ connections",
    "view ",
    "professional community",
    "linkedin",
)

_BAD_DESIGNATION_MARKERS = (
    "responsible for driving",
    "looking for",
    "hiring",
    "job opening",
    "show more",
    "posted by",
    "professional community",
)

_B2B_TERMS = [
    "b2b",
    "enterprise",
    "business customers",
    "merchant",
    "api",
    "platform",
    "saas",
    "bank partners",
    "corporate",
    "payments infrastructure",
]

_COMPANY_NOISE_TOKENS = {
    "co",
    "company",
    "corp",
    "corporation",
    "financial",
    "group",
    "inc",
    "india",
    "limited",
    "llc",
    "ltd",
    "payment",
    "payments",
    "private",
    "pvt",
    "services",
    "solutions",
    "systems",
    "technologies",
    "technology",
    "bank",
    "banks",
    "singapore",
    "india",
    "asia",
}


def validate_candidate(
    candidate: RawCandidate,
    *,
    roles: list[str],
    locations: list[str],
    industries: list[str],
    company_names: list[str],
    existing_urls: set[str],
    company_evidence: str = "",
    business_model: str = "Any",
    require_target_company: bool = False,
    minimum_confidence: int = 0,
    minimum_sources: int = 1,
) -> tuple[VerifiedLead | None, RejectedCandidate | None]:
    url = normalize_linkedin_url(candidate.linkedin_url)
    if not url:
        return None, _reject(candidate, "invalid_linkedin_url")
    if url in existing_urls:
        return None, _reject(candidate, "duplicate")
    if not _clean_identity(candidate):
        return None, _reject(candidate, "incomplete")
    if not role_matches(candidate.designation, roles):
        return None, _reject(candidate, "role")
    if require_target_company and company_names and not any(
        company_matches(candidate.company, company) for company in company_names
    ):
        return None, _reject(candidate, "target_company")
    location = location_match(candidate, locations)
    if not location:
        return None, _reject(candidate, "location")
    if not industry_matches(candidate, industries, company_names, company_evidence):
        return None, _reject(candidate, "industry_company")
    if not business_model_matches(candidate, company_evidence, business_model):
        return None, _reject(candidate, "business_model")
    if _independent_source_count(candidate.sources_seen or {candidate.source}) < max(
        1, minimum_sources
    ):
        return None, _reject(candidate, "source_count")
    evidence = clean_spaces(f"{candidate.evidence} {company_evidence}")[:1200]
    confidence = confidence_score(
        candidate,
        roles,
        locations,
        industries,
        company_names,
        company_evidence,
        business_model,
    )
    if confidence < minimum_confidence:
        return None, _reject(candidate, "confidence")
    return (
        VerifiedLead(
            name=clean_spaces(candidate.name),
            designation=clean_spaces(candidate.designation),
            company=clean_spaces(candidate.company),
            location=location,
            linkedin_id=linkedin_id(url),
            linkedin_url=url,
            source=", ".join(sorted(candidate.sources_seen)) or candidate.source,
            confidence=confidence,
            evidence=evidence,
        ),
        None,
    )


def role_matches(designation: str, roles: list[str]) -> bool:
    if not roles:
        return True
    return any(role_match_strength(designation, role) > 0 for role in roles)


def role_match_strength(designation: str, role: str) -> int:
    """Return 0 for no match and a larger value for a more exact role match."""
    return role_definition_matches(designation, role)


def company_matches(actual: str, requested: str) -> bool:
    if not normalize_text(actual) or not normalize_text(requested):
        return False
    actual_variants = _company_variants(actual)
    requested_variants = _company_variants(requested)
    return bool(actual_variants & requested_variants)


def company_match_strength(actual: str, requested: str) -> int:
    actual_key = normalize_text(actual)
    requested_key = normalize_text(requested)
    if not actual_key or not requested_key:
        return 0
    if actual_key == requested_key:
        return 3
    return 2 if company_matches(actual, requested) else 0


def _company_variants(value: str) -> set[str]:
    key = normalize_text(value)
    tokens = key.split()
    compact = "".join(tokens)
    core_tokens = [token for token in tokens if token not in _COMPANY_NOISE_TOKENS]
    variants = {key, compact}
    if core_tokens:
        core_phrase = " ".join(core_tokens)
        core_compact = "".join(core_tokens)
        if len(core_tokens) > 1 or len(core_tokens[0]) >= 4:
            variants.add(core_phrase)
            variants.add(core_compact)
        variants.update(
            token for token in core_tokens
            if len(token) >= 4 and token not in _COMPANY_NOISE_TOKENS
        )
    return {variant for variant in variants if len(variant.replace(" ", "")) >= 3}


def _role_level(value: str) -> str:
    if re.search(r"\b(?:svp|senior vice president)\b", value):
        return "vp"
    if re.search(r"\b(?:vp|vice president)\b", value):
        return "vp"
    for level in ("chief", "head", "director", "leader", "manager"):
        if term_in_text(value, level):
            return level
    return ""


def _level_matches(text: str, level: str) -> bool:
    if level == "vp":
        return bool(re.search(r"\b(?:s?vp|(?:senior )?vice president)\b", text))
    if level == "leader":
        return bool(re.search(r"\blead(?:er)?\b", text))
    return term_in_text(text, level)


def location_match(candidate: RawCandidate, locations: list[str]) -> str:
    if not locations:
        return "Not requested"
    text = clean_spaces(f"{candidate.title} {candidate.body} {candidate.evidence}")
    structured = re.search(r"\bLocation\s*:\s*([^·•|\n]{2,90})", text, re.IGNORECASE)
    if structured:
        location = clean_spaces(structured.group(1))
        canonical = canonical_location_from_text(location, locations)
        if canonical:
            return canonical
    return canonical_location_from_text(text[:1400], locations)


def canonical_location(location: str) -> str:
    return canonical_location_from_text(location, [location]) or clean_spaces(location)


def industry_matches(
    candidate: RawCandidate,
    industries: list[str],
    company_names: list[str],
    company_evidence: str = "",
) -> bool:
    if not industries and not company_names:
        return True
    evidence = clean_spaces(f"{candidate.company} {candidate.designation} {candidate.evidence} {company_evidence}")
    if company_names and any_term_in_text(candidate.company, company_names):
        return True
    return any_term_in_text(evidence, industries)


def business_model_matches(candidate: RawCandidate, company_evidence: str, business_model: str) -> bool:
    desired = normalize_text(business_model)
    if desired in {"", "any"}:
        return True
    evidence = clean_spaces(f"{candidate.company} {candidate.evidence} {company_evidence}")
    if desired in {"b2b only", "b2b"}:
        return any_term_in_text(evidence, _B2B_TERMS)
    if desired in {"b2c only", "b2c"}:
        return any_term_in_text(evidence, ["consumer", "customers", "users", "retail"])
    return True


def confidence_score(
    candidate: RawCandidate,
    roles: list[str],
    locations: list[str],
    industries: list[str],
    company_names: list[str],
    company_evidence: str,
    business_model: str = "Any",
) -> int:
    score = 50
    if roles and role_matches(candidate.designation, roles):
        score += 15
    if company_names and any(company_matches(candidate.company, company) for company in company_names):
        score += 12
    if locations and location_match(candidate, locations):
        score += 10
    if industries and industry_matches(candidate, industries, [], company_evidence):
        score += 8
    if len(candidate.sources_seen) > 1:
        score += 5
    if company_evidence:
        score += 3
    if normalize_text(business_model) not in {"", "any"}:
        score += 4
    return min(99, score)


def _clean_identity(candidate: RawCandidate) -> bool:
    name = clean_spaces(candidate.name)
    designation = clean_spaces(candidate.designation)
    company = clean_spaces(candidate.company)
    name_key = normalize_text(name)
    company_key = normalize_text(company)
    designation_key = normalize_text(designation)
    if name_key in _INVALID_NAMES or not name_key:
        return False
    if len(name_key.split()) < 2 or len(name_key.split()) > 6:
        return False
    if re.search(r"\d|@", name):
        return False
    if not designation_key or len(designation_key.split()) > 18:
        return False
    if any(marker in designation.lower() for marker in _BAD_DESIGNATION_MARKERS):
        return False
    # Allow company to be Unknown/empty — the target_company gate handles this constraint.
    if company_key and len(company_key.split()) > 10:
        return False
    if any(marker in company.lower() for marker in _BAD_COMPANY_MARKERS):
        return False
    return True


def _reject(candidate: RawCandidate, reason: str) -> RejectedCandidate:
    return RejectedCandidate(
        name=clean_spaces(candidate.name),
        designation=clean_spaces(candidate.designation),
        company=clean_spaces(candidate.company),
        linkedin_url=normalize_linkedin_url(candidate.linkedin_url) or candidate.linkedin_url,
        reason=reason,
        source=", ".join(sorted(candidate.sources_seen)) or candidate.source,
        evidence=clean_spaces(candidate.evidence)[:900],
    )


def _independent_source_count(sources: set[str]) -> int:
    return len(independent_source_families(sources))
