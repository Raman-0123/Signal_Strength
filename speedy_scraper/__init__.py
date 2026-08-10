"""Simple public-search lead scraper."""

from speedy_scraper.models import ScrapeConfig, ScrapeResult, VerifiedLead
from speedy_scraper.pipeline import LeadScraper

__all__ = ["LeadScraper", "ScrapeConfig", "ScrapeResult", "VerifiedLead"]

