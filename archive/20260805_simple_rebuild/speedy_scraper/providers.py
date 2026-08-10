"""Normalized Google, DDGS, Brave, and local seed providers."""

from __future__ import annotations

import csv
import urllib.parse
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from core.google_search import GoogleSecurityCheck, google_text_search
from speedy_scraper.domain import SearchPage, SearchRequest, SearchResult
from speedy_scraper.events import NullEventSink
from speedy_scraper.reliability import (
    ProviderBlockedError,
    ProxyPool,
    RateLimitError,
    RetryExecutor,
    TokenBucketRateLimiter,
    parse_retry_after,
)


class _ProviderBase:
    name = "base"

    def __init__(self, limiter, retries, proxies, event_sink=None):
        self.limiter: TokenBucketRateLimiter = limiter
        self.retries: RetryExecutor = retries
        self.proxies: ProxyPool = proxies
        self.event_sink = event_sink or NullEventSink()

    def _execute(self, request: SearchRequest, operation):
        proxy = self.proxies.acquire(request.job_id)
        self.limiter.acquire(self.name)

        def exhausted(exc: Exception) -> None:
            self.proxies.report_failure(request.job_id, rotate=True)

        value, attempts = self.retries.run(
            lambda: operation(proxy), provider=self.name,
            event_sink=self.event_sink, on_exhausted=exhausted,
        )
        self.proxies.report_success(request.job_id)
        return value, attempts, proxy


class DDGSSearchProvider(_ProviderBase):
    name = "ddgs"
    _empty_markers = ("no results found", "no results")
    _block_markers = (
        "403", "429", "blocked", "captcha", "challenge", "forbidden",
        "rate limit", "ratelimit", "too many requests", "bot detected",
        "connection refused", "connecterror", "upstream", "refused",
    )

    @classmethod
    def _is_empty_error(cls, exc: Exception) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in cls._empty_markers)

    @classmethod
    def _is_block_error(cls, exc: Exception) -> bool:
        message = f"{type(exc).__name__}: {exc}".lower()
        return any(marker in message for marker in cls._block_markers)

    def search(self, request: SearchRequest) -> SearchPage:
        def operation(proxy: str):
            from ddgs import DDGS

            try:
                with DDGS(proxy=proxy or None, timeout=20) as ddgs:
                    return list(ddgs.text(
                        request.query,
                        region="in-en",
                        safesearch="off",
                        max_results=request.max_results,
                        page=request.page,
                        backend="yahoo",
                    ))
            except Exception as exc:
                if self._is_empty_error(exc):
                    return []
                if self._is_block_error(exc):
                    raise ProviderBlockedError(
                        f"DuckDuckGo blocked or refused the search request: {exc}",
                        provider=self.name,
                    ) from exc
                raise

        raw, attempts, proxy = self._execute(request, operation)
        results = tuple(SearchResult(
            title=str(item.get("title", "")),
            body=str(item.get("body", "")),
            href=str(item.get("href", "")),
        ) for item in raw)
        next_page = request.page + 1 if len(results) >= request.max_results else None
        return SearchPage(
            results=results, provider=self.name, page=request.page,
            next_page=next_page, proxy_id=self.proxies.identifier(proxy), attempts=attempts,
            metadata={"result_count": len(results)},
            retry_information={"attempts": attempts, "retried": attempts > 1},
        )


class BraveSearchProvider(_ProviderBase):
    name = "brave"

    def search(self, request: SearchRequest) -> SearchPage:
        def operation(proxy: str):
            url = "https://search.brave.com/search?" + urllib.parse.urlencode({
                "q": request.query,
                "offset": (request.page - 1) * 10,
            })
            response = requests.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/124 Safari/537.36",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                timeout=20,
                proxies={"http": proxy, "https": proxy} if proxy else None,
            )
            if response.status_code == 429:
                raise RateLimitError(
                    "Brave rate limit",
                    retry_after=parse_retry_after(response.headers.get("Retry-After")),
                )
            if response.status_code in {408, 500, 502, 503, 504}:
                raise requests.ConnectionError(f"Brave HTTP {response.status_code}")
            response.raise_for_status()
            return response.text

        html, attempts, proxy = self._execute(request, operation)
        soup = BeautifulSoup(html, "html.parser")
        seen: set[str] = set()
        results: list[SearchResult] = []
        for anchor in soup.select("a[href]"):
            href = str(anchor.get("href", ""))
            if request.linkedin_only and "linkedin.com/in/" not in href:
                continue
            if not href.startswith("http") or href in seen:
                continue
            seen.add(href)
            container = anchor.find_parent(["div", "article"])
            body = container.get_text(" ", strip=True) if container else anchor.get_text(" ", strip=True)
            results.append(SearchResult(anchor.get_text(" ", strip=True), body, href))
            if len(results) >= request.max_results:
                break
        next_page = request.page + 1 if len(results) >= request.max_results else None
        return SearchPage(
            results=tuple(results), provider=self.name, page=request.page,
            next_page=next_page, proxy_id=self.proxies.identifier(proxy), attempts=attempts,
            metadata={"result_count": len(results)},
            retry_information={"attempts": attempts, "retried": attempts > 1},
        )


class GoogleSearchProvider(_ProviderBase):
    name = "google"

    def __init__(self, limiter, retries, proxies, browser_state, event_sink=None):
        super().__init__(limiter, retries, proxies, event_sink)
        self.browser_state = browser_state

    def search(self, request: SearchRequest) -> SearchPage:
        proxy = self.proxies.acquire(request.job_id)
        if proxy:
            # Runtime-only: proxy credentials must never enter a persisted
            # browser checkpoint.  The sticky pool reacquires the same proxy
            # if this exact request is resumed.
            self.browser_state["proxy"] = proxy
        self.limiter.acquire(self.name)

        def operation():
            return google_text_search(
                request.query,
                page=request.page,
                max_results=request.max_results,
                browser_state=self.browser_state,
                linkedin_only=request.linkedin_only,
            )

        def retryable(exc: Exception) -> bool:
            if isinstance(exc, GoogleSecurityCheck):
                return False
            message = str(exc).lower()
            if any(marker in message for marker in ("not installed", "does not point", "configuration")):
                return False
            return (
                "timeout" in type(exc).__name__.lower()
                or isinstance(exc, requests.ConnectionError)
                or any(marker in message for marker in ("navigation", "browser exited", "cdp", "target closed"))
            )

        try:
            raw, attempts = self.retries.run(
                operation, provider=self.name, event_sink=self.event_sink, retryable=retryable,
                on_exhausted=lambda exc: self.proxies.report_failure(request.job_id, rotate=True),
            )
        finally:
            self.browser_state.pop("proxy", None)
        self.proxies.report_success(request.job_id)
        results = tuple(SearchResult(
            title=str(item.get("title", "")),
            body=str(item.get("body", "")),
            href=str(item.get("href", "")),
        ) for item in raw)
        return SearchPage(
            results=results, provider=self.name, page=request.page,
            next_page=self.browser_state.get("last_search_next_page"),
            proxy_id=self.proxies.identifier(proxy), attempts=attempts,
            metadata={"result_count": len(results), "browser": "cdp"},
            retry_information={"attempts": attempts, "retried": attempts > 1},
        )


class LocalSeedSearchProvider(_ProviderBase):
    name = "local"

    def __init__(self, seed_path: str | Path, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.seed_path = Path(seed_path)

    def search(self, request: SearchRequest) -> SearchPage:
        if not self.seed_path.exists():
            return SearchPage((), self.name, request.page)
        query_terms = {term.lower().strip('"()') for term in request.query.split() if len(term) > 2}
        results: list[SearchResult] = []
        with self.seed_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                text = " ".join(str(value) for value in row.values()).lower()
                if query_terms and not any(term in text for term in query_terms):
                    continue
                title = f"{row.get('Full_Name', '')} - {row.get('Designation', '')} at {row.get('Company', '')} | LinkedIn"
                body = f"Location: {row.get('Location') or row.get('City') or row.get('HQ', '')}. {text[:350]}"
                results.append(SearchResult(title, body, row.get("LinkedIn_URL", "")))
                if len(results) >= request.max_results:
                    break
        return SearchPage(tuple(results), self.name, request.page)


class LegacySearchClient:
    """Small DDGS-compatible facade for the existing workflow parsers."""

    def __init__(self, provider, job_id: str):
        self.provider = provider
        self.job_id = job_id

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        return False

    def text(self, query: str, *, max_results: int = 10, page: int = 1, **_kwargs):
        search_page = self.provider.search(SearchRequest(
            query=query,
            page=page,
            max_results=max_results,
            linkedin_only=False,
            job_id=self.job_id,
        ))
        return [result.as_dict() for result in search_page.results]


class ReliableHttpClient:
    """Requests facade using the same rate, retry, proxy, and event services."""

    def __init__(self, limiter, retries, proxies, event_sink=None):
        self.limiter = limiter
        self.retries = retries
        self.proxies = proxies
        self.event_sink = event_sink or NullEventSink()
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "SpeedyScraper-PublicContactFinder/2.0",
            "Accept": "text/html,application/xhtml+xml",
        })

    def get(self, url: str, *, job_id: str, **kwargs):
        proxy = self.proxies.acquire(job_id)
        self.limiter.acquire("public_contact")

        def operation():
            response = self.session.get(
                url,
                proxies={"http": proxy, "https": proxy} if proxy else None,
                **kwargs,
            )
            retry_seconds = parse_retry_after(response.headers.get("Retry-After"))
            if response.status_code == 429:
                raise RateLimitError("Public site rate limit", retry_after=retry_seconds)
            if response.status_code in {408, 500, 502, 503, 504}:
                raise requests.ConnectionError(f"Public site HTTP {response.status_code}")
            return response

        response, _attempts = self.retries.run(
            operation,
            provider="public_contact",
            event_sink=self.event_sink,
            on_exhausted=lambda _exc: self.proxies.report_failure(job_id, rotate=True),
        )
        self.proxies.report_success(job_id)
        return response


class ProviderRegistry:
    def __init__(
        self,
        config,
        event_sink=None,
        browser_state=None,
        *,
        limiter=None,
        retries=None,
        proxies=None,
    ):
        # Reliability services may be shared across registries.  The engine does
        # this so rate limits are provider-wide and proxy cooldowns survive page
        # and candidate checkpoints rather than resetting on every request.
        limiter = limiter or TokenBucketRateLimiter(config.rate_limits)
        retries = retries or RetryExecutor(config.retry)
        proxies = proxies or ProxyPool(config.proxy)
        common = (limiter, retries, proxies)
        browser_state = browser_state if browser_state is not None else {}
        self.providers = {
            "ddgs": DDGSSearchProvider(*common, event_sink=event_sink),
            "brave": BraveSearchProvider(*common, event_sink=event_sink),
            "google": GoogleSearchProvider(*common, browser_state=browser_state, event_sink=event_sink),
            "local": LocalSeedSearchProvider(
                Path(__file__).resolve().parents[1] / "India_B2B_Lead_Intelligence_Database.csv",
                *common, event_sink=event_sink,
            ),
        }

    def get(self, name: str):
        key = str(name or "ddgs").strip().lower()
        if key not in self.providers:
            raise ValueError(f"Unsupported search provider: {name}")
        return self.providers[key]
