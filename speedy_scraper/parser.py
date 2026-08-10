from __future__ import annotations

import re

from speedy_scraper.linkedin import normalize_linkedin_url
from speedy_scraper.models import RawCandidate, SearchResult
from speedy_scraper.text import clean_spaces, normalize_text

_PROFILE_SUFFIX_RE = re.compile(r"\s*(?:\|\s*)?LinkedIn(?:\s.*)?$", re.IGNORECASE)


def candidates_from_results(results: list[SearchResult]) -> list[RawCandidate]:
    candidates: list[RawCandidate] = []
    for result in results:
        candidates.extend(_json_record_candidates(result))
        direct_url = normalize_linkedin_url(result.href)
        if direct_url:
            candidate = parse_profile_result(result, direct_url)
            if candidate:
                candidates.append(candidate)
    return _dedupe_candidates(candidates)


def parse_profile_result(result: SearchResult, clean_url: str | None = None) -> RawCandidate | None:
    url = clean_url or normalize_linkedin_url(result.href)
    if not url:
        return None
    title = _trim_blended_title(result.title)
    body = _trim_blended_body(result.body)
    name, designation, company = parse_profile_fields(title, body)
    evidence = clean_spaces(f"{title} {body}")[:900]
    return RawCandidate(
        name=name,
        designation=designation,
        company=company,
        linkedin_url=url,
        title=title,
        body=body,
        source=result.source,
        query=result.query,
        evidence=evidence,
        sources_seen={result.source},
        queries_seen={result.query},
    )


def parse_profile_fields(title: str, body: str = "") -> tuple[str, str, str]:
    title = clean_spaces(_PROFILE_SUFFIX_RE.sub("", title or ""))
    parts = [
        _clean_fragment(part)
        for part in re.split(r"\s+[-–—]\s+", title)
        if _clean_fragment(part)
    ]
    if len(parts) == 1 and "|" in title:
        parts = [_clean_fragment(part) for part in title.split("|") if _clean_fragment(part)]
    name = parts[0] if parts else "Unknown"
    designation = ""
    company = ""

    for field in parts[1:]:
        role, org = _split_role_company(field)
        if role:
            designation = role
            company = org
            break
    if not designation and len(parts) >= 2:
        designation = parts[1]
    if not company and len(parts) >= 3:
        company = parts[2]

    body_role, body_company = _body_role_company(body)
    if body_role and (not designation or len(normalize_text(body_role).split()) > len(normalize_text(designation).split())):
        designation = body_role
    if body_company:
        company = body_company

    if not company:
        company = _experience_company(body)
    if not designation:
        designation = _title_field(body)
    if not company:
        company = _company_field(body)

    name = _clean_name(name)
    designation = _clean_designation(designation)
    company = _clean_company(company)
    return name or "Unknown", designation, company or "Unknown"


def merge_candidates(existing: RawCandidate, incoming: RawCandidate) -> RawCandidate:
    existing.sources_seen.update(incoming.sources_seen or {incoming.source})
    existing.queries_seen.update(incoming.queries_seen or {incoming.query})
    if _name_quality(incoming.name) > _name_quality(existing.name):
        existing.name = incoming.name
    if _designation_quality(incoming.designation) > _designation_quality(existing.designation):
        existing.designation = incoming.designation
    if _company_quality(incoming.company) > _company_quality(existing.company):
        existing.company = incoming.company
    merged_evidence = clean_spaces(f"{existing.evidence} {incoming.evidence}")
    existing.evidence = merged_evidence[:1800]
    existing.body = clean_spaces(f"{existing.body} {incoming.body}")[:1800]
    existing.title = existing.title or incoming.title
    return existing


def _json_record_candidates(result: SearchResult) -> list[RawCandidate]:
    text = f"{result.title}\n{result.body}"
    found: list[RawCandidate] = []
    pattern = re.compile(
        r'"(?P<url>https?://(?:www\.)?linkedin\.com/in/[^"]+)"\s*:\s*\{(?P<object>.*?)(?=\n\s*\}\s*,?\s*(?:"https?://|$))',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(text):
        url = normalize_linkedin_url(match.group("url"))
        if not url:
            continue
        obj = match.group("object")
        name = _json_value(obj, "name")
        designation = _json_value(obj, "designation")
        company = _json_value(obj, "company")
        title = _json_value(obj, "title") or result.title
        body = _json_value(obj, "body") or result.body
        if not designation or not company:
            parsed_name, parsed_designation, parsed_company = parse_profile_fields(title, body)
            name = name or parsed_name
            designation = designation or parsed_designation
            company = company or parsed_company
        found.append(
            RawCandidate(
                name=_clean_name(name),
                designation=_clean_designation(designation),
                company=_clean_company(company) or "Unknown",
                linkedin_url=url,
                title=_trim_blended_title(title),
                body=clean_spaces(body),
                source=result.source,
                query=result.query,
                evidence=clean_spaces(f"{title} {body}")[:1800],
                sources_seen={result.source},
                queries_seen={result.query},
            )
        )
    return found


def _json_value(text: str, key: str) -> str:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*"(?P<value>(?:[^"\\]|\\.)*)"', text, re.DOTALL)
    if not match:
        return ""
    return clean_spaces(match.group("value").replace('\\"', '"'))


def _trim_blended_title(title: str) -> str:
    value = clean_spaces(title)
    value = re.split(r"\|\s*LinkedIn", value, maxsplit=1, flags=re.IGNORECASE)[0]
    value = re.split(r"\.{3}|…", value, maxsplit=1)[0]
    return clean_spaces(value)


def _trim_blended_body(body: str) -> str:
    """Keep the snippet attached to one result, not a concatenated result page."""
    value = clean_spaces(body)
    if not value:
        return ""
    value = re.split(
        r"(?<=\.)\s+(?=[A-Z][A-Za-z.'’-]+(?:\s+[A-Z][A-Za-z.'’-]+){1,4}\s+[-–—]\s+)",
        value,
        maxsplit=1,
    )[0]
    return clean_spaces(value[:1200])


def _split_role_company(value: str) -> tuple[str, str]:
    match = re.search(r"(?P<role>.{2,100}?)\s+(?:at|@)\s*(?P<company>[A-Za-z0-9&.,' /()_-]{2,100})", value)
    if not match:
        return "", ""
    role = _clean_designation(match.group("role"))
    company = _clean_company(match.group("company"))
    return role, company


def _body_role_company(body: str) -> tuple[str, str]:
    for pattern in (
        r"\bCurrent\s*:\s*(?P<value>[^·•|\n]{2,160})",
        r"\bCurrently\s+(?:serving|working)\s+as\s+(?P<value>[^·•|\n]{2,160})",
    ):
        match = re.search(pattern, body, re.IGNORECASE)
        if match:
            role, company = _split_role_company(match.group("value"))
            if role or company:
                return role, company
    return "", ""


def _experience_company(body: str) -> str:
    match = re.search(r"\bExperience\s*:\s*([^·•|\n]{2,100})", body, re.IGNORECASE)
    if not match:
        return ""
    return _clean_company(match.group(1))


def _title_field(body: str) -> str:
    match = re.search(r"\bTitle\s*:\s*([^·•|\n]{2,100})", body, re.IGNORECASE)
    return _clean_designation(match.group(1)) if match else ""


def _company_field(body: str) -> str:
    match = re.search(r"\b(?:Current Company|Company)\s*:\s*([^·•|\n]{2,100})", body, re.IGNORECASE)
    return _clean_company(match.group(1)) if match else ""


def _clean_fragment(value: str) -> str:
    return clean_spaces(str(value or "").strip(" -–—|·•"))


def _clean_name(value: str) -> str:
    value = _clean_fragment(value)
    value = re.sub(r"\b(?:LinkedIn|Profile)\b.*$", "", value, flags=re.IGNORECASE)
    return clean_spaces(value)


def _clean_designation(value: str) -> str:
    value = _clean_fragment(value)
    value = re.sub(r"^(?:as|currently)\s+", "", value, flags=re.IGNORECASE)
    value = re.split(r"\s+Experience\s*:", value, maxsplit=1, flags=re.IGNORECASE)[0]
    return clean_spaces(value)


def _clean_company(value: str) -> str:
    value = _clean_fragment(value)
    value = re.split(r"\b(?:Ex|Formerly|Location|Education)\b\s*:?", value, maxsplit=1, flags=re.IGNORECASE)[0]
    value = re.split(r"[·•|]", value, maxsplit=1)[0]
    value = re.sub(r"^at\s+", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*,\s*$", "", value)
    return clean_spaces(value)


def _base_field_quality(value: str) -> int:
    key = normalize_text(value)
    if key in {"", "unknown", "none", "na", "n a"}:
        return 0
    words = key.split()
    if len(words) > 18:
        return 1
    return 10


def _name_quality(value: str) -> int:
    score = _base_field_quality(value)
    words = normalize_text(value).split()
    if not score or not 2 <= len(words) <= 6 or re.search(r"\d|@", value):
        return 0
    return score + max(0, 7 - abs(len(words) - 2))


def _designation_quality(value: str) -> int:
    score = _base_field_quality(value)
    key = normalize_text(value)
    if not score:
        return 0
    role_markers = {
        "chief",
        "customer",
        "director",
        "engineer",
        "head",
        "lead",
        "leader",
        "manager",
        "officer",
        "president",
        "success",
        "technology",
        "vice",
        "vp",
    }
    marker_score = sum(token in role_markers for token in key.split())
    length_penalty = max(0, len(key.split()) - 10)
    return score + marker_score * 3 - length_penalty


def _company_quality(value: str) -> int:
    score = _base_field_quality(value)
    words = normalize_text(value).split()
    if not score or len(words) > 8:
        return 0
    return score + max(0, 6 - len(words))


def _dedupe_candidates(candidates: list[RawCandidate]) -> list[RawCandidate]:
    by_url: dict[str, RawCandidate] = {}
    for candidate in candidates:
        if candidate.linkedin_url in by_url:
            merge_candidates(by_url[candidate.linkedin_url], candidate)
        else:
            by_url[candidate.linkedin_url] = candidate
    return list(by_url.values())
