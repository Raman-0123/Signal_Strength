"""Rate limiting, retry, and sticky failure-only proxy rotation."""

from __future__ import annotations

import hashlib
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from threading import RLock
from typing import Callable, TypeVar
from urllib.parse import urlsplit, urlunsplit

import requests

from speedy_scraper.domain import ProxyConfig, RateLimitConfig, RetryConfig
from speedy_scraper.events import EventSink, NullEventSink

T = TypeVar("T")


class RetryableProviderError(RuntimeError):
    def __init__(self, message: str, *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class RateLimitError(RetryableProviderError):
    pass


class ProviderBlockedError(RetryableProviderError):
    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        retry_after: float | None = None,
    ):
        super().__init__(message, retry_after=retry_after)
        self.provider = provider


def parse_retry_after(value: str | None, *, now: datetime | None = None) -> float | None:
    """Parse both delta-seconds and HTTP-date Retry-After values."""
    if not value or not value.strip():
        return None
    cleaned = value.strip()
    try:
        return max(0.0, float(cleaned))
    except ValueError:
        pass
    try:
        deadline = parsedate_to_datetime(cleaned)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return max(0.0, (deadline.astimezone(timezone.utc) - current).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


class TokenBucketRateLimiter:
    def __init__(
        self,
        policies: dict[str, RateLimitConfig],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.policies = policies
        self.clock = clock
        self.sleeper = sleeper
        self._state: dict[str, dict[str, float]] = {}
        self._lock = RLock()

    def acquire(self, provider: str) -> None:
        policy = self.policies.get(provider, RateLimitConfig())
        capacity = max(1.0, float(policy.requests_per_minute))
        refill_rate = capacity / 60.0
        while True:
            with self._lock:
                now = self.clock()
                state = self._state.setdefault(provider, {
                    "tokens": capacity,
                    "updated": now,
                    "last_request": -1e9,
                })
                elapsed = max(0.0, now - state["updated"])
                state["tokens"] = min(capacity, state["tokens"] + elapsed * refill_rate)
                state["updated"] = now
                interval_wait = max(
                    0.0,
                    policy.minimum_interval_seconds - (now - state["last_request"]),
                )
                token_wait = 0.0 if state["tokens"] >= 1.0 else (1.0 - state["tokens"]) / refill_rate
                wait = max(interval_wait, token_wait)
                if wait <= 0:
                    state["tokens"] -= 1.0
                    state["last_request"] = now
                    return
            self.sleeper(wait)


@dataclass(slots=True)
class _ProxyState:
    failures: int = 0
    cooldown_until: float = 0.0


class ProxyPool:
    def __init__(
        self,
        config: ProxyConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.config = config
        self.clock = clock
        self._states = {url: _ProxyState() for url in config.urls}
        self._assignments: dict[str, str] = {}
        self._lock = RLock()

    @staticmethod
    def identifier(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:12] if url else "direct"

    @staticmethod
    def redacted(url: str) -> str:
        if not url:
            return "direct"
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, "", "", ""))

    def acquire(self, job_id: str) -> str:
        if not self.config.enabled or not self.config.urls:
            return ""
        with self._lock:
            assigned = self._assignments.get(job_id)
            if assigned and self._states[assigned].cooldown_until <= self.clock():
                return assigned
            available = [
                url for url in self.config.urls
                if self._states[url].cooldown_until <= self.clock()
            ]
            if not available:
                available = list(self.config.urls)
            seed = int(hashlib.sha256(job_id.encode()).hexdigest()[:8], 16)
            selected = available[seed % len(available)]
            self._assignments[job_id] = selected
            return selected

    def report_success(self, job_id: str) -> None:
        with self._lock:
            if proxy := self._assignments.get(job_id):
                self._states[proxy].failures = 0

    def report_failure(self, job_id: str, *, rotate: bool) -> None:
        with self._lock:
            proxy = self._assignments.get(job_id)
            if not proxy:
                return
            state = self._states[proxy]
            state.failures += 1
            if rotate and state.failures >= self.config.failure_threshold:
                state.cooldown_until = self.clock() + self.config.cooldown_seconds
                self._assignments.pop(job_id, None)


def default_retryable(exc: Exception) -> bool:
    if isinstance(exc, RetryableProviderError):
        return True
    if isinstance(exc, (requests.Timeout, requests.ConnectionError)):
        return True
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    markers = (
        "timeout", "timed out", "ratelimit", "rate limit", "temporar",
        "connection", "connecterror", "connection refused",
    )
    return any(part in name or part in message for part in markers)


class RetryExecutor:
    def __init__(
        self,
        config: RetryConfig,
        *,
        sleeper: Callable[[float], None] = time.sleep,
        randomizer: Callable[[], float] = random.random,
    ):
        self.config = config
        self.sleeper = sleeper
        self.randomizer = randomizer

    def run(
        self,
        operation: Callable[[], T],
        *,
        provider: str,
        event_sink: EventSink | None = None,
        retryable: Callable[[Exception], bool] = default_retryable,
        on_exhausted: Callable[[Exception], None] | None = None,
    ) -> tuple[T, int]:
        sink = event_sink or NullEventSink()
        last: Exception | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                return operation(), attempt
            except Exception as exc:
                last = exc
                is_retryable = retryable(exc)
                if not is_retryable or attempt >= self.config.max_attempts:
                    if on_exhausted and is_retryable:
                        on_exhausted(exc)
                    raise
                base = min(
                    self.config.max_delay_seconds,
                    self.config.base_delay_seconds * (self.config.multiplier ** (attempt - 1)),
                )
                delay = getattr(exc, "retry_after", None)
                if delay is None:
                    jitter_ratio = min(1.0, max(0.0, self.config.jitter_ratio))
                    delay = base * (
                        (1.0 - jitter_ratio) + jitter_ratio * self.randomizer()
                    )
                sink.emit(
                    "warning", "provider_retry",
                    f"{provider} request failed; retrying after backoff.",
                    {"provider": provider, "attempt": attempt, "delay_seconds": round(float(delay), 3), "error_type": type(exc).__name__},
                )
                self.sleeper(float(delay))
        assert last is not None
        raise last
