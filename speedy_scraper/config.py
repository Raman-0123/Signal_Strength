from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from speedy_scraper.models import DEFAULT_SOURCE_NAMES, ScrapeConfig
from speedy_scraper.taxonomy import contextualize_roles

CATALOG_PATH = Path(__file__).resolve().parents[1] / "config" / "catalog.yaml"


def load_catalog(path: Path | str = CATALOG_PATH) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ValueError("catalog.yaml must contain a mapping")
    return data


def config_from_mapping(value: dict[str, Any], *, catalog: dict[str, Any] | None = None) -> ScrapeConfig:
    catalog = catalog or load_catalog()
    preset_name = str(value.get("preset") or catalog.get("default_preset") or "").strip()
    preset = dict((catalog.get("presets") or {}).get(preset_name, {}))
    source_defaults = dict(catalog.get("source_defaults") or {})
    merged = {**source_defaults, **preset, **value}
    minimum_confidence_value = merged.get("minimum_confidence")
    return ScrapeConfig(
        roles=contextualize_roles(_list(merged.get("roles"))),
        locations=_list(merged.get("locations")),
        industries=_list(merged.get("industries")),
        company_names=_list(merged.get("company_names")),
        business_model=str(merged.get("business_model") or "Any"),
        target_count=max(1, int(merged.get("target_count") or 150)),
        sources=_list(merged.get("sources")) or list(DEFAULT_SOURCE_NAMES),
        max_queries=max(1, int(merged.get("max_queries") or 40)),
        max_results_per_query=max(1, int(merged.get("max_results_per_query") or 20)),
        max_pages_per_query=min(10, max(1, int(merged.get("max_pages_per_query") or 2))),
        candidate_pool_multiplier=max(1, int(merged.get("candidate_pool_multiplier") or 4)),
        browser_headless=bool(merged.get("browser_headless", True)),
        google_manual_challenge_seconds=min(
            300,
            max(0, int(merged.get("google_manual_challenge_seconds") or 0)),
        ),
        require_target_company=bool(merged.get("require_target_company", False)),
        minimum_confidence=min(
            99,
            max(0, int(85 if minimum_confidence_value is None else minimum_confidence_value)),
        ),
        minimum_sources=max(1, int(merged.get("minimum_sources") or 1)),
        source_failure_limit=max(1, int(merged.get("source_failure_limit") or 3)),
        include_terms=_list(merged.get("include_terms")),
        exclude_terms=_list(merged.get("exclude_terms")),
        query_mode=str(merged.get("query_mode") or "Balanced"),
        existing_files=[Path(item) for item in _list(merged.get("existing_files"))],
    )


def load_job_config(path: Path | str) -> ScrapeConfig:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError("job config must contain a mapping")
    return config_from_mapping(raw)


def preset_config(name: str | None = None) -> ScrapeConfig:
    catalog = load_catalog()
    preset = name or str(catalog.get("default_preset") or "")
    return config_from_mapping({"preset": preset}, catalog=catalog)


def _list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item).strip() for item in value if str(item).strip()]
