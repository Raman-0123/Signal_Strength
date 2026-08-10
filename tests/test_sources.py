import sys
from types import SimpleNamespace

import pytest

from speedy_scraper.sources import (
    DdgsSource,
    SourceError,
    _blocked_resource_types,
    _BrowserRuntime,
    _challenge_page,
    _parse_search_html,
    build_sources,
    configure_google_challenge_wait,
)


def test_ddgs_provider_block_is_classified_as_challenge(monkeypatch):
    class BlockedDDGS:
        def __init__(self, **_options):
            pass

        def text(self, _query, **_options):
            raise RuntimeError("429 Too Many Requests: captcha required")

    monkeypatch.setitem(sys.modules, "ddgs", SimpleNamespace(DDGS=BlockedDDGS))
    source = DdgsSource(backends=("brave",))

    with pytest.raises(SourceError) as caught:
        source.search("site:linkedin.com/in CTO", max_results=10)

    assert caught.value.challenge is True
    assert caught.value.disable_source is True


def test_browser_html_parser_extracts_linkedin_cards():
    html = """
    <html><body>
      <li class="b_algo">
        <a href="https://www.linkedin.com/in/asha-rao/?trk=abc">Asha Rao - CTO - Razorpay | LinkedIn</a>
        <p>Location: Bengaluru · payments fintech platform.</p>
      </li>
    </body></html>
    """
    results = _parse_search_html(html, query="q", source="bing_browser", max_results=10)
    assert len(results) == 1
    assert results[0].href.startswith("https://www.linkedin.com/in/asha-rao/")
    assert "Bengaluru" in results[0].body


def test_source_factory_includes_free_browser_sources():
    names = [
        source.name
        for source in build_sources(
            ["google_browser", "ddgs", "bing_browser", "duckduckgo_browser"]
        )
    ]
    assert names == [
        "google_browser",
        "ddgs",
        "bing_browser",
        "duckduckgo_browser",
    ]


def test_browser_html_parser_unwraps_duckduckgo_redirects():
    html = """
    <article>
      <a href="https://duckduckgo.com/l/?uddg=https%3A%2F%2Fin.linkedin.com%2Fin%2Fasha-rao%2F">
        Asha Rao - CTO - Razorpay | LinkedIn
      </a>
      <p>Location: Bengaluru</p>
    </article>
    """

    results = _parse_search_html(html, query="q", source="fixture", max_results=10)

    assert [result.href for result in results] == ["https://in.linkedin.com/in/asha-rao/"]


def test_fallback_anchor_parser_includes_the_anchor_itself():
    html = '<a href="https://www.linkedin.com/in/asha-rao/">Asha Rao - CTO</a>'

    results = _parse_search_html(html, query="q", source="fixture", max_results=10)

    assert len(results) == 1


def test_google_html_parser_extracts_current_result_cards():
    html = """
    <div class="MjjYud"><a href="/url?q=https%3A%2F%2Fwww.linkedin.com%2Fin%2Fasha-rao%2F">
      <h3>Asha Rao - CTO - Razorpay | LinkedIn</h3></a>
      <div class="VwiC3b">Location: Bengaluru · payments</div>
    </div><a id="pnnext" href="/search?q=x&amp;start=10">Next</a>
    """

    results = _parse_search_html(
        html,
        query="q",
        source="google_browser",
        max_results=10,
        result_selectors=("div.MjjYud",),
    )

    assert len(results) == 1
    assert results[0].href == "https://www.linkedin.com/in/asha-rao/"


def test_google_source_factory_requires_explicit_manual_wait_configuration():
    source = build_sources(["google_browser"])[0]

    assert source.manual_challenge_seconds == 0
    assert _challenge_page("Our systems have detected unusual traffic from your network")


def test_manual_google_recovery_keeps_captcha_images_enabled():
    source = build_sources(["google_browser"])[0]
    configure_google_challenge_wait([source], 120)

    assert source.manual_challenge_seconds == 120
    assert "image" not in _blocked_resource_types(
        headless=False,
        manual_challenge_seconds=source.manual_challenge_seconds,
    )
    assert "image" in _blocked_resource_types(
        headless=True,
        manual_challenge_seconds=source.manual_challenge_seconds,
    )


def test_duckduckgo_challenge_is_detected():
    assert _challenge_page(
        "Unfortunately, bots use DuckDuckGo too. Select all squares containing a duck."
    )


def test_visible_browser_never_blocks_challenge_images():
    assert "image" not in _blocked_resource_types(
        headless=False,
        manual_challenge_seconds=0,
    )


def test_browser_runtime_reuses_one_browser_driver():
    class FakeBrowser:
        def is_connected(self):
            return True

        def close(self):
            return None

    class FakeChromium:
        def __init__(self):
            self.launches = 0

        def launch(self, **_options):
            self.launches += 1
            return FakeBrowser()

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()

        def stop(self):
            return None

    runtime = _BrowserRuntime()
    runtime._playwright = FakePlaywright()

    first = runtime.browser(headless=False, channel=None, args=[])
    second = runtime.browser(headless=False, channel=None, args=[])

    assert first is second
    assert runtime._playwright.chromium.launches == 1
    runtime.close()


def test_google_first_page_submits_query_through_search_box():
    class FakeQueryBox:
        def __init__(self):
            self.value = ""
            self.keys = []

        @property
        def first(self):
            return self

        def wait_for(self, **_options):
            return None

        def fill(self, value):
            self.value = value

        def press(self, key):
            self.keys.append(key)

    class FakePage:
        def __init__(self):
            self.url = "about:blank"
            self.visited = []
            self.query_box = FakeQueryBox()

        def goto(self, url, **_options):
            self.url = url
            self.visited.append(url)

        def locator(self, _selector):
            return self.query_box

        def wait_for_load_state(self, *_args, **_options):
            return None

    source = build_sources(["google_browser"])[0]
    page = FakePage()

    source._navigate(
        page,
        url="https://www.google.com/search?q=ignored",
        query='site:linkedin.com/in "CTO" Razorpay Bengaluru',
        page=1,
    )

    assert page.visited == ["https://www.google.com/?hl=en"]
    assert page.query_box.value == 'site:linkedin.com/in "CTO" Razorpay Bengaluru'
    assert page.query_box.keys == ["Enter"]
