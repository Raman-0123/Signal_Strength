from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_SOURCE_NAMES = ["google_browser"]


@dataclass(frozen=True)
class SearchResult:
    title: str
    body: str
    href: str
    source: str
    query: str


@dataclass(frozen=True)
class SearchPage:
    results: list[SearchResult]
    page: int
    has_next: bool


@dataclass
class RawCandidate:
    name: str
    designation: str
    company: str
    linkedin_url: str
    title: str
    body: str
    source: str
    query: str
    evidence: str
    sources_seen: set[str] = field(default_factory=set)
    queries_seen: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class VerifiedLead:
    name: str
    designation: str
    company: str
    location: str
    linkedin_id: str
    linkedin_url: str
    source: str
    confidence: int
    evidence: str


@dataclass(frozen=True)
class RejectedCandidate:
    name: str
    designation: str
    company: str
    linkedin_url: str
    reason: str
    source: str
    evidence: str


@dataclass
class ScrapeConfig:
    roles: list[str]
    locations: list[str]
    industries: list[str]
    company_names: list[str] = field(default_factory=list)
    business_model: str = "Any"
    target_count: int = 150
    sources: list[str] = field(default_factory=lambda: list(DEFAULT_SOURCE_NAMES))
    max_queries: int = 40
    max_results_per_query: int = 20
    max_pages_per_query: int = 2
    candidate_pool_multiplier: int = 4
    browser_headless: bool = True
    google_manual_challenge_seconds: int = 180
    require_target_company: bool = False
    minimum_confidence: int = 85
    minimum_sources: int = 1
    source_failure_limit: int = 3
    include_terms: list[str] = field(default_factory=list)
    exclude_terms: list[str] = field(default_factory=list)
    query_mode: str = "Balanced"
    existing_files: list[Path] = field(default_factory=list)


@dataclass
class ScrapeResult:
    leads: list[VerifiedLead]
    rejections: list[RejectedCandidate]
    metrics: dict[str, int]
    queries: list[str]
    source_errors: list[str] = field(default_factory=list)
