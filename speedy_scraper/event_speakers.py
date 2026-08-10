from __future__ import annotations

import ipaddress
import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup

from speedy_scraper.linkedin import linkedin_id, normalize_linkedin_url
from speedy_scraper.models import DEFAULT_SOURCE_NAMES, SearchResult
from speedy_scraper.parser import parse_profile_fields
from speedy_scraper.sources import SearchSource, SourceError, build_sources, close_sources
from speedy_scraper.text import clean_spaces, normalize_text
from speedy_scraper.validator import company_matches

MAX_HTML_BYTES = 10 * 1024 * 1024
AUTO_MATCH_THRESHOLD = 0.80
AUTO_MATCH_MARGIN = 0.10

ProgressCallback = Callable[[dict[str, object]], None]


class EventSpeakerError(RuntimeError):
    pass


class NoSpeakersFoundError(EventSpeakerError):
    pass


PEOPLE_DATASET_MARKERS = (
    "allSpeakersData",
    "speakersData",
    "speakerData",
    "allSpeakers",
    "speakers",
    "people",
    "persons",
    "participants",
    "attendees",
    "teamMembers",
    "members",
)


@dataclass
class EventSpeaker:
    speaker_id: str
    name: str
    designation: str
    company: str
    country: str
    linkedin_url: str
    match_status: str
    confidence: float
    match_evidence: str
    source_url: str

    def as_dict(self) -> dict[str, object]:
        canonical = normalize_linkedin_url(self.linkedin_url)
        return {
            "Name": self.name,
            "Designation": self.designation,
            "Company": self.company,
            "Country": self.country,
            "LinkedIn ID": linkedin_id(canonical),
            "LinkedIn URL": canonical,
            "Match Status": self.match_status,
            "Confidence": round(float(self.confidence), 2),
            "Match Evidence": self.match_evidence,
            "Source URL": self.source_url,
        }


def find_event_speaker_linkedin_ids(
    source_url: str,
    *,
    enrich_missing: bool = True,
    sources: list[str] | None = None,
    browser_headless: bool = True,
    progress: ProgressCallback | None = None,
) -> list[EventSpeaker]:
    return find_url_people_linkedin_ids(
        source_url,
        enrich_missing=enrich_missing,
        sources=sources,
        browser_headless=browser_headless,
        progress=progress,
    )


def find_url_people_linkedin_ids(
    source_url: str,
    *,
    enrich_missing: bool = True,
    sources: list[str] | None = None,
    browser_headless: bool = True,
    progress: ProgressCallback | None = None,
) -> list[EventSpeaker]:
    html = fetch_public_html(source_url)
    speakers = extract_people_records(html, source_url)
    _emit(progress, "extracted", extracted=len(speakers), provided=count_status(speakers, "provided"))
    if not enrich_missing:
        return speakers

    search_sources = build_sources(sources or list(DEFAULT_SOURCE_NAMES))
    enriched: list[EventSpeaker] = []
    total = len(speakers)
    try:
        for index, speaker in enumerate(speakers, start=1):
            if speaker.match_status == "provided":
                enriched.append(speaker)
                _emit(
                    progress,
                    "speaker",
                    index=index,
                    total=total,
                    status="provided",
                    name=speaker.name,
                )
                continue
            enriched_speaker = enrich_speaker(
                speaker,
                search_sources,
                headless=browser_headless,
            )
            enriched.append(enriched_speaker)
            _emit(
                progress,
                "speaker",
                index=index,
                total=total,
                status=enriched_speaker.match_status,
                name=speaker.name,
            )
    finally:
        close_sources(search_sources)
    _emit(
        progress,
        "finished",
        extracted=len(enriched),
        provided=count_status(enriched, "provided"),
        matched=count_status(enriched, "matched"),
        ambiguous=count_status(enriched, "ambiguous"),
        not_found=count_status(enriched, "not_found"),
    )
    return enriched


def fetch_public_html(source_url: str) -> str:
    current = validate_public_source_url(source_url)
    opener = urllib.request.build_opener(_NoRedirectHandler)
    for _ in range(6):
        request = urllib.request.Request(
            current,
            headers={
                "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/124 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        try:
            response = opener.open(request, timeout=30)
        except urllib.error.HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308}:
                location = exc.headers.get("Location")
                if not location:
                    raise EventSpeakerError(f"Redirect without Location from {current}") from exc
                current = validate_public_source_url(urllib.parse.urljoin(current, location))
                continue
            raise EventSpeakerError(f"Failed to fetch page: HTTP {exc.code}") from exc
        final_url = validate_public_source_url(response.geturl())
        content_type = response.headers.get("Content-Type", "")
        if content_type and "html" not in content_type.lower():
            raise EventSpeakerError(f"Expected HTML but got {content_type}")
        payload = response.read(MAX_HTML_BYTES + 1)
        if len(payload) > MAX_HTML_BYTES:
            raise EventSpeakerError("HTML response exceeded 10 MiB")
        charset = response.headers.get_content_charset() or "utf-8"
        current = final_url
        return payload.decode(charset, errors="replace")
    raise EventSpeakerError("Too many redirects while fetching event page")


def validate_public_source_url(source_url: str) -> str:
    parsed = urllib.parse.urlparse(str(source_url or "").strip())
    if parsed.scheme not in {"http", "https"}:
        raise EventSpeakerError("Only http/https source URLs are allowed")
    if parsed.username or parsed.password:
        raise EventSpeakerError("Credentials are not allowed in source URLs")
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise EventSpeakerError("Source URL requires a hostname")
    if hostname in {"localhost", "localhost.localdomain"}:
        raise EventSpeakerError("Localhost source URLs are not allowed")
    try:
        addresses = socket.getaddrinfo(hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
    except socket.gaierror as exc:
        raise EventSpeakerError(f"Could not resolve source hostname: {hostname}") from exc
    for address in addresses:
        ip_value = address[4][0]
        try:
            ip = ipaddress.ip_address(ip_value)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise EventSpeakerError("Private, loopback, or reserved source addresses are not allowed")
    return urllib.parse.urlunparse(parsed)


def extract_event_speakers(html: str, source_url: str) -> list[EventSpeaker]:
    return extract_people_records(html, source_url)


def extract_people_records(html: str, source_url: str) -> list[EventSpeaker]:
    datasets = _people_datasets(html)
    speakers: list[EventSpeaker] = []
    seen_ids: set[str] = set()
    seen_people: set[str] = set()
    seen_urls: set[str] = set()
    url_indexes: dict[str, int] = {}
    for dataset_name, rows in datasets:
        for index, raw in enumerate(rows):
            speaker = _speaker_from_raw(raw, source_url, dataset_name, index)
            if speaker is None:
                continue
            identity = f"{normalize_text(speaker.name)}|{normalize_text(speaker.company)}"
            canonical_url = normalize_linkedin_url(speaker.linkedin_url)
            if (
                speaker.speaker_id in seen_ids
                or identity in seen_people
                or (canonical_url and canonical_url in seen_urls)
            ):
                if canonical_url and canonical_url in url_indexes:
                    current_index = url_indexes[canonical_url]
                    current = speakers[current_index]
                    current_quality = sum(bool(value) for value in (current.designation, current.company, current.country))
                    speaker_quality = sum(bool(value) for value in (speaker.designation, speaker.company, speaker.country))
                    if (
                        speaker_quality > current_quality
                        or (
                            speaker_quality == current_quality
                            and len(normalize_text(speaker.name).split()) < len(normalize_text(current.name).split())
                        )
                    ):
                        speakers[current_index] = speaker
                continue
            seen_ids.add(speaker.speaker_id)
            seen_people.add(identity)
            if canonical_url:
                seen_urls.add(canonical_url)
                url_indexes[canonical_url] = len(speakers)
            speakers.append(speaker)
    if not speakers:
        raise NoSpeakersFoundError(
            "No supported people dataset was found. Supported: event speaker JSON, "
            "Next.js embedded people/speakers data, JSON-LD Person, visible LinkedIn profile links, "
            "Divi/Elementor speaker-overlay cards, or generic HTML speaker card grids."
        )
    return speakers


def enrich_speaker(
    speaker: EventSpeaker,
    sources: list[SearchSource],
    *,
    headless: bool = True,
    include_terms: list[str] | None = None,
    exclude_terms: list[str] | None = None,
    on_source_error: ProgressCallback | None = None,
) -> EventSpeaker:
    results: list[SearchResult] = []
    for query in speaker_queries(speaker, include_terms=include_terms, exclude_terms=exclude_terms):
        for source in sources:
            try:
                results.extend(source.search(query, max_results=10, headless=headless))
            except SourceError as exc:
                if on_source_error:
                    _emit(
                        on_source_error,
                        "captcha_required" if exc.challenge else "source_error",
                        source=source.name,
                        message=str(exc),
                    )
                continue
        decision = choose_speaker_match(speaker, results)
        if decision.match_status == "matched":
            return decision
    return choose_speaker_match(speaker, results)


def speaker_queries(
    speaker: EventSpeaker,
    *,
    include_terms: list[str] | None = None,
    exclude_terms: list[str] | None = None,
) -> list[str]:
    name_unquoted = clean_spaces(speaker.name)
    name_quoted = _quote(speaker.name)
    queries = []
    
    include_clause = " ".join(_quote(term) for term in (include_terms or []) if clean_spaces(term))
    exclude_clause = " ".join(f'-{_quote(term)}' for term in (exclude_terms or []) if clean_spaces(term))
    
    # Try unquoted name first for max recall (Google handles names well), then fallback to quoted
    for name in [name_unquoted, name_quoted]:
        if speaker.company:
            queries.append(f"site:linkedin.com/in {name} {_quote(speaker.company)}")
        if speaker.designation:
            queries.append(f"site:linkedin.com/in {name} {_quote(speaker.designation)}")
        queries.append(f"site:linkedin.com/in {name} {include_clause} {exclude_clause}".strip())
    return list(dict.fromkeys(queries))


def choose_speaker_match(speaker: EventSpeaker, results: Iterable[SearchResult]) -> EventSpeaker:
    candidates: dict[str, tuple[float, list[str]]] = {}
    for result in results:
        canonical = normalize_linkedin_url(result.href)
        if not canonical:
            continue
        parsed_name, parsed_designation, parsed_company = parse_profile_fields(result.title, result.body)
        name_points, name_evidence = _name_score(speaker.name, parsed_name)
        if not name_points:
            continue
        candidate_text = " ".join([result.title, parsed_designation, parsed_company])
        score = name_points
        evidence = [name_evidence]
        if speaker.company and company_matches(parsed_company, speaker.company):
            score += 0.30
            evidence.append("company_match")
        if _designation_matches(speaker.designation, candidate_text):
            score += 0.20
            evidence.append("designation_match")
        current = candidates.get(canonical)
        if current is None or score > current[0]:
            candidates[canonical] = (min(score, 1.0), evidence)

    ranked = sorted(candidates.items(), key=lambda item: (-item[1][0], item[0]))
    if not ranked:
        return _with_match(speaker, "", "not_found", 0.0, "no eligible personal LinkedIn result")

    top_url, (top_score, top_evidence) = ranked[0]
    margin = top_score - ranked[1][1][0] if len(ranked) > 1 else 1.0
    evidence_text = "; ".join([*top_evidence, f"candidate={top_url}", f"margin={margin:.2f}"])
    if top_score >= AUTO_MATCH_THRESHOLD and margin >= AUTO_MATCH_MARGIN:
        return _with_match(speaker, top_url, "matched", top_score, evidence_text)
    return _with_match(speaker, "", "ambiguous", top_score, evidence_text)


def speakers_frame(speakers: list[EventSpeaker]) -> pd.DataFrame:
    return pd.DataFrame(
        [speaker.as_dict() for speaker in speakers],
        columns=[
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
        ],
    )


def write_speaker_exports(speakers: list[EventSpeaker], output_base: Path | str) -> tuple[Path, Path]:
    base = Path(output_base)
    base.parent.mkdir(parents=True, exist_ok=True)
    csv_path = base.with_suffix(".csv")
    xlsx_path = base.with_suffix(".xlsx")
    frame = speakers_frame(speakers)
    frame.to_csv(csv_path, index=False)
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Speakers", index=False)
    return csv_path, xlsx_path


def count_status(speakers: list[EventSpeaker], status: str) -> int:
    return sum(1 for speaker in speakers if speaker.match_status == status)


def _people_datasets(html: str) -> list[tuple[str, list[dict[str, Any]]]]:
    datasets: list[tuple[str, list[dict[str, Any]]]] = []
    decoded_flight = _next_flight_text(html)
    for source_text in (decoded_flight, html):
        if source_text:
            datasets.extend(_marker_datasets(source_text))
    datasets.extend(_json_ld_datasets(html))
    # Divi/Elementor speaker-overlay cards (e.g. marketing-interactive.com)
    divi_rows = _divi_speaker_overlay_datasets(html)
    if divi_rows:
        datasets.append(("divi_speaker_overlay", divi_rows))
    # Generic HTML speaker card grids (repeated article/li/div with heading+text)
    if not datasets:
        card_rows = _html_speaker_card_datasets(html)
        if card_rows:
            datasets.append(("html_speaker_cards", card_rows))
    # Visible LinkedIn /in/ links — uses per-link sibling context resolver
    visible_rows = _visible_linkedin_rows(html)
    if visible_rows:
        datasets.append(("visible_linkedin_links", visible_rows))
    return datasets


def _marker_datasets(text: str) -> list[tuple[str, list[dict[str, Any]]]]:
    datasets: list[tuple[str, list[dict[str, Any]]]] = []
    decoder = json.JSONDecoder()
    for marker_name in PEOPLE_DATASET_MARKERS:
        marker = f'"{marker_name}":'
        position = 0
        while True:
            index = text.find(marker, position)
            if index < 0:
                break
            start = index + len(marker)
            try:
                value, end = decoder.raw_decode(text[start:].lstrip())
            except json.JSONDecodeError:
                position = start
                continue
            rows = _rows_from_payload(value)
            if _looks_like_people_rows(rows):
                datasets.append((marker_name, rows))
            position = start + end
    return datasets


def _rows_from_payload(value: object) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if not isinstance(value, dict):
        return []
    for key in ("data", "items", "results", "nodes", "records", "speakers", "people", "participants"):
        rows = value.get(key)
        if isinstance(rows, list):
            return [item for item in rows if isinstance(item, dict)]
    edges = value.get("edges")
    if isinstance(edges, list):
        return [edge["node"] for edge in edges if isinstance(edge, dict) and isinstance(edge.get("node"), dict)]
    return []


def _looks_like_people_rows(rows: list[dict[str, Any]]) -> bool:
    return any(_person_name(row) or _linkedin_from_raw(row) for row in rows[:12])


def _json_ld_datasets(html: str) -> list[tuple[str, list[dict[str, Any]]]]:
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = script.string or script.get_text() or ""
        if not text.strip():
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        for item in _iter_json_objects(payload):
            raw_type = item.get("@type") or item.get("type")
            types = raw_type if isinstance(raw_type, list) else [raw_type]
            if any(str(value).lower() == "person" for value in types):
                rows.append(item)
    return [("json_ld_person", rows)] if rows else []


def _iter_json_objects(value: object) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_json_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_json_objects(child)


def _visible_linkedin_rows(html: str) -> list[dict[str, Any]]:
    """Extract rows for pages where LinkedIn /in/ links are directly visible.

    Strategy order (first match wins per link):
    1. Wix Pro Gallery / self-wrapped anchors: anchor text itself is
       "Name Title at Company" — parse directly from anchor text.
    2. Sibling context: walk backwards through DOM siblings to find the
       nearest preceding heading (name) for merged-card layouts.
    3. Nearest-card fallback: original parent-walk heuristic.
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for anchor in soup.select("a[href*='linkedin.com/in/']"):
        href = str(anchor.get("href", ""))
        canonical = normalize_linkedin_url(href)
        if not canonical or canonical in seen_urls:
            continue
        seen_urls.add(canonical)

        name, designation, company = "", "", ""

        # Strategy 1: anchor text self-contained (Wix Pro Gallery, etc.)
        anchor_text = anchor.get_text(" ", strip=True)
        if anchor_text:
            name, designation, company = _parse_anchor_text_speaker(anchor_text)

        # Strategy 2: sibling context resolver
        if not name:
            name, designation, company = _sibling_context(anchor)

        # Strategy 3: nearest-card fallback
        if not name:
            card = _nearest_person_card(anchor)
            title = _card_title(card) or anchor_text
            body = card.get_text(" ", strip=True) if card is not None else anchor_text
            name, designation, company = parse_profile_fields(title, body)

        rows.append(
            {
                "name": name,
                "designation": designation,
                "company": company,
                "linkedinProfile": canonical,
                "sourceEvidence": f"linkedin_link:{canonical}",
            }
        )
    return rows


def _parse_anchor_text_speaker(text: str) -> tuple[str, str, str]:
    """Parse "Name Title at Company" or "Name · Title at Company" anchor text.

    Wix Pro Gallery links wrap the whole card as an anchor:
      "Sushil Katdare CX Director, GCI & ViiV at GSK"
      "Ishan Gupta CEO at iMark Infotech"

    Strategy:
      1. Split off company on last " at ".
      2. If "·" separator exists, split name/designation there.
      3. Otherwise take first 2 capitalised words as the name (standard
         first-last), with a look-ahead for 3-part names.
    """
    text = clean_spaces(text.replace("\u00b7", "·"))
    if not text:
        return "", "", ""

    company = ""
    rest = text
    if " at " in text:
        parts = text.rsplit(" at ", 1)
        rest = parts[0].strip()
        company = parts[1].strip()

    if "·" in rest:
        name_part, _, title_part = rest.partition("·")
        name = name_part.strip()
        designation = title_part.strip()
    else:
        _TITLE_WORDS = {
            "ceo", "cto", "cmo", "cfo", "coo", "cpo", "cio", "cdo", "cro", "chro",
            "head", "director", "manager", "founder", "co-founder", "cofounder",
            "president", "vice", "senior", "chief", "group", "global", "regional",
            "partner", "lead", "officer", "executive",
        }
        words = rest.split()
        if len(words) <= 2:
            name = rest
            designation = ""
        elif words[0][0].isupper() and words[1][0].isupper():
            w2_lower = words[2].lower().strip(".,") if len(words) >= 3 else ""
            if (
                len(words) >= 4
                and words[2][0].isupper()
                and w2_lower not in _TITLE_WORDS
                and "," not in words[2]
                and len(words[2]) <= 14
                and not words[3][0].isupper()
            ):
                name = " ".join(words[:3])
                designation = " ".join(words[3:])
            else:
                name = " ".join(words[:2])
                designation = " ".join(words[2:])
        else:
            name = words[0]
            designation = " ".join(words[1:])

    if not _valid_person_name(name):
        return "", "", ""
    return name, clean_spaces(designation), clean_spaces(company)


def _sibling_context(anchor) -> tuple[str, str, str]:
    """Walk backwards through siblings to find a heading that acts as the
    speaker name for this LinkedIn anchor, then extract designation/company
    from sibling text nodes that follow the heading.

    Returns (name, designation, company) strings; empty strings if not found.
    """
    parent = getattr(anchor, "parent", None)
    if parent is None:
        return "", "", ""

    # Collect all sibling elements before this anchor inside the parent
    siblings_before: list = []
    for sib in parent.children:
        if sib is anchor:
            break
        siblings_before.append(sib)

    # Walk backwards to find the nearest heading
    name = ""
    heading_el = None
    for sib in reversed(siblings_before):
        tag = getattr(sib, "name", None)
        if tag in {"h1", "h2", "h3", "h4", "h5", "strong"}:
            candidate = sib.get_text(" ", strip=True)
            if _valid_person_name(candidate):
                name = candidate
                heading_el = sib
                break
        # Also check if anchor itself wraps text that looks like a name
        anchor_text = anchor.get_text(" ", strip=True)
        if _valid_person_name(anchor_text):
            name = anchor_text
            break

    if not name:
        return "", "", ""

    # Collect text nodes/elements after the heading and before the next heading
    # to extract designation + company
    after_text: list[str] = []
    found_heading = heading_el is None  # if we used anchor text, skip heading search
    for sib in parent.children:
        if sib is heading_el:
            found_heading = True
            continue
        if not found_heading:
            continue
        if sib is anchor:
            break
        tag = getattr(sib, "name", None)
        if tag in {"h1", "h2", "h3", "h4", "h5", "strong"}:
            # Next speaker block starts here — stop
            break
        text = getattr(sib, "get_text", lambda **_: str(sib))(' ', strip=True)
        if isinstance(text, str) and text:
            after_text.append(text)

    combined = " ".join(after_text)
    _, designation, company = parse_profile_fields(name, combined)
    return name, designation, company


def _nearest_person_card(anchor) -> Any:
    for parent in anchor.parents:
        name = getattr(parent, "name", "")
        classes = " ".join(parent.get("class", []) if hasattr(parent, "get") else [])
        class_text = normalize_text(classes)
        if name in {"article", "li", "section"}:
            return parent
        if any(token in class_text for token in ("speaker", "person", "profile", "card", "team", "member")):
            return parent
    return getattr(anchor, "parent", None)


def _card_title(card) -> str:
    if card is None:
        return ""
    for selector in ("h1", "h2", "h3", "h4", "[class*='name']", "[class*='title']"):
        found = card.select_one(selector) if hasattr(card, "select_one") else None
        if found:
            text = found.get_text(" ", strip=True)
            if text:
                return text
    return ""


def _speaker_from_raw(
    raw: dict[str, Any],
    source_url: str,
    dataset_name: str,
    index: int,
) -> EventSpeaker | None:
    if not _is_active(raw):
        return None
    name = _person_name(raw)
    if not _valid_person_name(name):
        return None
    company = _text_from_keys(
        raw,
        (
            "companyName",
            "company",
            "organisation",
            "organization",
            "org",
            "employer",
            "worksFor",
            "affiliation",
            "currentCompany",
        ),
    )
    designation = _text_from_keys(
        raw,
        (
            "desgination",
            "designation",
            "jobTitle",
            "title",
            "position",
            "role",
            "headline",
        ),
    )
    parsed_name, parsed_designation, parsed_company = parse_profile_fields(
        name, " ".join(value for value in (designation, company) if value)
    )
    if parsed_name and normalize_text(parsed_name) not in {"unknown", "linkedin member"}:
        name = parsed_name
    designation = designation or parsed_designation
    company = company or parsed_company
    country = _country_value(
        _value_from_keys(raw, ("country", "countryName", "location", "address", "city"))
    )
    canonical = _linkedin_from_raw(raw)
    speaker_id = _text_from_keys(
        raw,
        ("speakerId", "personId", "memberId", "profileId", "documentId", "id", "slug"),
    )
    if not speaker_id:
        speaker_id = linkedin_id(canonical) or f"{dataset_name}:{index}:{normalize_text(name)}"
    return EventSpeaker(
        speaker_id=f"{dataset_name}:{speaker_id}",
        name=name,
        designation=designation,
        company=company,
        country=country,
        linkedin_url=canonical,
        match_status="provided" if canonical else "not_found",
        confidence=1.0 if canonical else 0.0,
        match_evidence=f"{dataset_name} LinkedIn field" if canonical else f"{dataset_name}: no provided LinkedIn URL",
        source_url=source_url,
    )


def _is_active(raw: dict[str, Any]) -> bool:
    for key in ("isActive", "active", "enabled", "published"):
        value = _value_from_keys(raw, (key,))
        if str(value).lower() in {"false", "0", "no"}:
            return False
    status = clean_spaces(str(_value_from_keys(raw, ("status",)) or ""))
    if normalize_text(status) in {"inactive", "draft", "disabled", "hidden", "deleted"}:
        return False
    return True


def _person_name(raw: dict[str, Any]) -> str:
    name = _text_from_keys(
        raw,
        ("fullName", "speakerName", "personName", "displayName", "name"),
    )
    if name:
        return name
    first = _text_from_keys(raw, ("firstName", "givenName"))
    last = _text_from_keys(raw, ("lastName", "familyName", "surname"))
    return clean_spaces(f"{first} {last}")


def _valid_person_name(value: str) -> bool:
    key = normalize_text(value)
    if key in {"", "linkedin", "speaker", "profile", "team", "person"}:
        return False
    words = key.split()
    return 1 <= len(words) <= 8 and not any(char.isdigit() for char in value)


def _linkedin_from_raw(raw: dict[str, Any]) -> str:
    for value in _values_from_keys(
        raw,
        (
            "linkedinProfile",
            "linkedInProfile",
            "linkedinUrl",
            "linkedInUrl",
            "linkedinURL",
            "linkedin_url",
            "linkedin",
            "linkedIn",
            "sameAs",
            "url",
            "profileUrl",
        ),
    ):
        for candidate in _flatten_values(value):
            canonical = normalize_linkedin_url(candidate)
            if canonical:
                return canonical
    raw_json = json.dumps(raw, default=str)
    for match in re_find_linkedin_urls(raw_json):
        canonical = normalize_linkedin_url(match)
        if canonical:
            return canonical
    return ""


def re_find_linkedin_urls(text: str) -> list[str]:
    return re_linkedin_url_pattern().findall(text)


def re_linkedin_url_pattern():
    import re

    return re.compile(r"https?://(?:[\w.-]+\.)?linkedin\.com/in/[A-Za-z0-9_.%-]+/?", re.IGNORECASE)


def _text_from_keys(raw: dict[str, Any], keys: tuple[str, ...]) -> str:
    value = _value_from_keys(raw, keys)
    return clean_spaces(_value_to_text(value))


def _value_from_keys(raw: dict[str, Any], keys: tuple[str, ...]) -> object:
    values = list(_values_from_keys(raw, keys))
    return values[0] if values else ""


def _values_from_keys(raw: dict[str, Any], keys: tuple[str, ...]) -> Iterable[object]:
    wanted = {_key_id(key) for key in keys}
    for key, value in raw.items():
        if _key_id(str(key)) in wanted:
            yield value
    for key, value in raw.items():
        if _key_id(str(key)) not in {"speaker", "person", "profile", "user", "member", "author"}:
            continue
        if isinstance(value, dict):
            yield from _values_from_keys(value, keys)


def _key_id(value: str) -> str:
    return normalize_text(value).replace(" ", "")


def _value_to_text(value: object) -> str:
    if isinstance(value, dict):
        for key in (
            "name",
            "fullName",
            "companyName",
            "title",
            "label",
            "country",
            "countryName",
            "addressCountry",
            "addressLocality",
        ):
            if key in value:
                return _value_to_text(value[key])
        return ""
    if isinstance(value, list):
        return clean_spaces(" ".join(_value_to_text(item) for item in value[:3]))
    return clean_spaces(str(value or ""))


def _flatten_values(value: object) -> Iterable[str]:
    if isinstance(value, dict):
        for child in value.values():
            yield from _flatten_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _flatten_values(child)
    else:
        yield str(value or "")


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


def _next_flight_text(html: str) -> str:
    chunks: list[str] = []
    prefix = "self.__next_f.push("
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.find_all("script"):
        text = (script.string or script.get_text() or "").strip()
        if not text.startswith(prefix):
            continue
        expression = text[len(prefix) :].strip()
        if expression.endswith(");"):
            expression = expression[:-2]
        elif expression.endswith(")"):
            expression = expression[:-1]
        try:
            payload = json.loads(expression)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(payload, list) and len(payload) > 1 and isinstance(payload[1], str):
            chunks.append(payload[1])
    return "".join(chunks)


def _speaker_payload(decoded_flight: str) -> dict[str, Any]:
    marker = '"allSpeakersData":'
    decoder = json.JSONDecoder()
    position = 0
    while True:
        index = decoded_flight.find(marker, position)
        if index < 0:
            break
        start = index + len(marker)
        try:
            value, _end = decoder.raw_decode(decoded_flight[start:].lstrip())
        except json.JSONDecodeError:
            position = start
            continue
        if isinstance(value, dict) and isinstance(value.get("data"), list):
            return value
        position = start
    raise NoSpeakersFoundError("No supported allSpeakersData payload was found")


def _country_value(value: object) -> str:
    if isinstance(value, dict):
        return clean_spaces(
            str(
                value.get("country")
                or value.get("name")
                or value.get("countryName")
                or value.get("addressCountry")
                or value.get("addressLocality")
                or ""
            )
        )
    return clean_spaces(str(value or ""))


def _quote(value: str) -> str:
    return '"' + clean_spaces(value).replace('"', "'") + '"'


def _name_score(source: str, candidate: str) -> tuple[float, str]:
    source_tokens = _name_tokens(source)
    candidate_tokens = _name_tokens(candidate)
    if not source_tokens or not candidate_tokens:
        return 0.0, ""
    if source_tokens == candidate_tokens:
        return 0.60, "exact_name"
    s_last = source_tokens[-1]
    c_last = candidate_tokens[-1]
    if s_last != c_last and not (s_last.startswith(c_last) or c_last.startswith(s_last)):
        return 0.0, ""
    if len(source_tokens) == len(candidate_tokens) and all(
        left == right
        or (len(left) == 1 and right.startswith(left))
        or (len(right) == 1 and left.startswith(right))
        for left, right in zip(source_tokens, candidate_tokens, strict=False)
    ):
        return 0.50, "similar_name"
    overlap = len(set(source_tokens) & set(candidate_tokens)) / max(len(set(source_tokens)), 1)
    if overlap >= 0.80:
        return 0.50, "similar_name"
    return 0.0, ""


def _name_tokens(value: str) -> list[str]:
    honorifics = {"dr", "mr", "mrs", "ms", "prof", "shri", "smt"}
    return [token for token in normalize_text(value).split() if token not in honorifics]


def _designation_matches(designation: str, candidate_text: str) -> bool:
    role_stopwords = {"a", "an", "and", "at", "for", "global", "in", "of", "on", "senior", "the", "to"}
    source_tokens = [
        token
        for token in normalize_text(designation).split()
        if token not in role_stopwords and len(token) > 1
    ]
    if not source_tokens:
        return False
    candidate_tokens = set(normalize_text(candidate_text).split())
    overlap = sum(token in candidate_tokens for token in source_tokens)
    required = 1 if len(source_tokens) == 1 else 2
    return overlap >= required and overlap / len(source_tokens) >= 0.50


def _with_match(
    speaker: EventSpeaker,
    linkedin_url: str,
    status: str,
    confidence: float,
    evidence: str,
) -> EventSpeaker:
    return EventSpeaker(
        speaker_id=speaker.speaker_id,
        name=speaker.name,
        designation=speaker.designation,
        company=speaker.company,
        country=speaker.country,
        linkedin_url=normalize_linkedin_url(linkedin_url),
        match_status=status,
        confidence=round(float(confidence), 2),
        match_evidence=evidence,
        source_url=speaker.source_url,
    )


def _emit(progress: ProgressCallback | None, event: str, **payload: object) -> None:
    if progress:
        progress({"event": event, **payload})


# ─────────────────────────────────────────────────────────────────────────────
# New parser: Divi / Elementor speaker-overlay cards
# Pattern: <div class="*speaker-overlay*"> or <div class="*speaker-box*">
#   heading[0] → name
#   heading[1] → designation
#   heading[2] → company
# Found on: conferences.marketing-interactive.com and similar Divi sites
# ─────────────────────────────────────────────────────────────────────────────

_OVERLAY_CLASS_TOKENS = (
    "speaker-overlay",
    "speaker-box",
    "speaker-card",
    "speaker-item",
    "speaker-profile",
    "panelist-card",
    "panelist-overlay",
    "presenter-card",
)


def _divi_speaker_overlay_datasets(html: str) -> list[dict[str, Any]]:
    """Extract speaker records from Divi/Elementor overlay-card pages.

    Each matching section div contains three consecutive headings:
      h3/h4[0] → speaker name
      h3/h4[1] → designation (may include " at Company" suffix)
      h3/h4[2] → company name (standalone)
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []

    # Build CSS selector matching any of the overlay class tokens
    selector = ", ".join(f"[class*='{token}']" for token in _OVERLAY_CLASS_TOKENS)
    cards = soup.select(selector)

    for card in cards:
        headings = [
            el.get_text(" ", strip=True)
            for el in card.find_all(["h1", "h2", "h3", "h4", "h5", "strong"])
            if el.get_text(strip=True)
        ]
        if not headings:
            continue
        name = headings[0] if _valid_person_name(headings[0]) else ""
        if not name:
            continue

        designation = ""
        company = ""
        if len(headings) >= 2:
            designation = headings[1]
        if len(headings) >= 3:
            company = headings[2]

        # If designation contains " at Company" pattern, split it
        if " at " in designation and not company:
            parts = designation.rsplit(" at ", 1)
            designation = parts[0].strip()
            company = parts[1].strip()

        # LinkedIn links within this card
        li_url = ""
        for a in card.find_all("a", href=True):
            canonical = normalize_linkedin_url(str(a["href"]))
            if canonical:
                li_url = canonical
                break

        rows.append(
            {
                "name": name,
                "designation": designation,
                "company": company,
                "linkedinProfile": li_url,
                "sourceEvidence": f"divi_overlay:{name}",
            }
        )
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# New parser: Generic HTML speaker card grid
# Pattern: repeated <article>, <li>, or <div> siblings inside a container
#   that each have a short heading + optional short paragraph
# Used as last-resort fallback for custom-CSS conference pages
# ─────────────────────────────────────────────────────────────────────────────

_CARD_CLASS_TOKENS = (
    "speaker", "panelist", "presenter", "person",
    "profile", "team-member", "member", "lineup",
)
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5"}


def _html_speaker_card_datasets(html: str) -> list[dict[str, Any]]:
    """Generic fallback: find repeated block elements with class names that
    suggest speaker/person cards, each containing a heading (name) and
    optional short text (designation/company).
    """
    soup = BeautifulSoup(html, "html.parser")
    rows: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    # Find candidate containers by class keyword
    selector = ", ".join(
        f"article[class*='{t}'], li[class*='{t}'], div[class*='{t}']"
        for t in _CARD_CLASS_TOKENS
    )
    cards = soup.select(selector)

    for card in cards:
        text = card.get_text(" ", strip=True)
        # Skip very long elements (they're containers, not individual cards)
        if len(text) > 1200:
            continue
        headings = [
            el.get_text(" ", strip=True)
            for el in card.find_all(list(_HEADING_TAGS))
            if el.get_text(strip=True)
        ]
        if not headings:
            continue
        name = headings[0] if _valid_person_name(headings[0]) else ""
        if not name or name in seen_names:
            continue
        seen_names.add(name)

        # Extract designation + company from heading[1] / heading[2] or body text
        designation, company = "", ""
        if len(headings) >= 2:
            _, designation, company = parse_profile_fields(name, headings[1])
        if not designation:
            _, designation, company = parse_profile_fields(name, text)

        li_url = ""
        for a in card.find_all("a", href=True):
            canonical = normalize_linkedin_url(str(a["href"]))
            if canonical:
                li_url = canonical
                break

        rows.append(
            {
                "name": name,
                "designation": designation,
                "company": company,
                "linkedinProfile": li_url,
                "sourceEvidence": f"html_card:{name}",
            }
        )
    return rows
