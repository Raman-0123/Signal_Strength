"""Persistent event-speaker extraction and LinkedIn enrichment."""

from __future__ import annotations

import ipaddress
import json
import re
import socket
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from core.utils import (
    company_name_variants,
    normalize_linkedin_url,
    normalize_text,
    parse_profile,
    person_identity_key,
    term_in_text,
)
from speedy_scraper.domain import JobRecord, JobStatus, SearchRequest, SearchResult
from speedy_scraper.events import RepositoryEventSink
from speedy_scraper.providers import ProviderRegistry
from speedy_scraper.reliability import (
    ProxyPool,
    RateLimitError,
    RetryExecutor,
    TokenBucketRateLimiter,
    parse_retry_after,
)

MAX_HTML_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5
AUTO_MATCH_THRESHOLD = 0.80
AUTO_MATCH_MARGIN = 0.10


class UnsafeSourceUrlError(ValueError):
    """Raised when a source URL could reach a non-public network address."""


class ResponseTooLargeError(ValueError):
    """Raised when a source response exceeds the configured safety limit."""


class NoSpeakersFoundError(ValueError):
    """Raised when a supported speaker payload is absent or empty."""


def validate_public_source_url(
    url: str,
    *,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    resolve: bool = True,
) -> str:
    """Validate an HTTP(S) URL and reject local/private resolution targets."""

    value = str(url or "").strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise UnsafeSourceUrlError(f"Invalid source URL: {exc}") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UnsafeSourceUrlError("source_url must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeSourceUrlError("source_url must not contain credentials")
    hostname = (parsed.hostname or "").strip().rstrip(".").lower()
    if not hostname:
        raise UnsafeSourceUrlError("source_url must include a hostname")
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        raise UnsafeSourceUrlError("source_url must resolve to a public host")
    if not resolve:
        return value

    try:
        resolved = resolver(hostname, port or (443 if parsed.scheme.lower() == "https" else 80), type=socket.SOCK_STREAM)
    except (OSError, socket.gaierror) as exc:
        raise UnsafeSourceUrlError(f"Could not resolve source host {hostname!r}") from exc
    if not resolved:
        raise UnsafeSourceUrlError(f"Could not resolve source host {hostname!r}")
    for item in resolved:
        address = str(item[4][0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as exc:
            raise UnsafeSourceUrlError(f"Source host returned an invalid address: {address}") from exc
        if not ip.is_global:
            raise UnsafeSourceUrlError(
                f"source_url resolves to a non-public address: {address}"
            )
    return value


class SafeHtmlFetcher:
    """Fetch public HTML with bounded redirects, response size, retries, and rate limits."""

    def __init__(
        self,
        limiter: TokenBucketRateLimiter,
        retries: RetryExecutor,
        proxies: ProxyPool,
        event_sink=None,
        *,
        session: requests.Session | None = None,
        resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
        max_bytes: int = MAX_HTML_BYTES,
    ):
        self.limiter = limiter
        self.retries = retries
        self.proxies = proxies
        self.event_sink = event_sink
        self.session = session or requests.Session()
        self.resolver = resolver
        self.max_bytes = max(1, int(max_bytes))
        self.session.headers.update({
            "User-Agent": "SpeedyScraper-EventSpeakers/2.0",
            "Accept": "text/html,application/xhtml+xml",
        })

    def _request(self, url: str, job_id: str):
        proxy = self.proxies.acquire(job_id)
        self.limiter.acquire("event_page")

        def operation():
            response = self.session.get(
                url,
                allow_redirects=False,
                stream=True,
                timeout=(10, 30),
                proxies={"http": proxy, "https": proxy} if proxy else None,
            )
            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            if response.status_code == 429:
                response.close()
                raise RateLimitError("Event page rate limit", retry_after=retry_after)
            if response.status_code in {408, 500, 502, 503, 504}:
                response.close()
                raise requests.ConnectionError(f"Event page HTTP {response.status_code}")
            return response

        response, attempts = self.retries.run(
            operation,
            provider="event_page",
            event_sink=self.event_sink,
            on_exhausted=lambda _exc: self.proxies.report_failure(job_id, rotate=True),
        )
        self.proxies.report_success(job_id)
        return response, attempts

    def fetch(self, url: str, *, job_id: str) -> str:
        current = str(url)
        for redirect_index in range(MAX_REDIRECTS + 1):
            current = validate_public_source_url(
                current,
                resolver=self.resolver,
                resolve=True,
            )
            response, _attempts = self._request(current, job_id)
            try:
                if response.is_redirect or response.is_permanent_redirect:
                    if redirect_index >= MAX_REDIRECTS:
                        raise UnsafeSourceUrlError("source_url exceeded the redirect limit")
                    location = response.headers.get("Location", "").strip()
                    if not location:
                        raise UnsafeSourceUrlError("source_url returned a redirect without a location")
                    current = urljoin(current, location)
                    continue

                response.raise_for_status()
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type not in {"text/html", "application/xhtml+xml"}:
                    raise ValueError(
                        f"source_url must return HTML, received {content_type or 'unknown content type'}"
                    )
                content_length = response.headers.get("Content-Length", "").strip()
                if content_length:
                    try:
                        if int(content_length) > self.max_bytes:
                            raise ResponseTooLargeError(
                                f"source response exceeds {self.max_bytes} bytes"
                            )
                    except ValueError as exc:
                        if isinstance(exc, ResponseTooLargeError):
                            raise

                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ResponseTooLargeError(
                            f"source response exceeds {self.max_bytes} bytes"
                        )
                    chunks.append(chunk)
                encoding = response.encoding or "utf-8"
                return b"".join(chunks).decode(encoding, errors="replace")
            finally:
                response.close()
        raise UnsafeSourceUrlError("source_url exceeded the redirect limit")


def linkedin_id_from_url(url: str) -> str:
    canonical = normalize_linkedin_url(url)
    if not canonical:
        return ""
    return urlsplit(canonical).path.strip("/").split("/", 1)[-1]


@dataclass(slots=True)
class EventSpeaker:
    speaker_id: str
    name: str
    designation: str
    company: str
    country: str
    linkedin_url: str = ""
    match_status: str = "not_found"
    confidence: float = 0.0
    match_evidence: str = ""
    source_url: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EventSpeaker":
        fields = cls.__dataclass_fields__
        return cls(**{key: value.get(key, fields[key].default) for key in fields})

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def as_export_row(self) -> dict[str, Any]:
        canonical = normalize_linkedin_url(self.linkedin_url)
        return {
            "Name": self.name,
            "Designation": self.designation,
            "Company": self.company,
            "Country": self.country,
            "LinkedIn ID": linkedin_id_from_url(canonical),
            "LinkedIn URL": canonical,
            "Match Status": self.match_status,
            "Confidence": round(float(self.confidence), 2),
            "Match Evidence": self.match_evidence,
            "Source URL": self.source_url,
        }


def _next_flight_text(html: str) -> str:
    chunks: list[str] = []
    prefix = "self.__next_f.push("
    for script in BeautifulSoup(html, "html.parser").find_all("script"):
        text = (script.string or script.get_text() or "").strip()
        if not text.startswith(prefix):
            continue
        expression = text[len(prefix):].strip()
        if expression.endswith(")"):
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
            value, _end = decoder.raw_decode(decoded_flight[start:])
        except json.JSONDecodeError:
            position = start
            continue
        if isinstance(value, dict) and isinstance(value.get("data"), list):
            return value
        position = start
    raise NoSpeakersFoundError("No supported allSpeakersData payload was found")


def extract_event_speakers(html: str, source_url: str) -> list[EventSpeaker]:
    """Extract active, unique speakers from the GFF Next.js Flight payload."""

    payload = _speaker_payload(_next_flight_text(html))
    extracted: list[EventSpeaker] = []
    seen_ids: set[str] = set()
    seen_people: set[str] = set()
    for index, raw in enumerate(payload.get("data", [])):
        if not isinstance(raw, dict):
            continue
        if raw.get("isActive", True) in {False, 0, "false", "False"}:
            continue
        name = re.sub(r"\s+", " ", str(raw.get("fullName", "")).strip())
        company = re.sub(r"\s+", " ", str(raw.get("companyName", "")).strip())
        if not name:
            continue
        speaker_id = str(
            raw.get("speakerId") or raw.get("documentId") or raw.get("id") or index
        )
        identity = person_identity_key(name, company)
        if speaker_id in seen_ids or (identity and identity in seen_people):
            continue
        seen_ids.add(speaker_id)
        if identity:
            seen_people.add(identity)
        country_value = raw.get("country", "")
        if isinstance(country_value, dict):
            country = str(country_value.get("country", "")).strip()
        else:
            country = str(country_value or "").strip()
        supplied = str(raw.get("linkedinProfile") or "").strip()
        canonical = normalize_linkedin_url(supplied)
        if canonical:
            status = "provided"
            confidence = 1.0
            evidence = "event page linkedinProfile"
        else:
            status = "not_found"
            confidence = 0.0
            evidence = "invalid provided LinkedIn URL" if supplied else "no provided LinkedIn URL"
        extracted.append(EventSpeaker(
            speaker_id=speaker_id,
            name=name,
            designation=re.sub(
                r"\s+", " ", str(raw.get("desgination") or raw.get("designation") or "").strip()
            ),
            company=company,
            country=country,
            linkedin_url=canonical,
            match_status=status,
            confidence=confidence,
            match_evidence=evidence,
            source_url=source_url,
        ))
    if not extracted:
        raise NoSpeakersFoundError("The speaker payload did not contain active speakers")
    return extracted


_HONORIFICS = {"mr", "mrs", "ms", "miss", "dr", "prof", "shri", "smt"}
_ROLE_STOPWORDS = {
    "a", "an", "and", "at", "for", "global", "in", "of", "on", "senior", "the", "to",
}


def _name_tokens(value: str) -> list[str]:
    return [token for token in normalize_text(value).split() if token not in _HONORIFICS]


def _name_score(source: str, candidate: str) -> tuple[float, str]:
    source_tokens = _name_tokens(source)
    candidate_tokens = _name_tokens(candidate)
    if not source_tokens or not candidate_tokens:
        return 0.0, ""
    if source_tokens == candidate_tokens:
        return 0.60, "exact_name"
    if source_tokens[-1] != candidate_tokens[-1]:
        return 0.0, ""
    if len(source_tokens) == len(candidate_tokens) and all(
        left == right or (len(left) == 1 and right.startswith(left)) or (len(right) == 1 and left.startswith(right))
        for left, right in zip(source_tokens, candidate_tokens)
    ):
        return 0.50, "similar_name"
    overlap = len(set(source_tokens) & set(candidate_tokens)) / max(len(set(source_tokens)), 1)
    if overlap >= 0.80:
        return 0.50, "similar_name"
    return 0.0, ""


def _designation_matches(designation: str, candidate_text: str) -> bool:
    source_tokens = [
        token for token in normalize_text(designation).split()
        if token not in _ROLE_STOPWORDS and len(token) > 1
    ]
    if not source_tokens:
        return False
    candidate_tokens = set(normalize_text(candidate_text).split())
    overlap = sum(token in candidate_tokens for token in source_tokens)
    required = 1 if len(source_tokens) == 1 else 2
    return overlap >= required and overlap / len(source_tokens) >= 0.50


@dataclass(frozen=True, slots=True)
class _Candidate:
    url: str
    score: float
    evidence: tuple[str, ...]


def choose_speaker_match(
    speaker: EventSpeaker,
    results: Iterable[SearchResult],
) -> EventSpeaker:
    """Apply deterministic name/company/role scoring to public search results."""

    by_url: dict[str, _Candidate] = {}
    for result in results:
        canonical = normalize_linkedin_url(result.href)
        if not canonical:
            continue
        parsed_name, parsed_role, parsed_company, _url = parse_profile(
            result.title,
            canonical,
            result.body,
        )
        name_points, name_evidence = _name_score(speaker.name, parsed_name or "")
        if not name_points:
            continue
        evidence = [name_evidence]
        score = name_points
        candidate_text = " ".join((
            result.title,
            result.body,
            parsed_company or "",
            parsed_role or "",
        ))
        variants = company_name_variants(speaker.company)
        if variants and any(term_in_text(candidate_text, variant) for variant in variants):
            score += 0.30
            evidence.append("company_match")
        if _designation_matches(speaker.designation, candidate_text):
            score += 0.20
            evidence.append("designation_match")
        candidate = _Candidate(canonical, min(score, 1.0), tuple(evidence))
        existing = by_url.get(canonical)
        if existing is None or candidate.score > existing.score:
            by_url[canonical] = candidate

    ranked = sorted(by_url.values(), key=lambda item: (-item.score, item.url))
    if not ranked:
        speaker.linkedin_url = ""
        speaker.match_status = "not_found"
        speaker.confidence = 0.0
        speaker.match_evidence = "no eligible personal LinkedIn result"
        return speaker

    top = ranked[0]
    margin = top.score - ranked[1].score if len(ranked) > 1 else 1.0
    details = "; ".join((*top.evidence, f"candidate={top.url}", f"margin={margin:.2f}"))
    if top.score >= AUTO_MATCH_THRESHOLD and margin >= AUTO_MATCH_MARGIN:
        speaker.linkedin_url = top.url
        speaker.match_status = "matched"
        speaker.confidence = round(top.score, 2)
        speaker.match_evidence = details
    else:
        speaker.linkedin_url = ""
        speaker.match_status = "ambiguous"
        speaker.confidence = round(top.score, 2)
        speaker.match_evidence = details
    return speaker


def _quoted(value: str) -> str:
    return str(value or "").replace('"', "'").strip()


class EventSpeakerEngine:
    """Run and checkpoint one event-speaker job."""

    def __init__(
        self,
        config,
        repository,
        logger=None,
        *,
        limiter: TokenBucketRateLimiter | None = None,
        retries: RetryExecutor | None = None,
        proxies: ProxyPool | None = None,
        fetcher: SafeHtmlFetcher | None = None,
    ):
        self.config = config
        self.repository = repository
        self.logger = logger
        self.limiter = limiter or TokenBucketRateLimiter(config.rate_limits)
        self.retries = retries or RetryExecutor(config.retry)
        self.proxies = proxies or ProxyPool(config.proxy)
        self.fetcher = fetcher

    def _save_state(self, job_id: str, state: dict[str, Any]) -> None:
        checkpoint = dict(self.repository.get_job(job_id).checkpoint)
        checkpoint["event_speakers"] = state
        self.repository.save_checkpoint(job_id, checkpoint)

    def _requested_stop(self, job_id: str, state: dict[str, Any]) -> JobRecord | None:
        job = self.repository.get_job(job_id)
        if job.status == JobStatus.PAUSE_REQUESTED:
            self._save_state(job_id, state)
            return self.repository.transition(job_id, JobStatus.PAUSED, outcome="Paused")
        if job.status == JobStatus.CANCEL_REQUESTED:
            self._save_state(job_id, state)
            return self.repository.transition(job_id, JobStatus.CANCELLED, outcome="Cancelled")
        return None

    def _search(self, provider, job_id: str, query: str) -> tuple[SearchResult, ...]:
        try:
            page = provider.search(SearchRequest(
                query=query,
                page=1,
                max_results=10,
                linkedin_only=True,
                job_id=job_id,
            ))
        except Exception as exc:
            if "no results found" not in str(exc).lower():
                raise
            self.repository.increment_metric(job_id, "speaker_search_queries")
            self.repository.increment_metric(job_id, "speaker_search_empty")
            return ()
        self.repository.increment_metric(job_id, "speaker_search_queries")
        self.repository.increment_metric(job_id, "provider_attempts", page.attempts)
        return page.results

    def _enrich(self, provider, job_id: str, speaker: EventSpeaker) -> EventSpeaker:
        queries = []
        if speaker.company:
            queries.append(
                f'site:linkedin.com/in "{_quoted(speaker.name)}" "{_quoted(speaker.company)}"'
            )
        if speaker.designation:
            queries.append(
                f'site:linkedin.com/in "{_quoted(speaker.name)}" "{_quoted(speaker.designation)}"'
            )
        if not queries:
            queries.append(f'site:linkedin.com/in "{_quoted(speaker.name)}"')

        combined: list[SearchResult] = []
        for query in dict.fromkeys(queries):
            combined.extend(self._search(provider, job_id, query))
            decision = choose_speaker_match(speaker, combined)
            if decision.match_status == "matched":
                return decision
        return choose_speaker_match(speaker, combined)

    def run(self, job_id: str) -> JobRecord:
        job = self.repository.get_job(job_id)
        if job.status == JobStatus.QUEUED:
            job = self.repository.transition(job_id, JobStatus.RUNNING, outcome="Running")
        if job.status not in {
            JobStatus.RUNNING,
            JobStatus.PAUSE_REQUESTED,
            JobStatus.CANCEL_REQUESTED,
        }:
            return job

        sink = RepositoryEventSink(self.repository, job_id, self.logger)
        state = dict(job.checkpoint.get("event_speakers") or {})
        stopped = self._requested_stop(job_id, state)
        if stopped:
            return stopped

        if not state.get("speakers"):
            source_url = str(job.request.get("source_url", ""))
            fetcher = self.fetcher or SafeHtmlFetcher(
                self.limiter,
                self.retries,
                self.proxies,
                sink,
            )
            sink.emit("info", "speaker_source_fetch", "Fetching the event speaker page.")
            html = fetcher.fetch(source_url, job_id=job_id)
            speakers = extract_event_speakers(html, source_url)
            state = {
                "phase": "enrichment",
                "speaker_index": 0,
                "speakers": [speaker.as_dict() for speaker in speakers],
            }
            self._save_state(job_id, state)
            sink.emit(
                "info",
                "speakers_extracted",
                f"Extracted {len(speakers)} active speakers.",
                {
                    "extracted": len(speakers),
                    "provided": sum(item.match_status == "provided" for item in speakers),
                },
            )

        speakers = [EventSpeaker.from_dict(value) for value in state["speakers"]]
        enrich_missing = bool(job.request.get("enrich_missing", True))
        provider = None
        if enrich_missing:
            registry = ProviderRegistry(
                self.config,
                event_sink=sink,
                limiter=self.limiter,
                retries=self.retries,
                proxies=self.proxies,
            )
            provider = registry.get(str(job.request.get("search_provider", "ddgs")))

        start = max(0, min(int(state.get("speaker_index", 0)), len(speakers)))
        for index in range(start, len(speakers)):
            stopped = self._requested_stop(job_id, state)
            if stopped:
                return stopped
            speaker = speakers[index]
            if speaker.match_status != "provided":
                if enrich_missing and provider is not None:
                    speaker = self._enrich(provider, job_id, speaker)
                else:
                    speaker.linkedin_url = ""
                    speaker.match_status = "not_found"
                    speaker.confidence = 0.0
                    speaker.match_evidence = "enrichment disabled"
                speakers[index] = speaker
            state["speaker_index"] = index + 1
            state["speakers"] = [item.as_dict() for item in speakers]
            self._save_state(job_id, state)
            if (index + 1) % 10 == 0 or index + 1 == len(speakers):
                sink.emit(
                    "info",
                    "speaker_progress",
                    f"Processed {index + 1} of {len(speakers)} speakers.",
                    {"processed": index + 1, "total": len(speakers)},
                )

        rows = [speaker.as_export_row() for speaker in speakers]
        counts = {
            status: sum(speaker.match_status == status for speaker in speakers)
            for status in ("provided", "matched", "ambiguous", "not_found")
        }
        self.repository.replace_artifact(job_id, "event_speakers", rows)
        state["phase"] = "completed"
        state["speaker_index"] = len(speakers)
        self._save_state(job_id, state)
        self.repository.set_metric(job_id, "speakers_extracted", len(speakers))
        for status, count in counts.items():
            self.repository.set_metric(job_id, f"speakers_{status}", count)
        sink.emit(
            "info",
            "speaker_workflow_completed",
            f"Completed {len(speakers)} speakers: {counts['provided']} provided, "
            f"{counts['matched']} matched, {counts['ambiguous']} ambiguous, "
            f"{counts['not_found']} not found.",
            {"total": len(speakers), **counts},
        )
        return self.repository.transition(
            job_id,
            JobStatus.COMPLETED,
            outcome=(
                f"{len(speakers)} speakers; {counts['provided'] + counts['matched']} "
                "LinkedIn profiles"
            ),
        )
