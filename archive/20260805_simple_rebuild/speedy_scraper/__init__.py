"""Production interfaces for Speedy-Scraper."""

from speedy_scraper.config import load_config
from speedy_scraper.domain import (
    BrowserMode,
    CandidateEvaluation,
    DedupKey,
    DedupScope,
    JobRecord,
    JobStatus,
    LeadRecord,
    LeadScrapeRequest,
    QueryPlanItem,
    ScheduleRecord,
    ScrapeCheckpoint,
    ScraperConfig,
    ScrapeResult,
)

__all__ = [
    "BrowserMode", "CandidateEvaluation", "DedupKey", "DedupScope",
    "JobRecord", "JobStatus", "LeadRecord", "LeadScrapeRequest",
    "QueryPlanItem", "ScheduleRecord", "ScrapeCheckpoint", "ScrapeResult",
    "ScraperConfig", "load_config",
]
