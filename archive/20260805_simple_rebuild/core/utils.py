import re
import unicodedata
import urllib.parse


def _join(items):
    return " OR ".join([f'"{i}"' if ' ' in i else i for i in items])

def _q(i):
    return f'"{i}"' if ' ' in i else i

def normalize_text(value):
    """Normalize text for deterministic, punctuation-insensitive matching."""
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text)
    text = text.replace("&", " and ")
    text = re.sub(r"[\u2010-\u2015]", "-", text)
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()

def normalize_linkedin_url(url):
    """Canonicalize any LinkedIn profile URL to a single dedupe format."""
    if not url:
        return ""
    value = str(url).strip()
    if "://" not in value:
        value = f"https://{value.lstrip('/')}"
    parsed = urllib.parse.urlparse(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host != "linkedin.com" and not host.endswith(".linkedin.com"):
        return ""
    match = re.match(
        r"^/(?:in|mwlite/profile/in)/([^/?#]+)",
        parsed.path,
        re.IGNORECASE,
    )
    if not match:
        return ""
    slug = match.group(1).strip().strip("/").lower()
    if normalize_text(slug) in {"404", "pub", "jobs", "job", "pulse", "feed", "posts", "company"}:
        return ""
    return f"https://www.linkedin.com/in/{slug}/"

def person_identity_key(name, company):
    """Stable fallback identity for files that lack a usable LinkedIn URL."""
    name_key = normalize_text(name)
    company_key = normalize_text(company)
    invalid = {"", "unknown", "none", "nan", "na", "n a"}
    if name_key in invalid or company_key in invalid:
        return ""
    return f"{name_key}|{company_key}"


def is_export_ready_profile(name, company, linkedin_url, designation=None):
    """Return whether a profile has the identity fields required for export.

    ``designation`` is optional for backwards compatibility. Harvesting callers
    pass it so a profile without a verifiable current title cannot be exported
    merely because a name, company, and URL were found.
    """
    invalid = {"", "unknown", "none", "nan", "na", "n a", "null"}
    name_key = normalize_text(name)
    company_key = normalize_text(company)
    if name_key in invalid or company_key in invalid | {
        "linkedin", "professional profile", "linkedin profile",
    }:
        return False
    if re.search(r"\d|@", str(name or "")) or len(name_key.split()) > 8:
        return False
    if designation is not None and normalize_text(designation) in invalid:
        return False
    if designation is not None:
        designation_text = str(designation or "")
        if (
            len(normalize_text(designation_text).split()) > 20
            or re.search(
                r"\b(?:i(?:'m| am)\s+hiring|hiring|looking for|job opening|"
                r"show more|posted by)\b",
                designation_text,
                flags=re.IGNORECASE,
            )
        ):
            return False
    if not normalize_linkedin_url(linkedin_url):
        return False
    # Truncated search titles occasionally produce values such as
    # ``Talent Acquisition at``. These are role fragments, not companies.
    if re.search(r"(?:\bat\b|@)\s*$", str(company or "").strip(), re.IGNORECASE):
        return False
    if re.search(
        r"\b(?:seeking|looking|hiring|searching|wanted|join|apply|opportunity|"
        r"openings?|vacancies?|the|a|an|for|to)\s*$",
        str(company or "").strip(),
        re.IGNORECASE,
    ):
        return False
    if (
        len(company_key.split()) > 12
        or re.search(
            r"\b(?:show more|one of the|i lead|i oversee|posted by|"
            r"professional community)\b",
            str(company or ""),
            flags=re.IGNORECASE,
        )
    ):
        return False
    return True


def split_csv_terms(raw):
    """Split comma-separated user keywords into a clean list."""
    if not raw:
        return []
    return [term.strip() for term in str(raw).split(",") if term.strip()]

def term_in_text(text, term):
    """Match a single term against normalized text.
    Handles:
    - Single words: word-boundary anchored to prevent e.g. 'delhi' matching 'delhivery'
    - Multi-word phrases: exact phrase match or a spaceless variant
      e.g. 'Rabbitt AI' will match 'Rabbitt AI' or 'rabbittai', not just 'Rabbitt'
    """
    text_norm = normalize_text(text)
    term_norm = normalize_text(term)
    if not text_norm or not term_norm:
        return False

    if " " in term_norm:
        # Try exact phrase match first
        if term_norm in text_norm:
            return True
        # Try spaceless variant (e.g. 'rabbitt ai' -> 'rabbittai')
        spaceless = term_norm.replace(" ", "")
        if spaceless in text_norm.replace(" ", ""):
            return True
        return False

    # Word-boundary anchored match — prevents partial token false positives
    return bool(re.search(r'(?<![a-z0-9])' + re.escape(term_norm) + r'(?![a-z0-9])', text_norm))

def any_term_matches(text, terms):
    """Return True when any term matches the supplied text."""
    return any(term_in_text(text, term) for term in terms if term)

def matching_terms(text, terms):
    """Return selected terms that match, preserving their input order."""
    return list(dict.fromkeys(term for term in terms if term and term_in_text(text, term)))

def combined_text(*parts):
    return " ".join(str(part) for part in parts if part)

def lead_signal_text(lead):
    """Build a searchable signal string from structured lead fields."""
    signal_phrases = []
    if str(lead.get("Conference_Speaker", "")).lower() == "yes":
        signal_phrases.append("speaker keynote keynote speaker panelist conference speaker summit speaker tedx panel discussion")
    if str(lead.get("Roundtable_Participation", "")).lower() == "yes":
        signal_phrases.append("roundtable cxo roundtable leadership summit executive roundtable boardroom cxo forum leadership forum c suite roundtable")
    if str(lead.get("Podcast_Participation", "")).lower() == "yes":
        signal_phrases.append("podcast interview")

    return combined_text(
        lead.get("Awards", ""),
        lead.get("Recent_News", ""),
        lead.get("Buying_Signals", ""),
        lead.get("Lead_Source", ""),
        lead.get("Notes", ""),
        lead.get("Company_Stage", ""),
        lead.get("Public_Private", ""),
        lead.get("Funding_Raised", ""),
        lead.get("AI_Adoption_Signals", ""),
        " ".join(signal_phrases),
    )

def company_name_variants(company_name):
    """Generate punctuation/legal-suffix aliases without broadening the brand.

    Multi-token companies deliberately do not get a first-word alias.  Treating
    ``Apollo.io`` as simply ``Apollo`` (or ``Tata Consultancy Services`` as
    simply ``Tata``) can associate a person with a different company.
    """
    base = normalize_text(company_name)
    if not base:
        return []
    variants = {company_name, base}
    tokens = [
        t for t in base.split()
        if t not in {
            "india", "private", "limited", "ltd", "ltds", "pvt", "company",
            "co", "inc", "incorporated", "corporation", "corp", "llp", "plc",
        }
    ]
    if tokens:
        variants.add(" ".join(tokens))
        # Spaceless concatenation supports punctuation/domain brand variants,
        # e.g. ``Apollo.io`` and ``ApolloIO``, without matching plain Apollo.
        spaceless = "".join(tokens)
        if len(spaceless) >= 4:
            variants.add(spaceless)
    return [v for v in variants if v]


def score_web_profile(role_hit, location_hit, industry_hit, signal_score, custom_hit,
                      company_known, business_model_hit=False):
    """Deterministic score for web-harvested profiles."""
    score = signal_score
    if role_hit:
        score += 12
    if location_hit:
        score += 10
    if industry_hit:
        score += 5
    if custom_hit:
        score += 8
    if company_known:
        score += 2
    if business_model_hit:
        score += 6
    return max(50, min(99, int(score)))

def parameter_match_summary(role_hit, location_hit, industry_hit, signal_hit=None, custom_hit=None):
    """Human-readable filter match summary for each harvested lead."""
    parts = [
        f"Role={'Yes' if role_hit else 'Check'}",
        f"Location={'Yes' if location_hit else 'Check'}",
        f"Industry={'Yes' if industry_hit else 'Check'}",
    ]
    if signal_hit is not None:
        parts.append(f"Signal={'Yes' if signal_hit else 'Check'}")
    if custom_hit is not None:
        parts.append(f"Custom={'Yes' if custom_hit else 'Check'}")
    return " | ".join(parts)


_ROLE_TITLE_TERMS = (
    "ceo", "chief executive officer", "founder", "co founder",
    "managing director", "md", "president", "vice president", "vp", "svp",
    "avp", "director", "chief", "cmo", "cto", "cio", "cfo", "coo", "cpo",
    "cro", "chro", "ciso", "cdo", "head", "partner", "manager", "officer",
    "chairman", "chairperson", "chair", "owner", "principal", "lead",
)

_PROFILE_TITLE_SUFFIX_RE = re.compile(
    r"\s*\|\s*(?:LinkedIn|Professional Profile)\b.*$",
    flags=re.IGNORECASE,
)


def _clean_profile_fragment(value, preserve_pipes=False):
    """Clean one search-title field without inventing missing identity data."""
    separators = r"[·•\n]" if preserve_pipes else r"[·•|\n]"
    fragment = re.split(separators, str(value or ""), maxsplit=1)[0]
    fragment = re.sub(r"\s+", " ", fragment).strip(" \t\r\n-–—,;:")
    return fragment


def _clean_company_fragment(value):
    """Remove common LinkedIn employer taglines without shortening real brands."""
    fragment = _clean_profile_fragment(value)
    # Google sometimes concatenates a company brand with a recruiting slogan,
    # e.g. ``Alstom Recruiting the Future, Today``. Sending that entire slogan
    # into evidence search produces unrelated queries and false rejections.
    fragment = re.sub(
        r"\s+(?:recruiting|hiring)\s+"
        r"(?:the|for|our|a|an)\b.*$",
        "",
        fragment,
        flags=re.IGNORECASE,
    )
    return fragment.strip(" \t\r\n-–—,;:")


def _looks_like_designation(value):
    """Return whether a title fragment has an explicit job-role marker."""
    return any_term_matches(value, _ROLE_TITLE_TERMS)


def _looks_like_profile_location(value):
    """Identify common LinkedIn location-only title fragments."""
    normalized = normalize_text(value)
    if not normalized:
        return False
    countries = (
        "india", "united states", "usa", "united kingdom", "uk", "singapore",
        "united arab emirates", "uae", "canada", "australia",
    )
    return "," in str(value) and any(term_in_text(normalized, country) for country in countries)


def _split_role_and_company(value):
    """Parse an explicit ``Role at Company`` fragment."""
    # A LinkedIn headline may contain pipe-separated descriptors before the
    # actual current role, e.g. "Talent Leader | Head of TA at CloudSEK".
    segments = [
        segment.strip()
        for segment in re.split(r"\s*[|·•]\s*", str(value or ""))
        if segment.strip()
    ]
    for segment in reversed(segments or [str(value or "")]):
        match = re.match(
            r"^\s*(?P<role>.+?)\s+(?:at|@)\s+(?P<company>.+?)\s*$",
            segment,
            flags=re.IGNORECASE,
        )
        if not match or not _looks_like_designation(match.group("role")):
            continue
        role_value = re.sub(
            r"^\s*(?:Current|Title)\s*:\s*",
            "",
            match.group("role"),
            flags=re.IGNORECASE,
        )
        return (
            _clean_profile_fragment(role_value),
            _clean_profile_fragment(match.group("company")),
        )
    return "", ""


def _profile_marker_matches(body, person_name):
    """Check whether LinkedIn's ``View X's profile`` marker matches this URL card."""
    marker = re.search(
        r"\bView\s+(.{1,100}?)[’']s\s+profile\s+on\s+LinkedIn\b",
        str(body or "")[:700],
        flags=re.IGNORECASE,
    )
    if not marker or not normalize_text(person_name):
        return True
    marker_name = normalize_text(marker.group(1))
    expected_name = normalize_text(person_name)
    return (
        marker_name == expected_name
        or marker_name.startswith(f"{expected_name} ")
        or expected_name.startswith(f"{marker_name} ")
    )


def _structured_experience_company(body, person_name=""):
    """Extract LinkedIn's first Experience value when current-profile markers exist."""
    body_text = str(body or "")
    if not _profile_marker_matches(body_text, person_name):
        return ""
    # A bare "Experience:" mention can be historical text from an arbitrary
    # result. LinkedIn's public profile card normally also exposes Location or
    # connection metadata, which makes the first Experience value materially
    # stronger current-company evidence.
    has_profile_schema = bool(re.search(
        r"\bLocation\s*:|\b\d[\d,+]*\s+connections?\s+on\s+LinkedIn\b",
        body_text,
        flags=re.IGNORECASE,
    ))
    if not has_profile_schema:
        return ""
    match = re.search(
        r"\bExperience\s*:\s*([^·•|\n]{2,120})",
        body_text[:420],
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    company = _clean_profile_fragment(match.group(1))
    return company if company and not _looks_like_designation(company) else ""


def _body_current_role_company(body):
    """Extract an explicitly current role/company from the profile-card lead text."""
    text = re.sub(r"\s+", " ", str(body or "")).strip()
    if not text:
        return "", ""

    # Search providers occasionally concatenate several LinkedIn cards. Only
    # inspect the first card-sized window associated with the result URL.
    lead = re.split(
        r"\b(?:Experience|Education|Location)\s*:",
        text[:600],
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0].strip(" ·•|")
    if not lead:
        return "", ""

    # Reject recruiting posts ("I'm hiring...", "Need a...") before looking
    # for role phrases; the vacancy belongs to neither the post author nor URL.
    if re.match(
        r"^(?:i(?:'m| am)\s+hiring|hiring|looking|seeking|need|wanted)\b",
        lead,
        flags=re.IGNORECASE,
    ):
        return "", ""

    # Common first-person current-position forms used in LinkedIn snippets.
    current_patterns = (
        r"^(?:currently\s+)?(?:serving|working)\s+as\s+(?:the|a|an)?\s*"
        r"(?P<role>[^,.|·]{3,120})",
        r"^as\s+(?:the|a|an)\s+(?P<role>[^,.|·]{3,120})",
        r"\bi\s+am\s+(?:currently\s+)?(?:the|a|an)\s+"
        r"(?P<role>[^,.|·]{3,120})",
    )
    for pattern in current_patterns:
        match = re.search(pattern, lead, flags=re.IGNORECASE)
        if not match:
            continue
        role = _clean_profile_fragment(match.group("role"))
        role = re.split(r"\s+(?:at|@)\s+", role, maxsplit=1, flags=re.IGNORECASE)[0]
        if _looks_like_designation(role):
            return role, ""

    # Explicit "Role at Company" headline segments are the strongest remaining
    # evidence. Keep the window small because some providers append the next
    # person's card without a reliable separator.
    for segment in re.split(r"\s*[|·•]\s*", lead[:300]):
        role, company = _split_role_and_company(segment)
        if role:
            return role, company

    # Many public cards begin directly with a pipe/bullet-delimited headline.
    first_segment = re.split(r"\s*[|·•]\s*|(?<=[.!?])\s+", lead, maxsplit=1)[0]
    first_segment = re.split(
        r"\s+[-–—]\s+(?=\d|\byears?\b|\byrs?\b)",
        first_segment,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    first_segment = _clean_profile_fragment(first_segment)
    if len(first_segment) <= 140 and _looks_like_designation(first_segment):
        return first_segment, ""
    return "", ""


def _leading_google_profile_fields(body, person_name="", designation_hint=""):
    """Parse Google's current LinkedIn card layout.

    Google commonly renders public profile citations as
    ``Location · Current role · Current company Name. Headline``. These leading
    fields are materially stronger than later free-text/history fragments.
    """
    raw_segments = [
        re.sub(r"\s+", " ", segment).strip()
        for segment in re.split(r"\s*[·•]\s*", str(body or "")[:650])
        if re.sub(r"\s+", " ", segment).strip()
    ]
    if len(raw_segments) < 2:
        return "", "", ""

    first = raw_segments[0]
    second = raw_segments[1]
    name_key = normalize_text(person_name)
    first_key = normalize_text(first)
    if (
        not first_key
        or (name_key and (
            first_key == name_key
            or first_key.startswith(f"{name_key} ")
        ))
        or _looks_like_designation(first)
    ):
        return "", "", ""

    structured_role = (
        _clean_profile_fragment(second)
        if _looks_like_designation(second)
        else ""
    )
    if not structured_role and not designation_hint:
        return "", "", ""

    company = ""
    if len(raw_segments) >= 3:
        candidate = raw_segments[2]
        cut_patterns = [
            r"\bExperience\b",
            r"\bAs\s+(?:the|a|an)\b",
        ]
        if person_name:
            name_tokens = [
                re.escape(token)
                for token in re.findall(r"[A-Za-z0-9]+", person_name)
                if token
            ]
            if name_tokens:
                cut_patterns.append(
                    r"\b" + r"[\W_]+".join(name_tokens) + r"\b"
                )
        for role_value in (structured_role, designation_hint):
            if role_value:
                cut_patterns.append(re.escape(role_value))
        cut_match = re.search(
            "|".join(f"(?:{pattern})" for pattern in cut_patterns),
            candidate,
            flags=re.IGNORECASE,
        )
        if cut_match and cut_match.start() > 0:
            candidate = candidate[:cut_match.start()]
        candidate = _clean_company_fragment(candidate)
        if (
            1 <= len(normalize_text(candidate).split()) <= 12
            and not _looks_like_designation(candidate)
            and not _looks_like_profile_location(candidate)
        ):
            company = candidate

    return first, structured_role, company


def parse_profile(title, href, body=""):
    """Extract a current name, designation and company from a LinkedIn result.

    Search snippets are incomplete and may contain historical experience. This
    parser trusts the profile title, explicit current-position phrases, and
    LinkedIn's structured profile-card fields. A bare historical
    ``Experience:`` mention is not treated as a current company.
    """
    clean_url = normalize_linkedin_url(href)
    if "linkedin.com/in/" not in clean_url:
        return None, None, None, None

    title_clean = _PROFILE_TITLE_SUFFIX_RE.sub("", str(title or "")).strip()
    title_clean = re.split(r"\.{3}|…", title_clean, maxsplit=1)[0].strip()
    parts = [
        _clean_profile_fragment(part, preserve_pipes=True)
        for part in re.split(r"\s+[-–—]\s+", title_clean)
        if _clean_profile_fragment(part, preserve_pipes=True)
    ]
    if len(parts) == 1 and "|" in title_clean:
        parts = [
            _clean_profile_fragment(part)
            for part in re.split(r"\s*\|\s*", title_clean)
            if _clean_profile_fragment(part)
        ]
    name = parts[0] if parts else "Unknown"
    designation = ""
    company = ""

    title_fields = parts[1:]

    # Prefer an explicit "Role at Company" fragment anywhere in the title.
    # LinkedIn titles often put a generic label first, for example:
    # "Jane - Talent Leader - Head of Talent Acquisition at Acme".
    for field in title_fields:
        inline_role, inline_company = _split_role_and_company(field)
        if inline_role:
            designation = inline_role
            company = inline_company
            break

    if not designation:
        for index, field in enumerate(title_fields):
            if not _looks_like_designation(field):
                continue
            designation = field
            if index + 1 < len(title_fields):
                company_candidate = title_fields[index + 1]
                if (not _looks_like_designation(company_candidate)
                        and not _looks_like_profile_location(company_candidate)):
                    company = company_candidate
            elif index > 0:
                company_candidate = title_fields[index - 1]
                if (not _looks_like_designation(company_candidate)
                        and not _looks_like_profile_location(company_candidate)):
                    company = company_candidate
            break

    if not designation and title_fields:
        company_candidate = title_fields[0]
        if (not _looks_like_designation(company_candidate)
                and not _looks_like_profile_location(company_candidate)):
            company = company_candidate

    body_text = str(body or "")
    body_role, body_company = _body_current_role_company(body_text)
    if body_role:
        # An explicit current-position sentence/headline is more complete than
        # generic title fragments such as "Talent Leader" or "AVP".
        if (not designation or body_company or
                len(normalize_text(body_role).split()) > len(normalize_text(designation).split())):
            designation = body_role
    if body_company:
        company = body_company

    _, card_role, card_company = _leading_google_profile_fields(
        body_text,
        person_name=name,
        designation_hint=designation,
    )
    if card_role:
        # Google's second leading field is the current-role field. Prefer it to
        # an aspirational or historical title headline.
        designation = card_role
    if card_company:
        company = card_company

    current_match = re.search(
        r"\bCurrent\s*:\s*([^·•|\n]{2,140})",
        body_text,
        flags=re.IGNORECASE,
    )
    if current_match and (not designation or not company):
        current_value = _clean_profile_fragment(current_match.group(1))
        current_role, current_company = _split_role_and_company(current_value)
        if not designation and current_role:
            designation = current_role
        if not company and current_company:
            company = current_company
        if (not company and current_value and not current_role
                and not _looks_like_designation(current_value)
                and not _looks_like_profile_location(current_value)):
            company = current_value

    if not designation:
        title_match = re.search(
            r"\bTitle\s*:\s*([^·•|\n]{2,100})",
            body_text,
            flags=re.IGNORECASE,
        )
        if title_match and _looks_like_designation(title_match.group(1)):
            designation = _clean_profile_fragment(title_match.group(1))

    if not company:
        company_match = re.search(
            r"\b(?:Current\s+Company|Company)\s*:\s*([^·•|\n]{2,100})",
            body_text,
            flags=re.IGNORECASE,
        )
        if company_match:
            company_candidate = _clean_profile_fragment(company_match.group(1))
            if (not _looks_like_designation(company_candidate)
                    and not _looks_like_profile_location(company_candidate)):
                company = company_candidate

    structured_company = _structured_experience_company(body_text, person_name=name)
    if structured_company:
        # LinkedIn's structured Experience value fixes truncated search titles
        # such as "Head of TA - Seeking out the ..." and completes titles that
        # expose only the current designation.
        company = structured_company

    # Clean up name.
    name = re.sub(r'\s*(linkedin|profile).*', '', name, flags=re.IGNORECASE).strip()
    designation = _clean_profile_fragment(designation)
    designation = re.sub(
        r"^(?:currently\s+(?:serving|working)\s+as|as)\s+(?:the|a|an)?\s*",
        "",
        designation,
        flags=re.IGNORECASE,
    ).strip()
    company = _clean_company_fragment(company) or "Unknown"
    if normalize_text(company) in {
        "linkedin", "linkedin profile", "professional profile",
    }:
        company = "Unknown"

    return name, designation, company, clean_url

_AMBIGUOUS_LOCATION_TOKENS = {"ncr", "ny", "uk", "uae", "bkc"}


def extract_profile_location(body, title, location_keywords,
                             require_current_evidence=False, person_name=""):
    """Return the selected current-profile location evidence, if present.

    Structured ``Location:`` text is strongest.  Otherwise only the early
    snippet body and explicit profile-location title patterns are considered,
    which avoids matching past roles or arbitrary company/title tokens.
    """
    if not location_keywords:
        return "Not requested"
    body_text = body or ""
    marker_matches = _profile_marker_matches(body_text, person_name)
    # Search engines commonly expose LinkedIn's structured "Location: ..."
    # field after the first 200 characters.  That is current-profile evidence,
    # unlike an unlabelled location buried in employment history.
    structured_locations = [
        _clean_profile_fragment(match.group(1))
        for match in re.finditer(
            r"\bLocation\s*:\s*([^\n·•|]{2,80})",
            body_text[:650],
            flags=re.IGNORECASE,
        )
    ]
    if marker_matches:
        for location in structured_locations:
            if any_term_matches(location, location_keywords):
                return location

    # Current Google LinkedIn citations often expose the profile location as
    # the first structured field, without a literal ``Location:`` label:
    # ``Bengaluru, Karnataka, India · Head of TA · Company``.
    leading_location, _, _ = _leading_google_profile_fields(
        body_text,
        person_name=person_name,
        designation_hint=(
            str(title or "") if _looks_like_designation(title) else ""
        ),
    )
    if (
        marker_matches
        and leading_location
        and any_term_matches(leading_location, location_keywords)
    ):
        return leading_location

    body_current = body_text[:420]

    # Standalone abbreviations like "NCR" are common company/name tokens
    # ("NCR Voyix") and should not qualify as geography unless they appear in
    # LinkedIn's structured Location field or as part of a less ambiguous phrase.
    safe_keywords = [
        term for term in location_keywords
        if normalize_text(term) not in _AMBIGUOUS_LOCATION_TOKENS
    ]
    if not require_current_evidence:
        for term in safe_keywords:
            if term_in_text(body_current, term):
                return term

        normalized_requested = {normalize_text(term) for term in location_keywords}
        if "ncr" in normalized_requested:
            for term in ("Delhi NCR", "National Capital Region"):
                if term_in_text(body_current, term):
                    return term

    # LinkedIn sometimes puts profile geography in the title as
    # "Name - New Delhi, Delhi, India | Professional Profile".  Only consider
    # that pattern, not arbitrary role/company text in the title.
    title_text = title or ""
    title_location_candidates = []
    for match in re.finditer(
        r"\s[-–]\s([^|]{2,90})\|\s*(?:professional profile|linkedin)\b",
        title_text,
        flags=re.IGNORECASE,
    ):
        candidate = match.group(1).strip()
        if "," in candidate or any_term_matches(candidate, ["India", "United States", "Singapore", "UAE", "UK"]):
            title_location_candidates.append(candidate)
    for candidate in title_location_candidates:
        if any_term_matches(candidate, safe_keywords):
            return candidate
    return ""


def check_location_in_snippet(body, title, href, location_keywords,
                              require_current_evidence=False, person_name=""):
    """Return True when the result has current-profile location evidence."""
    return bool(extract_profile_location(
        body, title, location_keywords,
        require_current_evidence=require_current_evidence,
        person_name=person_name,
    ))


# ---------------------------------------------------------------------------
# ROLE SYNONYM EXPANSION
# ---------------------------------------------------------------------------
_ROLE_ABBREV_MAP = {
    "ceo":   ["ceo", "chief executive officer"],
    "cmo":   ["cmo", "chief marketing officer"],
    "cto":   ["cto", "chief technology officer"],
    "cio":   ["cio", "chief information officer"],
    "cfo":   ["cfo", "chief financial officer"],
    "coo":   ["coo", "chief operating officer"],
    "cro":   ["cro", "chief revenue officer"],
    "cpo":   ["cpo", "chief product officer"],
    "chro":  ["chro", "chief human resources officer"],
    "ciso":  ["ciso", "chief information security officer"],
    "cdo":   ["cdo", "chief digital officer"],
    "cso":   ["cso", "chief strategy officer"],
    "vp":    ["vp", "vice president"],
    "svp":   ["svp", "senior vice president"],
    "evp":   ["evp", "executive vice president"],
    "md":    ["md", "managing director"],
    "gm":    ["gm", "general manager"],
    "avp":   ["avp", "assistant vice president"],
}

def _expand_role_terms(terms: list) -> list:
    """Expand role keywords to include both abbreviation and full-form variants."""
    expanded = []
    seen = set()
    for t in terms:
        t_norm = normalize_text(t)
        phrase_variants = [t]
        for abbrev, variants in _ROLE_ABBREV_MAP.items():
            norm_variants = [normalize_text(v) for v in variants]
            if t_norm == abbrev or t_norm in norm_variants:
                phrase_variants = list(variants)
                break
            # Expand abbreviations embedded in a phrase, e.g.
            # "VP Talent Acquisition" -> "Vice President Talent Acquisition".
            if re.search(rf"\b{re.escape(abbrev)}\b", t_norm):
                phrase_variants.extend(
                    re.sub(
                        rf"\b{re.escape(abbrev)}\b",
                        normalize_text(variant),
                        t_norm,
                    )
                    for variant in variants
                )
        for variant in phrase_variants:
            if variant not in seen:
                seen.add(variant)
                expanded.append(variant)
    return expanded

def check_role_in_title(title, body, role_keywords):
    """Return True if any role keyword (or its synonym) appears strictly in the title.
    Falls back to the first 100 chars of body (the designation line) only.
    This prevents body text like 'CM Delhi Govt' triggering a CMO role match.
    """
    if not role_keywords:
        return True
    expanded = _expand_role_terms(role_keywords)
    # The first body line is commonly LinkedIn's current designation.
    title_text = combined_text(title, (body or "")[:120])
    if any_term_matches(title_text, expanded):
        return True

    # Job-title punctuation and connector words are not stable across search
    # indexes: "Head of Talent Acquisition", "Talent Acquisition Head", and
    # "Global Head, Talent Acquisition" describe the same title. Require all
    # meaningful tokens (including seniority) without requiring exact order.
    title_normalized = normalize_text(title_text)
    connector_words = {"a", "an", "and", "at", "for", "of", "the", "to"}
    for role in expanded:
        tokens = [
            token for token in normalize_text(role).split()
            if token not in connector_words
        ]
        if len(tokens) >= 2 and all(
            term_in_text(title_normalized, token) for token in tokens
        ):
            return True
    return False

def get_parameter_matches(text, all_roles, all_locs, all_inds, all_sigs,
                          custom_terms, organization_terms=None):
    """Return exact user-selected values matched by each parameter category."""
    matched_roles = []
    for role in all_roles or []:
        if any_term_matches(text, _expand_role_terms([role])):
            matched_roles.append(role)

    return {
        "Role": list(dict.fromkeys(matched_roles)),
        "Location": matching_terms(text, all_locs or []),
        "Industry": matching_terms(text, all_inds or []),
        "Signal": matching_terms(text, all_sigs or []),
        "Custom": matching_terms(text, custom_terms or []),
        "Organisation": matching_terms(text, organization_terms or []),
    }


def get_detailed_match_summary(text, all_roles, all_locs, all_inds, all_sigs,
                               custom_terms, organization_terms=None):
    """Pinpoint which selected values matched instead of returning generic flags."""
    matches = get_parameter_matches(
        text, all_roles, all_locs, all_inds, all_sigs,
        custom_terms, organization_terms,
    )

    parts = []
    if all_roles:
        parts.append(f"Role: {', '.join(matches['Role']) or 'No match'}")
    if all_locs:
        parts.append(f"Location: {', '.join(matches['Location']) or 'No match'}")
    if all_inds:
        parts.append(f"Industry: {', '.join(matches['Industry']) or 'No match'}")
    if all_sigs:
        parts.append(f"Signal: {', '.join(matches['Signal']) or 'No match'}")
    if custom_terms:
        parts.append(f"Custom: {', '.join(matches['Custom']) or 'No match'}")
    if organization_terms:
        parts.append(f"Organisation: {', '.join(matches['Organisation']) or 'No match'}")

    return " | ".join(parts)
