"""Find phone numbers explicitly published on public company web pages.

This module does not authenticate, submit forms, solve CAPTCHAs, rotate
identities, or bypass access controls.  It stays on the supplied company domain,
honours robots.txt, and keeps the public source and surrounding evidence for
every result.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import asdict, dataclass
from typing import Callable, Iterable
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from core.utils import normalize_text

USER_AGENT = "SpeedyScraper-PublicContactFinder/1.0"
DEFAULT_PATHS = (
    "",
    "/contact",
    "/contact-us",
    "/about",
    "/about-us",
    "/leadership",
    "/team",
    "/management",
    "/press",
    "/media",
    "/investors",
)
RELEVANT_PATH_TERMS = (
    "contact", "about", "leadership", "team", "management", "executive",
    "press", "media", "news", "investor", "corporate", "office",
)
BLOCKED_PATH_TERMS = (
    "login", "log-in", "signin", "sign-in", "signup", "sign-up", "auth",
    "account", "checkout", "subscribe",
)
PHONE_CUES = (
    "phone", "telephone", "tel", "call", "mobile", "contact", "office",
    "reception", "switchboard", "media", "press", "investor",
)
PHONE_CANDIDATE_RE = re.compile(
    r"(?<![\w])(?:\+?\d{1,3}[\s()./-]*)?"
    r"(?:\(?\d{2,5}\)?[\s()./-]*){1,3}\d{3,5}(?![\w])"
)


@dataclass(frozen=True)
class PublicContact:
    phone: str
    phone_type: str
    attribution: str
    leader_name: str
    source_url: str
    page_title: str
    evidence: str
    confidence: str

    def to_dict(self) -> dict:
        return asdict(self)


def _canonical_url(url: str) -> str:
    parsed = urlsplit(str(url or "").strip())
    if not parsed.scheme:
        parsed = urlsplit(f"https://{str(url or '').strip()}")
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Enter a valid public company domain or HTTP(S) URL.")
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    return urlunsplit((scheme, parsed.netloc.lower(), path, "", ""))


def _registrable_scope(hostname: str) -> str:
    host = str(hostname or "").lower().strip(".")
    return host[4:] if host.startswith("www.") else host


def _is_allowed_url(url: str, allowed_host: str) -> bool:
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    host = _registrable_scope(parsed.hostname)
    if host != allowed_host and not host.endswith(f".{allowed_host}"):
        return False
    path_norm = normalize_text(parsed.path)
    return not any(term_in_path(path_norm, term) for term in BLOCKED_PATH_TERMS)


def term_in_path(path_norm: str, term: str) -> bool:
    return bool(re.search(
        rf"(?<![a-z0-9]){re.escape(normalize_text(term))}(?![a-z0-9])",
        path_norm,
    ))


def _normalize_phone(candidate: str) -> str:
    # ``tel:`` links frequently URL-encode spaces as ``%20``. Decode before
    # stripping punctuation so the digits in the escape sequence do not become
    # part of the phone number.
    raw = unquote(str(candidate or "")).strip()
    raw = re.sub(
        r"\s*(?:ext(?:ension)?\.?|x)\s*\d+\s*$",
        "",
        raw,
        flags=re.IGNORECASE,
    )
    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if not 8 <= len(digits) <= 15:
        return ""

    # Avoid dates, years, invoice-like values and repeated placeholder digits.
    if len(set(digits)) <= 2:
        return ""
    if re.fullmatch(r"(?:19|20)\d{6}", digits):
        return ""
    return f"+{digits}" if has_plus else digits


def _phone_type(phone: str, evidence: str) -> str:
    digits = re.sub(r"\D", "", phone)
    evidence_norm = normalize_text(evidence)
    if "fax" in evidence_norm:
        return "fax"
    if digits.startswith(("1800", "1860", "800")):
        return "toll_free"
    indian_number = digits[-10:] if digits.startswith("91") and len(digits) == 12 else digits
    if len(indian_number) == 10 and indian_number[0] in "6789":
        return "possible_mobile_or_direct"
    return "business_phone"


def _clean_evidence(value: str, limit: int = 280) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _leader_is_near(evidence: str, leader_name: str) -> bool:
    leader_norm = normalize_text(leader_name)
    return bool(leader_norm and leader_norm in normalize_text(evidence))


def _make_contact(
    candidate: str,
    evidence: str,
    source_url: str,
    page_title: str,
    leader_name: str,
    explicit_tel_link: bool,
) -> PublicContact | None:
    phone = _normalize_phone(candidate)
    if not phone:
        return None

    evidence_clean = _clean_evidence(evidence)
    evidence_norm = normalize_text(evidence_clean)
    leader_near = _leader_is_near(evidence_clean, leader_name)
    cue_near = any(cue in evidence_norm for cue in PHONE_CUES)
    if not explicit_tel_link and not cue_near:
        return None

    attribution = "leader_context" if leader_near else "company_general"
    confidence = (
        "high" if explicit_tel_link and leader_near
        else "medium" if explicit_tel_link or leader_near
        else "low"
    )
    return PublicContact(
        phone=phone,
        phone_type=_phone_type(phone, evidence_clean),
        attribution=attribution,
        leader_name=leader_name if leader_near else "",
        source_url=source_url,
        page_title=page_title,
        evidence=evidence_clean,
        confidence=confidence,
    )


def extract_contacts_from_html(
    html: str,
    source_url: str,
    leader_name: str = "",
) -> list[PublicContact]:
    """Extract source-attributed public phones from one HTML document."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    page_title = _clean_evidence(
        soup.title.get_text(" ", strip=True) if soup.title else "",
        limit=120,
    )
    found: list[PublicContact] = []
    seen = set()

    for anchor in soup.select('a[href^="tel:"]'):
        candidate = anchor.get("href", "")[4:].split("?", 1)[0]
        container = anchor.find_parent(["li", "p", "address", "div", "section"])
        evidence = (
            container.get_text(" ", strip=True)
            if container is not None
            else anchor.get_text(" ", strip=True)
        )
        contact = _make_contact(
            candidate, evidence, source_url, page_title, leader_name, True,
        )
        if contact and contact.phone not in seen:
            seen.add(contact.phone)
            found.append(contact)

    visible_text = soup.get_text(" ", strip=True)
    for match in PHONE_CANDIDATE_RE.finditer(visible_text):
        start = max(0, match.start() - 180)
        end = min(len(visible_text), match.end() + 180)
        evidence = visible_text[start:end]
        contact = _make_contact(
            match.group(0), evidence, source_url, page_title, leader_name, False,
        )
        if contact and contact.phone not in seen:
            seen.add(contact.phone)
            found.append(contact)

    return found


class PublicContactFinder:
    """Crawl a small public, robots-aware slice of an official company site."""

    def __init__(
        self,
        domain_or_url: str,
        max_pages: int = 20,
        request_delay: float = 0.5,
        timeout: float = 12,
        status: Callable[[str], None] | None = None,
        session: requests.Session | None = None,
        search_client=None,
        request_get: Callable | None = None,
    ):
        self.base_url = _canonical_url(domain_or_url)
        parsed = urlsplit(self.base_url)
        self.allowed_host = _registrable_scope(parsed.hostname or "")
        self.max_pages = max(1, min(int(max_pages), 50))
        self.request_delay = max(0.0, float(request_delay))
        self.timeout = max(2.0, min(float(timeout), 30.0))
        self.status = status or (lambda _message: None)
        self.session = session or requests.Session()
        self.search_client = search_client
        self.request_get = request_get or self.session.get
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
        })
        self._robots: dict[str, RobotFileParser] = {}

    def _robot_parser(self, url: str) -> RobotFileParser:
        parsed = urlsplit(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            parser = RobotFileParser()
            parser.set_url(urljoin(origin, "/robots.txt"))
            try:
                response = self.request_get(
                    parser.url, timeout=self.timeout, allow_redirects=True,
                )
                if response.ok:
                    parser.parse(response.text.splitlines())
                else:
                    parser.parse([])
            except requests.RequestException:
                parser.parse([])
            self._robots[origin] = parser
        return self._robots[origin]

    def _can_fetch(self, url: str) -> bool:
        return self._robot_parser(url).can_fetch(USER_AGENT, url)

    def _search_urls(self, leader_name: str) -> list[str]:
        """Use public search results only as crawl seeds."""
        query_parts = [f"site:{self.allowed_host}"]
        if leader_name:
            query_parts.append(f'"{leader_name}"')
        query_parts.append("(phone OR contact OR leadership OR media)")
        try:
            if self.search_client is not None:
                results = list(self.search_client.text(" ".join(query_parts), max_results=10))
            else:
                from ddgs import DDGS
                with DDGS() as ddgs:
                    results = list(ddgs.text(" ".join(query_parts), max_results=10))
        except Exception:
            return []
        urls = []
        for result in results:
            try:
                url = _canonical_url(result.get("href", ""))
            except ValueError:
                continue
            if _is_allowed_url(url, self.allowed_host):
                urls.append(url)
        return urls

    def _seed_urls(self, leader_name: str) -> list[tuple[str, int]]:
        origin = f"{urlsplit(self.base_url).scheme}://{urlsplit(self.base_url).netloc}"
        urls = [_canonical_url(urljoin(origin, path)) for path in DEFAULT_PATHS]
        urls.extend(self._search_urls(leader_name))
        unique = list(dict.fromkeys(urls))
        return [(url, 0) for url in unique if _is_allowed_url(url, self.allowed_host)]

    def _relevant_links(self, html: str, current_url: str) -> Iterable[str]:
        soup = BeautifulSoup(html or "", "html.parser")
        for anchor in soup.select("a[href]"):
            href = anchor.get("href", "").strip()
            if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
                continue
            try:
                url = _canonical_url(urljoin(current_url, href))
            except ValueError:
                continue
            if not _is_allowed_url(url, self.allowed_host):
                continue
            path_norm = normalize_text(urlsplit(url).path)
            if any(term_in_path(path_norm, term) for term in RELEVANT_PATH_TERMS):
                yield url

    def find(self, leader_name: str = "") -> list[PublicContact]:
        queue = deque(self._seed_urls(leader_name))
        queued = {url for url, _depth in queue}
        visited = set()
        contacts: dict[str, PublicContact] = {}

        while queue and len(visited) < self.max_pages:
            url, depth = queue.popleft()
            if url in visited:
                continue
            visited.add(url)
            if not self._can_fetch(url):
                self.status(f"robots.txt blocked: {url}")
                continue

            self.status(f"Checking public page {len(visited)}/{self.max_pages}: {url}")
            try:
                response = self.request_get(
                    url,
                    timeout=self.timeout,
                    allow_redirects=True,
                )
            except requests.RequestException as exc:
                self.status(f"Skipped ({type(exc).__name__}): {url}")
                continue

            final_url = _canonical_url(response.url)
            content_type = response.headers.get("content-type", "").lower()
            if (
                response.status_code != 200
                or "text/html" not in content_type
                or not _is_allowed_url(final_url, self.allowed_host)
            ):
                self.status(f"Skipped HTTP {response.status_code}: {url}")
                continue

            for contact in extract_contacts_from_html(
                response.text, final_url, leader_name=leader_name,
            ):
                existing = contacts.get(contact.phone)
                rank = {"low": 1, "medium": 2, "high": 3}
                if existing is None or rank[contact.confidence] > rank[existing.confidence]:
                    contacts[contact.phone] = contact

            if depth < 1:
                for discovered in self._relevant_links(response.text, final_url):
                    if discovered not in visited and discovered not in queued:
                        queued.add(discovered)
                        queue.append((discovered, depth + 1))

            if self.request_delay:
                time.sleep(self.request_delay)

        return sorted(
            contacts.values(),
            key=lambda contact: (
                contact.attribution != "leader_context",
                {"high": 0, "medium": 1, "low": 2}[contact.confidence],
                contact.phone,
            ),
        )
