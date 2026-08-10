"""Typed domain models shared by every Speedy-Scraper interface."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Protocol


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_VERIFICATION = "waiting_verification"
    PAUSE_REQUESTED = "pause_requested"
    PAUSED = "paused"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    EXHAUSTED = "exhausted"
    FAILED = "failed"

    @property
    def terminal(self) -> bool:
        return self in {
            self.CANCELLED, self.COMPLETED, self.EXHAUSTED, self.FAILED,
        }


class ResultKind(StrEnum):
    QUALIFIED = "qualified"
    STRICT = "strict"


class DedupScope(StrEnum):
    GLOBAL = "global"
    JOB = "job"
    IMPORT = "import"


class BrowserMode(StrEnum):
    INTERACTIVE = "interactive"
    SCHEDULED_HEADLESS = "scheduled_headless"


@dataclass(frozen=True, slots=True)
class DedupKey:
    kind: str
    value: str


@dataclass(frozen=True, slots=True)
class RetryConfig:
    max_attempts: int = 3
    base_delay_seconds: float = 1.0
    multiplier: float = 2.0
    max_delay_seconds: float = 30.0
    jitter_ratio: float = 1.0


@dataclass(frozen=True, slots=True)
class RateLimitConfig:
    requests_per_minute: int = 30
    minimum_interval_seconds: float = 0.5


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    enabled: bool = False
    urls: tuple[str, ...] = ()
    strategy: str = "sticky_failure_only"
    failure_threshold: int = 2
    cooldown_seconds: float = 300.0


@dataclass(frozen=True, slots=True)
class BrowserConfig:
    chrome_path: str = ""
    profile_root: str = "data/browser_profiles"
    interactive_headed: bool = True
    scheduled_headless: bool = True
    captcha_poll_seconds: float = 2.0
    navigation_timeout_seconds: float = 60.0
    page_settle_min_seconds: float = 1.2
    page_settle_max_seconds: float = 2.2
    post_search_min_seconds: float = 0.7
    post_search_max_seconds: float = 1.2


@dataclass(frozen=True, slots=True)
class StorageConfig:
    database_path: str = "data/speedy_scraper.db"
    busy_timeout_ms: int = 10_000
    retention_days: int = 90


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    timezone: str = "Asia/Kolkata"
    max_workers: int = 2
    misfire_grace_seconds: int = 900
    cleanup_interval_seconds: int = 3600


@dataclass(frozen=True, slots=True)
class ScoringConfig:
    role: int = 12
    location: int = 10
    industry: int = 5
    custom: int = 8
    company_known: int = 2
    business_model: int = 6
    minimum: int = 50
    maximum: int = 99


@dataclass(frozen=True, slots=True)
class QueryBudgetConfig:
    acceptance_rate: float = 0.15
    citations_per_query: int = 30
    headroom: float = 1.4
    maximum_queries: int = 180


@dataclass(frozen=True, slots=True)
class ScraperConfig:
    data_dir: str = "data"
    log_level: str = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    api_key: str = ""
    retry: RetryConfig = field(default_factory=RetryConfig)
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    scoring: ScoringConfig = field(default_factory=ScoringConfig)
    query_budget: QueryBudgetConfig = field(default_factory=QueryBudgetConfig)
    proxy: ProxyConfig = field(default_factory=ProxyConfig)
    rate_limits: dict[str, RateLimitConfig] = field(default_factory=lambda: {
        "google": RateLimitConfig(20, 1.5),
        "ddgs": RateLimitConfig(20, 2.0),
        "brave": RateLimitConfig(30, 1.0),
        "event_page": RateLimitConfig(30, 0.5),
        "public_contact": RateLimitConfig(30, 0.5),
    })


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    body: str
    href: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SearchRequest:
    query: str
    page: int = 1
    max_results: int = 10
    linkedin_only: bool = True
    job_id: str = ""


@dataclass(frozen=True, slots=True)
class SearchPage:
    results: tuple[SearchResult, ...]
    provider: str
    page: int
    next_page: int | None = None
    proxy_id: str = ""
    attempts: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    retry_information: dict[str, Any] = field(default_factory=dict)


class SearchProvider(Protocol):
    name: str

    def search(self, request: SearchRequest) -> SearchPage: ...


@dataclass(frozen=True, slots=True)
class QueryPlanItem:
    query: str
    bucket: str = "discovery"
    fallback_query: str = ""
    roles: tuple[str, ...] = ()
    locations: tuple[str, ...] = ()
    strategy_version: int = 0

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "QueryPlanItem":
        return cls(
            query=str(value.get("query", "")),
            bucket=str(value.get("bucket", "discovery")),
            fallback_query=str(value.get("fallback_query", "")),
            roles=tuple(value.get("roles", ())),
            locations=tuple(value.get("locations", ())),
            strategy_version=int(value.get("strategy_version", 0)),
        )

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["roles"] = list(self.roles)
        value["locations"] = list(self.locations)
        return value


@dataclass(frozen=True, slots=True)
class LeadScrapeRequest:
    location_ids: tuple[str, ...] = ()
    role_ids: tuple[str, ...] = ()
    industry_ids: tuple[str, ...] = ()
    signal_ids: tuple[str, ...] = ()
    custom_keywords: tuple[str, ...] = ()
    organizations: tuple[str, ...] = ()
    business_model: str = "Any"
    gcc_only: bool = False
    target_count: int = 15
    discovery_provider: str = "ddgs"
    validation_provider: str = "ddgs"
    edited_queries: tuple[str, ...] = ()
    dedup_import_ids: tuple[str, ...] = ()
    browser_mode: BrowserMode = BrowserMode.INTERACTIVE

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["browser_mode"] = self.browser_mode.value
        return value


@dataclass(slots=True)
class CandidateEvaluation:
    hard_qualified: bool
    strict_qualified: bool
    hits: dict[str, bool] = field(default_factory=dict)
    matches: dict[str, list[str]] = field(default_factory=dict)
    signal: dict[str, Any] = field(default_factory=dict)
    business: dict[str, Any] = field(default_factory=dict)
    gcc: dict[str, Any] = field(default_factory=dict)
    score: int = 50
    evidence_text: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LeadRecord:
    name: str
    designation: str
    company: str
    linkedin_url: str
    verified_location: str
    score: int = 50
    query_bucket: str = "discovery"
    source: str = ""
    hard_qualified: bool = True
    strict_qualified: bool = False
    evaluation: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)

    def as_export_row(self) -> dict[str, str]:
        return {
            "Name": self.name,
            "Designation": self.designation,
            "Company": self.company,
            "Location": self.verified_location,
        }

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def as_legacy_dict(self) -> dict[str, Any]:
        detail = dict(self.evaluation.get("legacy", {}))
        detail.update({
            "Full_Name": self.name,
            "Designation": self.designation,
            "Company": self.company,
            "LinkedIn_URL": self.linkedin_url,
            "Location_Evidence": self.verified_location,
            "Lead_Score": self.score,
            "Query_Bucket": self.query_bucket,
        })
        return detail

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LeadRecord":
        return cls(**{key: value[key] for key in cls.__dataclass_fields__ if key in value})


@dataclass(slots=True)
class ScrapeCheckpoint:
    query_index: int = 0
    query_state: dict[str, Any] = field(default_factory=dict)
    partial_profiles: dict[str, dict[str, str]] = field(default_factory=dict)
    browser_state: dict[str, Any] = field(default_factory=dict)
    pending_validation: dict[str, Any] | None = None
    security_check: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "ScrapeCheckpoint":
        return cls(**(value or {}))


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    workflow: str
    status: JobStatus
    request: dict[str, Any]
    checkpoint: dict[str, Any]
    qualified_count: int = 0
    strict_count: int = 0
    outcome: str = ""
    error_code: str = ""
    error_message: str = ""
    created_at: str = ""
    updated_at: str = ""
    started_at: str = ""
    completed_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        return value


@dataclass(frozen=True, slots=True)
class ScheduleRecord:
    id: str
    name: str
    workflow: str
    trigger: dict[str, Any]
    request: dict[str, Any]
    timezone: str
    enabled: bool = True
    last_job_id: str = ""
    next_run_at: str = ""
    created_at: str = ""
    updated_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ScrapeProgress:
    job: JobRecord
    processed_candidates: int = 0
    message: str = ""


@dataclass(frozen=True, slots=True)
class ScrapeResult:
    job: JobRecord
    qualified: tuple[LeadRecord, ...] = ()
    strict: tuple[LeadRecord, ...] = ()
    metrics: dict[str, float] = field(default_factory=dict)
