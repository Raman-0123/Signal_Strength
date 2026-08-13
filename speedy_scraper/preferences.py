from __future__ import annotations

from pathlib import Path
from typing import Iterable

from speedy_scraper.background_jobs import read_json, write_json

POC_RESULT_COLUMNS = (
    "Name",
    "Designation",
    "Company",
    "LinkedIn ID",
    "LinkedIn URL",
    "Source",
    "Confidence",
    "Match Evidence",
    "Requested Company",
    "Requested Designation",
)
DEFAULT_POC_RESULT_COLUMNS = (
    "Name",
    "Designation",
    "Company",
    "LinkedIn URL",
)
POC_PREFERENCES_PATH = Path(__file__).resolve().parent.parent / "config" / "ui_preferences.json"


def normalize_poc_result_columns(columns: Iterable[object]) -> list[str]:
    """Return supported POC columns in the table's canonical order."""
    selected = {str(column) for column in columns}
    return [column for column in POC_RESULT_COLUMNS if column in selected]


def load_poc_result_columns(path: Path | str = POC_PREFERENCES_PATH) -> list[str]:
    """Load the saved POC table view, falling back to the product default."""
    settings = read_json(path, default={})
    saved = settings.get("poc_result_columns") if isinstance(settings, dict) else None
    columns = normalize_poc_result_columns(saved or [])
    return columns or list(DEFAULT_POC_RESULT_COLUMNS)


def save_poc_result_columns(
    columns: Iterable[object], path: Path | str = POC_PREFERENCES_PATH
) -> list[str]:
    """Persist the user's POC table view and return the normalized selection."""
    normalized = normalize_poc_result_columns(columns)
    if not normalized:
        raise ValueError("At least one POC result column must remain visible.")
    settings = read_json(path, default={})
    if not isinstance(settings, dict):
        settings = {}
    settings["poc_result_columns"] = normalized
    write_json(path, settings)
    return normalized
