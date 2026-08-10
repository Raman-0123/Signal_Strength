"""Keyless Google result collection with a manual CAPTCHA handoff.

The browser is launched as a separate Chromium process and controlled through
the Chrome DevTools Protocol.  Keeping that process outside Playwright's own
lifecycle lets Streamlit rerun while a human completes a Google verification
page in the visible browser window.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import signal
import socket
import subprocess
import tempfile
import time
import urllib.parse
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright


class GoogleSecurityCheck(RuntimeError):
    """Raised when Google requires a human to verify the browser session."""

    def __init__(
        self,
        query: str,
        url: str,
        reason: str = "Google CAPTCHA",
        phase: str = "discovery",
        page: int = 1,
        linkedin_only: bool = True,
        max_results: int = 10,
    ):
        super().__init__(f"GOOGLE_SECURITY_CHECK:{reason}")
        self.query = query
        self.url = url
        self.reason = reason
        self.phase = phase
        self.page = max(1, int(page))
        self.linkedin_only = bool(linkedin_only)
        self.max_results = min(max(1, int(max_results)), 10)

    def as_dict(self) -> dict:
        return {
            "engine": "Google",
            "query": self.query,
            "url": self.url,
            "reason": self.reason,
            "phase": self.phase,
            "page": self.page,
            "linkedin_only": self.linkedin_only,
            "max_results": self.max_results,
        }


def _free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _cdp_ready(port: int) -> bool:
    if not port:
        return False
    try:
        response = requests.get(
            f"http://127.0.0.1:{port}/json/version",
            timeout=0.8,
        )
        return response.ok and "webSocketDebuggerUrl" in response.json()
    except (requests.RequestException, ValueError):
        return False


def google_browser_running(browser_state: dict | None) -> bool:
    """Return true only when the checkpoint still owns the live CDP process."""
    if not browser_state:
        return False
    return _registered_process_matches(
        int(browser_state.get("pid") or 0),
        int(browser_state.get("port") or 0),
        str(browser_state.get("profile_dir") or ""),
    )


def _chromium_executable(browser_state: dict | None = None) -> str:
    configured = str((browser_state or {}).get("chrome_path", "")).strip()
    configured = configured or os.environ.get("SPEEDY_SCRAPER_CHROME_PATH", "").strip()
    if configured:
        if not Path(configured).is_file():
            raise RuntimeError(
                "SPEEDY_SCRAPER_CHROME_PATH does not point to a browser executable."
            )
        return configured
    with sync_playwright() as playwright:
        executable = playwright.chromium.executable_path
    if not Path(executable).is_file():
        raise RuntimeError(
            "Playwright Chromium is not installed. Run: playwright install chromium"
        )
    return executable


def _profile_directory(browser_state: dict) -> str:
    existing = str(browser_state.get("profile_dir", "")).strip()
    if existing:
        Path(existing).mkdir(parents=True, exist_ok=True)
        return existing
    profile_dir = tempfile.mkdtemp(prefix="speedy-scraper-google-")
    browser_state["profile_dir"] = profile_dir
    return profile_dir


def _browser_registry_path() -> Path:
    workspace = str(Path(__file__).resolve().parents[1])
    digest = hashlib.sha256(workspace.encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / (
        f"speedy-scraper-google-owner-{digest}.json"
    )


def _registered_process_matches(pid: int, port: int, profile_dir: str) -> bool:
    """Verify a registry target before stopping a stale controlled browser."""
    if pid <= 1 or not port or not _cdp_ready(port):
        return False
    if not Path(profile_dir).name.startswith("speedy-scraper-google-"):
        return False
    try:
        command = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return (
        "Google Chrome for Testing" in command
        and f"--remote-debugging-port={port}" in command
        and f"--user-data-dir={profile_dir}" in command
    )


def _retire_registered_browser() -> None:
    """Enforce one controlled Google browser for this workspace."""
    registry = _browser_registry_path()
    try:
        owner = json.loads(registry.read_text())
    except (OSError, ValueError, TypeError):
        return
    pid = int(owner.get("pid") or 0)
    port = int(owner.get("port") or 0)
    profile_dir = str(owner.get("profile_dir") or "")
    if _registered_process_matches(pid, port, profile_dir):
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    try:
        registry.unlink(missing_ok=True)
    except OSError:
        pass


def _register_browser(browser_state: dict) -> None:
    payload = {
        "pid": int(browser_state.get("pid") or 0),
        "port": int(browser_state.get("port") or 0),
        "profile_dir": str(browser_state.get("profile_dir") or ""),
    }
    registry = _browser_registry_path()
    temporary = registry.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(payload))
        temporary.replace(registry)
    except OSError:
        pass


def _wait_for_cdp(port: int, process: subprocess.Popen, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(
                f"Google browser exited during startup (code {process.returncode})."
            )
        if _cdp_ready(port):
            return
        time.sleep(0.15)
    raise RuntimeError("Google browser did not expose its control port in time.")


def _start_browser(
    browser_state: dict,
    *,
    headed: bool,
    initial_url: str = "about:blank",
) -> dict:
    _retire_registered_browser()
    port = _free_local_port()
    profile_dir = _profile_directory(browser_state)
    command = [
        _chromium_executable(browser_state),
        f"--remote-debugging-port={port}",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-features=Translate",
        "--disable-popup-blocking",
        "--window-size=1366,900",
        "--lang=en-US,en;q=0.9",
        (
            "--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    ]
    proxy = str(browser_state.get("proxy", "")).strip()
    if proxy:
        # Proxy credentials originate in environment-backed configuration. The
        # browser registry stores only pid/port/profile path and never the URL.
        command.append(f"--proxy-server={proxy}")
    if not headed:
        command.append("--headless=new")
    command.append(initial_url or "about:blank")
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        _wait_for_cdp(port, process)
    except Exception:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        raise
    browser_state.update({
        "pid": process.pid,
        "port": port,
        "headed": bool(headed),
    })
    _register_browser(browser_state)
    return browser_state


def close_google_browser(browser_state: dict | None) -> None:
    """Stop only the Chromium process belonging to this scraper session."""
    if not browser_state:
        return
    pid = int(browser_state.get("pid") or 0)
    port = int(browser_state.get("port") or 0)
    profile_dir = str(browser_state.get("profile_dir") or "")
    if pid > 1 and _registered_process_matches(pid, port, profile_dir):
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and _cdp_ready(port):
            time.sleep(0.1)
    browser_state.pop("pid", None)
    browser_state.pop("port", None)
    browser_state.pop("headed", None)
    browser_state.pop("resume_result", None)
    browser_state.pop("captcha_result_probe", None)
    registry = _browser_registry_path()
    try:
        owner = json.loads(registry.read_text())
        if int(owner.get("pid") or 0) == pid:
            registry.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        pass


def _ensure_browser(browser_state: dict) -> dict:
    port = int(browser_state.get("port") or 0)
    if _cdp_ready(port):
        return browser_state
    browser_state.pop("pid", None)
    browser_state.pop("port", None)
    # Interactive runs remain visible. Scheduled runs may start headless and
    # are relaunched with the same persistent profile only if human
    # verification becomes necessary.
    return _start_browser(
        browser_state,
        headed=not bool(browser_state.get("headless", False)),
    )


def _show_manual_challenge(browser_state: dict, challenge_url: str) -> None:
    """Keep the exact challenge visible, or reopen it if the browser was headless."""
    if browser_state.get("headed") and _cdp_ready(
        int(browser_state.get("port") or 0)
    ):
        return
    close_google_browser(browser_state)
    browser_state["headless"] = False
    _start_browser(
        browser_state,
        headed=True,
        initial_url=challenge_url or "https://www.google.com/",
    )


def _is_security_check(url: str, title: str, body: str) -> bool:
    combined = f"{url}\n{title}\n{body}".lower()
    markers = (
        "/sorry/",
        "unusual traffic",
        "not a robot",
        "our systems have detected",
        "recaptcha",
        "automated queries",
    )
    return any(marker in combined for marker in markers)


def _unwrap_google_url(value: str) -> str:
    href = str(value or "").strip()
    if not href:
        return ""
    parsed = urllib.parse.urlparse(href)
    if parsed.netloc.endswith("google.com") and parsed.path in {"/url", "/imgres"}:
        params = urllib.parse.parse_qs(parsed.query)
        href = (params.get("q") or params.get("url") or [""])[0]
    return urllib.parse.unquote(href)


def _normalize_google_items(
    items: list[dict],
    max_results: int,
    *,
    linkedin_only: bool = True,
) -> list[dict]:
    """Convert Google citation cards to the search-result shape used by engines."""
    normalized = []
    seen = set()
    for item in items:
        href = _unwrap_google_url(item.get("href", ""))
        if linkedin_only and not _is_linkedin_profile_url(href):
            continue
        if not linkedin_only and not href.lower().startswith(("http://", "https://")):
            continue
        if href in seen:
            continue
        title = " ".join(str(item.get("title", "")).split())
        body = " ".join(str(item.get("body", "")).split())
        if not title:
            continue
        seen.add(href)
        normalized.append({"href": href, "title": title, "body": body})
        if len(normalized) >= max_results:
            break
    return normalized


def _is_linkedin_profile_url(value: str) -> bool:
    """Accept only a real LinkedIn host whose path is an ``/in/`` profile."""
    href = str(value or "").strip()
    if not href:
        return False
    if "://" not in href:
        href = f"https://{href.lstrip('/')}"
    parsed = urllib.parse.urlparse(href)
    host = (parsed.hostname or "").lower().rstrip(".")
    path = urllib.parse.unquote(parsed.path or "")
    return (
        (host == "linkedin.com" or host.endswith(".linkedin.com"))
        and path.lower().startswith("/in/")
        and bool(path[4:].strip("/"))
    )


_GOOGLE_CARD_SCRIPT = """
() => {
    const rows = [];
    const seen = new Set();
    const headings = Array.from(document.querySelectorAll('a h3'));
    for (const heading of headings) {
        const anchor = heading.closest('a');
        if (!anchor || !anchor.href || seen.has(anchor.href)) continue;
        const card = heading.closest('div.MjjYud, div.tF2Cxc, div.g')
            || anchor.parentElement?.parentElement
            || anchor.parentElement;
        const snippet = card?.querySelector(
            'div.VwiC3b, div[data-sncf], div.IsZvec, span.aCOpRe'
        );
        rows.push({
            href: anchor.href,
            title: heading.innerText || anchor.innerText || '',
            body: snippet?.innerText || card?.innerText || ''
        });
        seen.add(anchor.href);
    }
    return rows;
}
"""


def _extract_google_items(page_obj) -> list[dict]:
    return list(page_obj.evaluate(_GOOGLE_CARD_SCRIPT) or [])


def _normalized_query(value: str) -> str:
    return " ".join(urllib.parse.unquote_plus(str(value or "")).split())


def _expected_results_page(url: str, query: str, page: int) -> bool:
    """Confirm that the visible tab is the solved query and expected offset."""
    parsed = urllib.parse.urlparse(str(url or ""))
    if not parsed.netloc.lower().endswith("google.com"):
        return False
    if parsed.path != "/search":
        return False
    params = urllib.parse.parse_qs(parsed.query)
    visible_query = (params.get("q") or [""])[0]
    if _normalized_query(visible_query) != _normalized_query(query):
        return False
    expected_start = (max(1, int(page)) - 1) * 10
    try:
        visible_start = int((params.get("start") or ["0"])[0])
    except (TypeError, ValueError):
        return False
    return visible_start == expected_start


def _google_next_page_number(page_obj, current_page: int) -> int | None:
    """Read Google's real Next link instead of assuming a fixed page count."""
    selectors = (
        "a#pnnext",
        'a[aria-label="Next page"]',
        'a[aria-label="Next"]',
    )
    for selector in selectors:
        try:
            locator = page_obj.locator(selector)
            if locator.count() < 1:
                continue
            href = locator.first.get_attribute("href")
            if not href:
                continue
            parsed = urllib.parse.urlparse(
                urllib.parse.urljoin("https://www.google.com", href)
            )
            params = urllib.parse.parse_qs(parsed.query)
            start = int((params.get("start") or ["0"])[0])
            next_page = (start // 10) + 1
            if next_page > max(1, int(current_page)):
                return next_page
        except (AttributeError, TypeError, ValueError):
            continue
    return None


def google_security_check_resolved(
    browser_state: dict | None,
    check: dict | None,
) -> bool:
    """Detect a manually solved CAPTCHA and cache that exact results page.

    This never solves or bypasses a challenge. It only observes the visible
    browser after the user has completed it and Google has redirected back to
    the original search results.
    """
    if not browser_state or not check:
        return False
    port = int(browser_state.get("port") or 0)
    if not _cdp_ready(port):
        return False

    query = str(check.get("query", ""))
    page_number = max(1, int(check.get("page", 1)))
    linkedin_only = bool(check.get("linkedin_only", True))
    max_results = min(max(1, int(check.get("max_results", 10))), 10)

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}",
                timeout=5_000,
            )
            context = browser.contexts[0]
            if not context.pages:
                return False
            page_obj = context.pages[-1]
            page_url = page_obj.url
            page_title = page_obj.title()
            body_text = page_obj.locator("body").inner_text(timeout=3_000)[:5000]
            if _is_security_check(page_url, page_title, body_text):
                browser_state.pop("captcha_result_probe", None)
                return False
            if not _expected_results_page(page_url, query, page_number):
                browser_state.pop("captcha_result_probe", None)
                return False
            if page_obj.evaluate("document.readyState") != "complete":
                return False
            raw_items = _extract_google_items(page_obj)
            explicit_empty = any(
                marker in body_text.lower()
                for marker in (
                    "did not match any documents",
                    "no results found",
                    "your search -",
                )
            )
            if not raw_items and not explicit_empty:
                return False
            probe = {
                "url": page_url,
                "raw_count": len(raw_items),
            }
            if browser_state.get("captcha_result_probe") != probe:
                browser_state["captcha_result_probe"] = probe
                return False
            results = _normalize_google_items(
                raw_items,
                max_results,
                linkedin_only=linkedin_only,
            )
            next_page = _google_next_page_number(page_obj, page_number)
            if not linkedin_only and len(context.pages) > 1:
                # Evidence validation runs in a temporary background tab. Once
                # a human completes its CAPTCHA, cache the exact page, close
                # that tab, and return the user to profile discovery.
                page_obj.close()
                if context.pages:
                    context.pages[0].bring_to_front()
    except Exception:
        return False

    browser_state["resume_result"] = {
        "query": query,
        "page": page_number,
        "linkedin_only": linkedin_only,
        "results": results,
        "next_page": next_page,
    }
    browser_state.pop("captcha_result_probe", None)
    return True


def _take_resumed_results(
    browser_state: dict,
    query: str,
    page: int,
    linkedin_only: bool,
    max_results: int,
):
    cached = browser_state.get("resume_result")
    if not isinstance(cached, dict):
        return None
    if (
        _normalized_query(cached.get("query", "")) != _normalized_query(query)
        or int(cached.get("page", 1)) != max(1, int(page))
        or bool(cached.get("linkedin_only", True)) != bool(linkedin_only)
    ):
        return None
    browser_state.pop("resume_result", None)
    browser_state["last_search_next_page"] = cached.get("next_page")
    return list(cached.get("results", []))[:max_results]


def _google_search_params(query: str, page: int, max_results: int) -> dict:
    """Return exact, non-overlapping Google result-page parameters."""
    page_number = max(1, int(page))
    return {
        "q": query,
        "start": (page_number - 1) * 10,
        "num": min(max(1, int(max_results)), 10),
        "filter": "0",
        "hl": "en",
        "gl": "in",
        "pws": "0",
        # Verbatim mode prevents Google from silently rewriting a precise
        # LinkedIn role/location query into a generic "talent" web search.
        "tbs": "li:1",
        "nfpr": "1",
    }


def google_text_search(
    query: str,
    *,
    page: int = 1,
    max_results: int = 50,
    browser_state: dict | None = None,
    linkedin_only: bool = True,
) -> list[dict]:
    """Collect Google result citation cards without an API key.

    The controlled browser remains visible while results are collected. On a
    security page it stays open on that exact challenge and this function raises
    ``GoogleSecurityCheck``. The caller should keep its query index unchanged
    and retry after manual verification.
    """
    if browser_state is None:
        browser_state = {}
    browser_state.pop("last_search_next_page", None)
    resumed_results = _take_resumed_results(
        browser_state,
        query,
        page,
        linkedin_only,
        max_results,
    )
    if resumed_results is not None:
        return resumed_results
    _ensure_browser(browser_state)
    port = int(browser_state["port"])
    params = _google_search_params(query, page, max_results)
    search_url = "https://www.google.com/search?" + urllib.parse.urlencode(params)
    challenge = None
    items = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{port}",
            timeout=15_000,
        )
        context = browser.contexts[0]
        pages = context.pages
        discovery_page = pages[0] if pages else context.new_page()
        temporary_evidence_page = not linkedin_only
        if linkedin_only:
            # Discovery owns the single visible tab. Close any stale validation
            # tabs restored after a prior interrupted run.
            page_obj = discovery_page
            for stale_page in pages[1:]:
                try:
                    stale_page.close()
                except Exception:
                    pass
            page_obj.bring_to_front()
        else:
            # Company/person validation is necessary for strict filters, but it
            # must not make the visible browser look like discovery has drifted.
            # Navigate a temporary tab in the background and restore discovery
            # immediately after evidence extraction.
            page_obj = context.new_page()
            discovery_page.bring_to_front()
        navigation_timeout = int(
            float(browser_state.get("navigation_timeout_seconds", 60.0)) * 1000
        )
        page_obj.goto(
            search_url, wait_until="domcontentloaded", timeout=navigation_timeout,
        )
        settle_min = float(browser_state.get("page_settle_min_seconds", 1.2))
        settle_max = max(
            settle_min, float(browser_state.get("page_settle_max_seconds", 2.2)),
        )
        page_obj.wait_for_timeout(int(random.uniform(settle_min, settle_max) * 1000))

        page_url = page_obj.url
        page_title = page_obj.title()
        body_text = page_obj.locator("body").inner_text(timeout=5_000)[:5000]
        if _is_security_check(page_url, page_title, body_text):
            challenge = (page_url, page_title or "Google verification required")
            # CAPTCHA always needs manual intervention, so reveal the exact
            # challenged validation tab only in this case.
            page_obj.bring_to_front()
        else:
            items = _extract_google_items(page_obj)
            browser_state["last_search_next_page"] = (
                _google_next_page_number(page_obj, page)
            )
            if temporary_evidence_page:
                page_obj.close()
                discovery_page.bring_to_front()

    if challenge:
        challenge_url, reason = challenge
        try:
            _show_manual_challenge(browser_state, challenge_url)
        except Exception as exc:
            # A headless scheduled host may not currently have a display. Keep
            # the exact challenge checkpoint waiting until a display exists.
            browser_state["verification_display_error"] = type(exc).__name__
        raise GoogleSecurityCheck(
            query,
            challenge_url,
            reason,
            page=page,
            linkedin_only=linkedin_only,
            max_results=max_results,
        )

    post_min = float(browser_state.get("post_search_min_seconds", 0.7))
    post_max = max(post_min, float(browser_state.get("post_search_max_seconds", 1.2)))
    time.sleep(random.uniform(post_min, post_max))
    return _normalize_google_items(
        items,
        max_results,
        linkedin_only=linkedin_only,
    )
