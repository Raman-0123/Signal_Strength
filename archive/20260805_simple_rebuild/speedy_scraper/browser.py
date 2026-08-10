"""Persistent Google browser state owned by a durable scrape job."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from core.google_search import (
    close_google_browser,
    google_browser_running,
    google_security_check_resolved,
)


class BrowserManager:
    """Prepare, observe, and close the one browser profile assigned to a job.

    The manager deliberately has no challenge-solving behavior.  It only
    persists browser metadata and observes whether a user-completed challenge
    returned to the exact saved search request.
    """

    def __init__(self, config):
        self.config = config

    def prepare(self, job_id: str, state: dict[str, Any], *, scheduled: bool) -> dict[str, Any]:
        profile_root = Path(self.config.profile_root)
        state.setdefault(
            "profile_dir",
            str(profile_root / f"speedy-scraper-google-{job_id}"),
        )
        state.setdefault("headless", bool(scheduled and self.config.scheduled_headless))
        if self.config.chrome_path:
            state.setdefault("chrome_path", self.config.chrome_path)
        state.setdefault("navigation_timeout_seconds", self.config.navigation_timeout_seconds)
        state.setdefault("page_settle_min_seconds", self.config.page_settle_min_seconds)
        state.setdefault("page_settle_max_seconds", self.config.page_settle_max_seconds)
        state.setdefault("post_search_min_seconds", self.config.post_search_min_seconds)
        state.setdefault("post_search_max_seconds", self.config.post_search_max_seconds)
        return state

    @staticmethod
    def display_available() -> bool:
        if sys.platform in {"darwin", "win32"}:
            return True
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))

    @staticmethod
    def verification_resolved(state: dict[str, Any], check: dict[str, Any] | None) -> bool:
        return google_security_check_resolved(state, check)

    @staticmethod
    def is_running(state: dict[str, Any]) -> bool:
        return google_browser_running(state)

    @staticmethod
    def close(state: dict[str, Any] | None) -> None:
        close_google_browser(state)
