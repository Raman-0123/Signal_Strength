import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from speedy_scraper.config import load_config
from speedy_scraper.domain import ProxyConfig, RetryConfig, SearchRequest
from speedy_scraper.providers import (
    BraveSearchProvider,
    DDGSSearchProvider,
    GoogleSearchProvider,
    LegacySearchClient,
    LocalSeedSearchProvider,
    ProviderRegistry,
    ReliableHttpClient,
)
from speedy_scraper.reliability import (
    ProviderBlockedError,
    ProxyPool,
    RateLimitError,
    RetryExecutor,
    default_retryable,
)


class _Limiter:
    def __init__(self):
        self.providers = []

    def acquire(self, provider):
        self.providers.append(provider)


class _FakeDDGS:
    last_search_kwargs = {}

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def text(self, query, **kwargs):
        type(self).last_search_kwargs = kwargs
        return [
            {"title": f"{query} result", "body": "Delhi", "href": "https://linkedin.com/in/a"},
            {"title": "Second", "body": "Mumbai", "href": "https://example.com"},
        ][: kwargs["max_results"]]


class _BlockedDDGS(_FakeDDGS):
    def text(self, *_args, **_kwargs):
        raise RuntimeError("429 Too Many Requests captcha challenge")


class _EmptyDDGS(_FakeDDGS):
    def text(self, *_args, **_kwargs):
        raise RuntimeError("No results found.")


class _BrokenDDGS(_FakeDDGS):
    def text(self, *_args, **_kwargs):
        raise ValueError("unexpected parser failure")


class ProviderAdapterTests(unittest.TestCase):
    def setUp(self):
        self.limiter = _Limiter()
        self.retry = RetryExecutor(
            RetryConfig(max_attempts=2, base_delay_seconds=0, max_delay_seconds=0),
            sleeper=lambda _delay: None,
        )
        self.proxies = ProxyPool(ProxyConfig())
        self.request = SearchRequest(
            query="CMO Delhi", page=1, max_results=2, linkedin_only=True, job_id="job",
        )

    def test_ddgs_brave_google_and_local_normalize_pages(self):
        with patch.dict(sys.modules, {"ddgs": SimpleNamespace(DDGS=_FakeDDGS)}):
            page = DDGSSearchProvider(self.limiter, self.retry, self.proxies).search(self.request)
        self.assertEqual(len(page.results), 2)
        self.assertEqual(page.next_page, 2)
        self.assertEqual(page.retry_information["attempts"], 1)
        self.assertEqual(_FakeDDGS.last_search_kwargs["backend"], "yahoo")

        response = requests.Response()
        response.status_code = 200
        response._content = (
            b'<article><a href="https://linkedin.com/in/jane">Jane Doe</a><p>CMO</p></article>'
            b'<a href="https://example.com/not-linkedin">Other</a>'
            b'<a href="/relative">Relative</a>'
        )
        with patch("speedy_scraper.providers.requests.get", return_value=response):
            page = BraveSearchProvider(self.limiter, self.retry, self.proxies).search(self.request)
        self.assertEqual([item.title for item in page.results], ["Jane Doe"])
        self.assertIsNone(page.next_page)

        browser_state = {}

        def google_search(*_args, **_kwargs):
            browser_state["last_search_next_page"] = 4
            return [{"title": "Jane", "body": "CMO", "href": "https://linkedin.com/in/jane"}]

        proxy_pool = ProxyPool(ProxyConfig(enabled=True, urls=("http://user:secret@proxy:8080",)))
        with patch("speedy_scraper.providers.google_text_search", side_effect=google_search):
            page = GoogleSearchProvider(
                self.limiter, self.retry, proxy_pool, browser_state,
            ).search(self.request)
        self.assertEqual(page.next_page, 4)
        self.assertNotIn("proxy", browser_state)
        self.assertNotIn("secret", page.proxy_id)

        with tempfile.TemporaryDirectory() as directory:
            seed = Path(directory) / "seed.csv"
            seed.write_text(
                "Full_Name,Designation,Company,Location,LinkedIn_URL\n"
                "Jane Doe,CMO,Acme,Delhi,https://linkedin.com/in/jane\n",
                encoding="utf-8",
            )
            local = LocalSeedSearchProvider(seed, self.limiter, self.retry, self.proxies)
            page = local.search(self.request)
            self.assertEqual(page.results[0].title, "Jane Doe - CMO at Acme | LinkedIn")
            missing = LocalSeedSearchProvider(
                Path(directory) / "missing.csv", self.limiter, self.retry, self.proxies,
            ).search(self.request)
            self.assertEqual(missing.results, ())

    def test_provider_error_classification_and_shared_http_retry(self):
        class DDGSException(RuntimeError):
            pass

        self.assertTrue(default_retryable(DDGSException("ConnectError: connection refused")))
        self.assertTrue(default_retryable(ProviderBlockedError("blocked", provider="ddgs")))

        one_try = RetryExecutor(RetryConfig(max_attempts=1), sleeper=lambda _delay: None)
        with patch.dict(sys.modules, {"ddgs": SimpleNamespace(DDGS=_BlockedDDGS)}):
            with self.assertRaises(ProviderBlockedError):
                DDGSSearchProvider(self.limiter, one_try, self.proxies).search(self.request)
        with patch.dict(sys.modules, {"ddgs": SimpleNamespace(DDGS=_EmptyDDGS)}):
            empty = DDGSSearchProvider(self.limiter, one_try, self.proxies).search(self.request)
        self.assertEqual(empty.results, ())
        with patch.dict(sys.modules, {"ddgs": SimpleNamespace(DDGS=_BrokenDDGS)}):
            with self.assertRaisesRegex(ValueError, "unexpected parser failure"):
                DDGSSearchProvider(self.limiter, one_try, self.proxies).search(self.request)

        limited = requests.Response()
        limited.status_code = 429
        limited.headers["Retry-After"] = "0"
        with patch("speedy_scraper.providers.requests.get", return_value=limited):
            with self.assertRaises(RateLimitError):
                BraveSearchProvider(
                    self.limiter,
                    RetryExecutor(RetryConfig(max_attempts=1), sleeper=lambda _delay: None),
                    self.proxies,
                ).search(self.request)

        server_error = requests.Response()
        server_error.status_code = 503
        with patch("speedy_scraper.providers.requests.get", return_value=server_error):
            with self.assertRaises(requests.ConnectionError):
                BraveSearchProvider(
                    self.limiter,
                    RetryExecutor(RetryConfig(max_attempts=1), sleeper=lambda _delay: None),
                    self.proxies,
                ).search(self.request)

        ok = requests.Response()
        ok.status_code = 200
        ok._content = b"ok"
        client = ReliableHttpClient(self.limiter, self.retry, self.proxies)
        client.session.get = Mock(side_effect=[limited, ok])
        self.assertIs(client.get("https://example.com", job_id="job", timeout=1), ok)
        self.assertEqual(client.session.get.call_count, 2)

    def test_legacy_client_registry_and_google_non_retryable_error(self):
        provider = Mock()
        provider.search.return_value = SimpleNamespace(results=())
        with LegacySearchClient(provider, "job") as client:
            self.assertEqual(client.text("query"), [])
        self.assertTrue(provider.search.called)

        registry = ProviderRegistry(load_config(env={}), limiter=self.limiter, retries=self.retry, proxies=self.proxies)
        self.assertEqual(registry.get("DDGS").name, "ddgs")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            registry.get("unknown")

        state = {"proxy": "will-be-replaced"}
        google = GoogleSearchProvider(self.limiter, self.retry, self.proxies, state)
        with patch(
            "speedy_scraper.providers.google_text_search",
            side_effect=RuntimeError("Chrome is not installed configuration"),
        ):
            with self.assertRaisesRegex(RuntimeError, "not installed"):
                google.search(self.request)
        self.assertNotIn("proxy", state)


if __name__ == "__main__":
    unittest.main()
