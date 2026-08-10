from __future__ import annotations

import base64
import random
import time
import urllib.parse
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from speedy_scraper.linkedin import normalize_linkedin_url
from speedy_scraper.models import SearchPage, SearchResult


class SourceError(RuntimeError):
    def __init__(self, message: str, *, disable_source: bool = False):
        super().__init__(message)
        self.disable_source = disable_source


GOOGLE_CHALLENGE_SOURCE = "google_browser"


class _BrowserRuntime:
    """Own one Playwright driver per worker process and share it across sources."""

    def __init__(self) -> None:
        self._playwright: Any = None
        self._browsers: dict[tuple[bool, str | None], Any] = {}

    def playwright(self) -> Any:
        if self._playwright is not None:
            return self._playwright
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - import environment issue
            raise SourceError(f"Playwright is not available: {exc}") from exc
        self._playwright = sync_playwright().start()
        return self._playwright

    def browser(
        self,
        *,
        headless: bool,
        channel: str | None,
        args: list[str],
    ) -> Any:
        key = (headless, channel)
        browser = self._browsers.get(key)
        if browser is not None and browser.is_connected():
            return browser
        options: dict[str, Any] = {"headless": headless, "args": args}
        if channel:
            options["channel"] = channel
        try:
            browser = self.playwright().chromium.launch(**options)
        except Exception:
            if not channel:
                raise
            options.pop("channel", None)
            browser = self.playwright().chromium.launch(**options)
        self._browsers[key] = browser
        return browser

    def persistent_context(
        self,
        profile_dir: Path,
        *,
        headless: bool,
        channel: str | None,
        args: list[str],
        context_options: dict[str, Any],
    ) -> Any:
        options: dict[str, Any] = {
            "headless": headless,
            "args": args,
            **context_options,
        }
        if channel:
            options["channel"] = channel
        try:
            return self.playwright().chromium.launch_persistent_context(
                str(profile_dir),
                **options,
            )
        except Exception:
            if not channel:
                raise
            options.pop("channel", None)
            return self.playwright().chromium.launch_persistent_context(
                str(profile_dir),
                **options,
            )

    def close(self) -> None:
        for browser in self._browsers.values():
            try:
                browser.close()
            except Exception:
                pass
        self._browsers.clear()
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._playwright = None


_BROWSER_RUNTIME = _BrowserRuntime()


class SearchSource:
    name = "source"

    def search(self, query: str, *, max_results: int, headless: bool = True) -> list[SearchResult]:
        raise NotImplementedError

    def search_page(
        self,
        query: str,
        *,
        page: int,
        max_results: int,
        headless: bool = True,
    ) -> SearchPage:
        if page > 1:
            return SearchPage(results=[], page=page, has_next=False)
        return SearchPage(
            results=self.search(query, max_results=max_results, headless=headless),
            page=page,
            has_next=False,
        )

    def close(self) -> None:
        """Release reusable network or browser resources."""


class DdgsSource(SearchSource):
    name = "ddgs"

    def __init__(
        self,
        backends: tuple[str, ...] = ("brave", "yahoo"),
        *,
        personal_profiles_only: bool = True,
    ):
        self.backends = backends
        self.personal_profiles_only = personal_profiles_only
        self._client: Any = None

    def search(self, query: str, *, max_results: int, headless: bool = True) -> list[SearchResult]:
        from ddgs import DDGS

        if self._client is None:
            self._client = DDGS(timeout=20)

        last_error: Exception | None = None
        for backend in self.backends:
            try:
                kwargs = {
                    "region": "in-en",
                    "safesearch": "off",
                    "max_results": max(max_results, 10),
                }
                if backend != "auto":
                    kwargs["backend"] = backend
                raw = list(self._client.text(query, **kwargs))
                results = _search_results_from_raw(
                    raw,
                    query=query,
                    source=self.name,
                    max_results=max_results,
                    personal_profiles_only=self.personal_profiles_only,
                )
                if results:
                    return results
            except TypeError:
                try:
                    raw = list(self._client.text(query, max_results=max_results))
                    results = _search_results_from_raw(
                        raw,
                        query=query,
                        source=self.name,
                        max_results=max_results,
                        personal_profiles_only=self.personal_profiles_only,
                    )
                    if results:
                        return results
                except Exception as exc:  # pragma: no cover - depends on live provider
                    last_error = exc
            except Exception as exc:  # pragma: no cover - depends on live provider
                last_error = exc
        if last_error is None:
            return []
        raise SourceError(f"DDGS failed for query: {last_error}")


class BrowserSearchSource(SearchSource):
    def __init__(
        self,
        *,
        name: str,
        search_url: str,
        result_selectors: tuple[str, ...],
        wait_seconds: float = 2.0,
        profile_dir: Path | None = None,
        manual_challenge_seconds: int = 0,
        browser_channel: str | None = None,
        request_interval_seconds: float = 0,
        request_jitter_seconds: float = 0,
    ):
        self.name = name
        self.search_url = search_url
        self.result_selectors = result_selectors
        self.wait_seconds = wait_seconds
        self.profile_dir = profile_dir
        self.manual_challenge_seconds = max(0, manual_challenge_seconds)
        self.browser_channel = browser_channel
        self.request_interval_seconds = max(0.0, request_interval_seconds)
        self.request_jitter_seconds = max(0.0, request_jitter_seconds)
        self._context: Any = None
        self._page: Any = None
        self._headless: bool | None = None
        self._disabled_reason: str | None = None
        self._last_request_started_at: float | None = None

    def search(self, query: str, *, max_results: int, headless: bool = True) -> list[SearchResult]:
        return self.search_page(
            query,
            page=1,
            max_results=max_results,
            headless=headless,
        ).results

    def search_page(
        self,
        query: str,
        *,
        page: int,
        max_results: int,
        headless: bool = True,
    ) -> SearchPage:
        if self._disabled_reason:
            raise SourceError(self._disabled_reason, disable_source=True)
        page_number = max(1, int(page))
        page_size = min(10, max(1, int(max_results)))
        encoded = urllib.parse.quote_plus(query)
        url = self.search_url.format(
            query=encoded,
            page=page_number,
            start=(page_number - 1) * page_size,
            first=(page_number - 1) * page_size + 1,
            page_size=page_size,
        )
        try:
            page_obj = self._ensure_page(headless)
            self._wait_for_request_slot(page_obj)
            self._navigate(page_obj, url=url, query=query, page=page_number)
            selector = ", ".join(self.result_selectors)
            try:
                page_obj.locator(selector).first.wait_for(
                    state="attached",
                    timeout=max(1000, int(self.wait_seconds * 1000)),
                )
            except Exception:
                page_obj.wait_for_timeout(350)
            html = _safe_page_content(page_obj)
        except Exception as exc:  # pragma: no cover - browser/network dependent
            self.close()
            raise SourceError(f"{self.name} browser search failed: {exc}") from exc
        results = _parse_search_html(
            html,
            query=query,
            source=self.name,
            max_results=page_size,
            result_selectors=self.result_selectors,
        )
        if not results and _challenge_page(html) and not headless and self.manual_challenge_seconds:
            html = _wait_for_manual_challenge(page_obj, self.manual_challenge_seconds)
            results = _parse_search_html(
                html,
                query=query,
                source=self.name,
                max_results=page_size,
                result_selectors=self.result_selectors,
            )
        if not results and _challenge_page(html):
            self._disabled_reason = (
                f"{self.name} challenge detected; this source is disabled for the current job"
            )
            self.close()
            raise SourceError(
                self._disabled_reason,
                disable_source=True,
            )
        return SearchPage(
            results=results,
            page=page_number,
            has_next=bool(results) and (_has_next_page(html) or len(results) >= page_size),
        )

    def _ensure_page(self, headless: bool):
        if self._page is not None and self._headless == headless:
            return self._page
        self.close()
        stealth_args = [
            "--disable-blink-features=AutomationControlled",
            "--disable-infobars",
            "--disable-extensions",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1440,900",
        ]
        context_options: dict[str, Any] = {
            "viewport": {"width": 1440, "height": 900},
            "locale": "en-US",
            "user_agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "extra_http_headers": {
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Sec-Ch-Ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
                "Sec-Ch-Ua-Mobile": "?0",
                "Sec-Ch-Ua-Platform": '"macOS"',
            },
        }
        if self.profile_dir is not None:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._context = _BROWSER_RUNTIME.persistent_context(
                self.profile_dir,
                headless=headless,
                channel=self.browser_channel,
                args=stealth_args,
                context_options=context_options,
            )
        else:
            browser = _BROWSER_RUNTIME.browser(
                headless=headless,
                channel=self.browser_channel,
                args=stealth_args,
            )
            self._context = browser.new_context(**context_options)
        blocked_resource_types = _blocked_resource_types(
            headless=headless,
            manual_challenge_seconds=self.manual_challenge_seconds,
        )
        self._context.route(
            "**/*",
            lambda route: route.abort()
            if route.request.resource_type in blocked_resource_types
            else route.continue_(),
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        # Mask the navigator.webdriver fingerprint
        self._page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        self._headless = headless
        return self._page

    def _navigate(self, page_obj: Any, *, url: str, query: str, page: int) -> None:
        if self.name != GOOGLE_CHALLENGE_SOURCE or page > 1:
            page_obj.goto(url, wait_until="domcontentloaded", timeout=45000)
            return

        current_host = urllib.parse.urlparse(str(page_obj.url or "")).hostname or ""
        if not current_host.endswith("google.com") or "/sorry/" in str(page_obj.url):
            page_obj.goto(
                "https://www.google.com/?hl=en",
                wait_until="domcontentloaded",
                timeout=45000,
            )
        query_box = page_obj.locator("textarea[name='q'], input[name='q']").first
        try:
            query_box.wait_for(state="visible", timeout=5000)
        except Exception:
            # Preserve the challenge/consent HTML so the caller can classify it accurately.
            return
        query_box.fill(query)
        pre_nav_url = str(page_obj.url or "")
        query_box.press("Enter")
        try:
            # Wait for the URL to change, confirming navigation has actually started,
            # then wait for the page to reach domcontentloaded.
            page_obj.wait_for_function(
                "url => window.location.href !== url",
                pre_nav_url,
                timeout=8000,
            )
            page_obj.wait_for_load_state("domcontentloaded", timeout=45000)
        except Exception:
            # Google can update the result page without a traditional navigation event.
            page_obj.wait_for_timeout(800)

    def _wait_for_request_slot(self, page_obj: Any) -> None:
        if self._last_request_started_at is not None:
            target_interval = self.request_interval_seconds + random.uniform(
                0,
                self.request_jitter_seconds,
            )
            remaining = target_interval - (time.monotonic() - self._last_request_started_at)
            if remaining > 0:
                page_obj.wait_for_timeout(int(remaining * 1000))
        self._last_request_started_at = time.monotonic()

    def close(self) -> None:
        for resource in (self._page, self._context):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        self._page = None
        self._context = None
        self._headless = None


def build_sources(names: list[str]) -> list[SearchSource]:
    sources: list[SearchSource] = []
    for name in names:
        key = name.strip().lower()
        if key == "ddgs":
            sources.append(DdgsSource())
        elif key == "google_browser":
            sources.append(
                BrowserSearchSource(
                    name="google_browser",
                    search_url=(
                        "https://www.google.com/search?q={query}&start={start}&num={page_size}"
                        "&filter=0&hl=en&gl=in&pws=0&tbs=li:1&nfpr=1"
                    ),
                    result_selectors=("div.MjjYud", "div.tF2Cxc", "div.g"),
                    wait_seconds=2.5,
                    profile_dir=(
                        Path(__file__).resolve().parents[1]
                        / "data"
                        / "browser_profiles"
                        / "google-search-chrome"
                    ),
                    manual_challenge_seconds=0,
                    browser_channel="chrome",
                    request_interval_seconds=6,
                    request_jitter_seconds=3,
                )
            )
        elif key == "bing_browser":
            sources.append(
                BrowserSearchSource(
                    name="bing_browser",
                    search_url="https://www.bing.com/search?q={query}&first={first}&count={page_size}",
                    result_selectors=("li.b_algo",),
                    browser_channel="chrome",
                )
            )
        elif key == "duckduckgo_browser":
            sources.append(
                BrowserSearchSource(
                    name="duckduckgo_browser",
                    search_url=(
                        "https://html.duckduckgo.com/html/?q={query}&s={start}&kl=in-en"
                    ),
                    result_selectors=("div.result", "div.results_links", "article"),
                    browser_channel="chrome",
                )
            )
        else:
            raise ValueError(f"Unsupported source: {name}")
    return sources


def close_sources(sources: list[SearchSource]) -> None:
    for source in sources:
        try:
            source.close()
        except Exception:
            pass
    _BROWSER_RUNTIME.close()


def configure_google_challenge_wait(
    sources: list[SearchSource],
    timeout_seconds: int,
) -> list[SearchSource]:
    """Enable the opt-in manual recovery window without changing source factories."""
    timeout = max(0, min(300, int(timeout_seconds)))
    for source in sources:
        if source.name == GOOGLE_CHALLENGE_SOURCE and hasattr(
            source, "manual_challenge_seconds"
        ):
            source.manual_challenge_seconds = timeout
    return sources


def source_family(name: str) -> str:
    key = str(name or "").strip().lower()
    return "google" if key.startswith("google") else key


def independent_source_families(names: list[str] | set[str]) -> set[str]:
    return {family for name in names if (family := source_family(name))}


def search_source_page(
    source: SearchSource,
    query: str,
    *,
    page: int,
    max_results: int,
    headless: bool = True,
) -> SearchPage:
    """Call a page-aware source while keeping injected legacy/fake sources compatible."""
    method = getattr(source, "search_page", None)
    if callable(method):
        return method(
            query,
            page=page,
            max_results=max_results,
            headless=headless,
        )
    if page > 1:
        return SearchPage(results=[], page=page, has_next=False)
    return SearchPage(
        results=source.search(query, max_results=max_results, headless=headless),
        page=page,
        has_next=False,
    )


def _parse_search_html(
    html: str,
    *,
    query: str,
    source: str,
    max_results: int,
    result_selectors: tuple[str, ...] = (),
) -> list[SearchResult]:
    soup = BeautifulSoup(html, "html.parser")
    results: list[SearchResult] = []
    seen: set[str] = set()

    selector = ", ".join(result_selectors) or "li.b_algo, article, div[data-testid='result']"
    cards = soup.select(selector)
    if not cards:
        cards = soup.select("a[href*='linkedin.com/in/']")
    for card in cards:
        anchors = [card] if getattr(card, "name", "") == "a" else card.select("a[href]")
        for anchor in anchors:
            href = _unwrap_search_href(str(anchor.get("href", "")))
            if not normalize_linkedin_url(href):
                continue
            title = anchor.get_text(" ", strip=True)
            body = card.get_text(" ", strip=True) if hasattr(card, "get_text") else title
            key = normalize_linkedin_url(href)
            if key in seen:
                continue
            seen.add(key)
            results.append(SearchResult(title=title, body=body, href=href, source=source, query=query))
            if len(results) >= max_results:
                return results
    return results


def _search_results_from_raw(
    raw: list[dict[str, Any]],
    *,
    query: str,
    source: str,
    max_results: int,
    personal_profiles_only: bool = True,
) -> list[SearchResult]:
    results: list[SearchResult] = []
    seen: set[str] = set()
    for item in raw:
        href = _unwrap_search_href(str(item.get("href", "")))
        canonical = normalize_linkedin_url(href)
        key = canonical or href.split("#", 1)[0]
        if personal_profiles_only and not canonical:
            continue
        if not key or key in seen:
            continue
        seen.add(key)
        results.append(
            SearchResult(
                title=str(item.get("title", "")),
                body=str(item.get("body", "")),
                href=href,
                source=source,
                query=query,
            )
        )
        if len(results) >= max_results:
            break
    return results


def _unwrap_search_href(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    parsed = urllib.parse.urlparse(raw)
    host = (parsed.hostname or "").lower()
    query = urllib.parse.parse_qs(parsed.query)
    if (not host or host.endswith("google.com")) and parsed.path in {"/url", "/imgres"}:
        return (query.get("q") or query.get("url") or [raw])[0]
    if host.endswith("duckduckgo.com") and query.get("uddg"):
        return query["uddg"][0]
    if host.endswith("bing.com") and query.get("u"):
        encoded = query["u"][0]
        if encoded.startswith("a1"):
            try:
                payload = encoded[2:] + "=" * (-len(encoded[2:]) % 4)
                return base64.urlsafe_b64decode(payload).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return raw
        if encoded.startswith(("http://", "https://")):
            return encoded
    return raw


def _challenge_page(html: str) -> bool:
    text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True).lower()
    markers = (
        "one last step",
        "please solve the challenge",
        "verifying you're not a bot",
        "verify you are human",
        "unexpected error. please try again",
        "support email address includes an anonymized error code",
        "our systems have detected unusual traffic",
        "automated queries",
        "recaptcha",
        "unfortunately, bots use duckduckgo too",
        "select all squares containing a duck",
        "complete the following challenge to confirm this search was made by a human",
        "unfortunately, bots use duckduckgo too",
        "select all squares containing a duck",
    )
    return any(marker in text for marker in markers)


def _blocked_resource_types(
    *,
    headless: bool,
    manual_challenge_seconds: int,
) -> set[str]:
    blocked = {"font", "media"}
    if headless:
        blocked.add("image")
    return blocked


def _has_next_page(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    return bool(
        soup.select_one(
            "a#pnnext, a[aria-label='Next page'], a[aria-label='Next'], "
            "a.sb_pagN, a.result--more__btn"
        )
    )


def _wait_for_manual_challenge(page_obj: Any, timeout_seconds: int) -> str:
    deadline = time.monotonic() + max(1, timeout_seconds)
    html = _safe_page_content(page_obj)
    while _challenge_page(html) and time.monotonic() < deadline:
        page_obj.wait_for_timeout(1000)
        html = _safe_page_content(page_obj)
    return html


def _safe_page_content(page_obj: Any, *, retries: int = 3, wait_ms: int = 600) -> str:
    """Return page HTML, retrying if Playwright raises a mid-navigation error."""
    for attempt in range(retries):
        try:
            return page_obj.content()
        except Exception as exc:
            msg = str(exc).lower()
            if "navigating" in msg or "detached" in msg or "target closed" in msg:
                if attempt < retries - 1:
                    page_obj.wait_for_timeout(wait_ms)
                    try:
                        page_obj.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:
                        pass
                    continue
            raise
    return page_obj.content()
