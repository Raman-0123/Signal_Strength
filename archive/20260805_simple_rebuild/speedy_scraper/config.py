"""YAML and environment based configuration loading."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from speedy_scraper.domain import (
    BrowserConfig,
    ProxyConfig,
    QueryBudgetConfig,
    RateLimitConfig,
    RetryConfig,
    SchedulerConfig,
    ScoringConfig,
    ScraperConfig,
    StorageConfig,
)

ROOT = Path(__file__).resolve().parents[1]


def _installed_resource(folder: str, name: str) -> Path:
    source_path = ROOT / folder / name
    if source_path.exists():
        return source_path
    return Path(sys.prefix) / folder / name


DEFAULT_CONFIG_PATH = _installed_resource("config", "default.yaml")
DEFAULT_CATALOG_PATH = _installed_resource("config", "catalog.yaml")


@dataclass(frozen=True, slots=True)
class Catalog:
    version: int
    locations: dict[str, list[str]]
    roles: dict[str, list[str]]
    industries: dict[str, list[str]]
    signals: dict[str, list[str]]
    role_labels: dict[str, str]
    industry_labels: dict[str, str]
    query_templates: dict[str, str]

    def resolve(self, category: str, identifiers: list[str] | tuple[str, ...]) -> list[str]:
        source = getattr(self, category)
        resolved: list[str] = []
        for raw in identifiers:
            key = str(raw).split(" - ", 1)[0]
            values = source.get(key)
            if values is None:
                values = [str(raw)]
            for value in values:
                if value not in resolved:
                    resolved.append(value)
        return resolved


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise ValueError(f"Configuration file not found: {path}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Configuration root must be a mapping: {path}")
    return value


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _deep_merge(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


def _parse_bool(value: str) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Expected a boolean value, received {value!r}")


def _environment_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    direct: dict[str, tuple[str, Callable[[str], Any]]] = {
        "SPEEDY_SCRAPER_DATA_DIR": ("data_dir", str),
        "SPEEDY_SCRAPER_LOG_LEVEL": ("log_level", str),
        "SPEEDY_SCRAPER_API_HOST": ("api_host", str),
        "SPEEDY_SCRAPER_API_PORT": ("api_port", int),
        "SPEEDY_SCRAPER_API_KEY": ("api_key", str),
    }
    nested: dict[str, tuple[str, str, Callable[[str], Any]]] = {
        "SPEEDY_SCRAPER_DB_PATH": ("storage", "database_path", str),
        "SPEEDY_SCRAPER_CHROME_PATH": ("browser", "chrome_path", str),
        "SPEEDY_SCRAPER_RETENTION_DAYS": ("storage", "retention_days", int),
        "SPEEDY_SCRAPER_SCHEDULER_TIMEZONE": ("scheduler", "timezone", str),
        "SPEEDY_SCRAPER_SCHEDULED_HEADLESS": ("browser", "scheduled_headless", _parse_bool),
    }
    result: dict[str, Any] = {}
    for name, (key, converter) in direct.items():
        if name in env:
            result[key] = converter(env[name])
    for name, (section, key, converter) in nested.items():
        if name in env:
            result.setdefault(section, {})[key] = converter(env[name])
    if raw_urls := env.get("SPEEDY_SCRAPER_PROXY_URLS", "").strip():
        result["proxy"] = {
            "enabled": True,
            "urls": [item.strip() for item in raw_urls.split(",") if item.strip()],
        }
    # Generic nested overrides use double underscores, for example
    # SPEEDY_SCRAPER_RETRY__MAX_ATTEMPTS=5.
    reserved = {*direct, *nested, "SPEEDY_SCRAPER_PROXY_URLS"}
    prefix = "SPEEDY_SCRAPER_"
    for name, raw in env.items():
        if name in reserved or not name.startswith(prefix) or "__" not in name:
            continue
        keys = [part.lower() for part in name[len(prefix):].split("__") if part]
        if not keys:
            continue
        parsed = yaml.safe_load(raw)
        cursor = result
        for key in keys[:-1]:
            cursor = cursor.setdefault(key, {})
        cursor[keys[-1]] = parsed
    return result


def _validate_config(value: dict[str, Any]) -> None:
    retry = value.get("retry", {})
    if not 1 <= int(retry.get("max_attempts", 3)) <= 10:
        raise ValueError("retry.max_attempts must be between 1 and 10")
    storage = value.get("storage", {})
    if int(storage.get("retention_days", 90)) < 1:
        raise ValueError("storage.retention_days must be positive")
    query_budget = value.get("query_budget", {})
    if not 0 < float(query_budget.get("acceptance_rate", 0.15)) <= 1:
        raise ValueError("query_budget.acceptance_rate must be in (0, 1]")
    if int(query_budget.get("citations_per_query", 30)) < 1:
        raise ValueError("query_budget.citations_per_query must be positive")
    if int(query_budget.get("maximum_queries", 180)) < 1:
        raise ValueError("query_budget.maximum_queries must be positive")
    scheduler = value.get("scheduler", {})
    if int(scheduler.get("max_workers", 2)) < 1:
        raise ValueError("scheduler.max_workers must be positive")
    for name, policy in value.get("rate_limits", {}).items():
        if int(policy.get("requests_per_minute", 30)) < 1:
            raise ValueError(f"rate_limits.{name}.requests_per_minute must be positive")
        if float(policy.get("minimum_interval_seconds", 0.5)) < 0:
            raise ValueError(f"rate_limits.{name}.minimum_interval_seconds cannot be negative")
    browser = value.get("browser", {})
    for field_name in (
        "captcha_poll_seconds", "navigation_timeout_seconds",
        "page_settle_min_seconds", "page_settle_max_seconds",
        "post_search_min_seconds", "post_search_max_seconds",
    ):
        if float(browser.get(field_name, 0)) < 0:
            raise ValueError(f"browser.{field_name} cannot be negative")
    if float(browser.get("page_settle_max_seconds", 2.2)) < float(
        browser.get("page_settle_min_seconds", 1.2)
    ):
        raise ValueError("browser.page_settle_max_seconds must be >= page_settle_min_seconds")
    if float(browser.get("post_search_max_seconds", 1.2)) < float(
        browser.get("post_search_min_seconds", 0.7)
    ):
        raise ValueError("browser.post_search_max_seconds must be >= post_search_min_seconds")
    proxy = value.get("proxy", {})
    if int(proxy.get("failure_threshold", 2)) < 1:
        raise ValueError("proxy.failure_threshold must be positive")
    for url in proxy.get("urls", ()):
        if not str(url).lower().startswith(("http://", "https://", "socks4://", "socks5://", "socks5h://")):
            raise ValueError(f"Unsupported proxy URL: {url!r}")


def load_config(
    path: str | Path | None = None,
    *,
    overrides: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
) -> ScraperConfig:
    value = asdict(ScraperConfig())
    committed = _read_yaml(DEFAULT_CONFIG_PATH) if DEFAULT_CONFIG_PATH.exists() else {}
    if committed.get("api_key") or committed.get("proxy", {}).get("urls"):
        raise ValueError("API keys and proxy URLs must not be stored in committed YAML.")
    value = _deep_merge(value, committed)
    if path:
        user_value = _read_yaml(Path(path).expanduser().resolve())
        if user_value.get("api_key") or user_value.get("proxy", {}).get("urls"):
            raise ValueError("API keys and proxy URLs must be supplied through environment variables.")
        value = _deep_merge(value, user_value)
    value = _deep_merge(value, _environment_overrides(os.environ if env is None else env))
    if overrides:
        value = _deep_merge(value, overrides)
    _validate_config(value)
    rate_limits = {
        key: RateLimitConfig(**settings)
        for key, settings in value.get("rate_limits", {}).items()
    }
    return ScraperConfig(
        data_dir=str(value.get("data_dir", "data")),
        log_level=str(value.get("log_level", "INFO")).upper(),
        api_host=str(value.get("api_host", "127.0.0.1")),
        api_port=int(value.get("api_port", 8000)),
        api_key=str(value.get("api_key", "")),
        retry=RetryConfig(**value.get("retry", {})),
        browser=BrowserConfig(**value.get("browser", {})),
        storage=StorageConfig(**value.get("storage", {})),
        scheduler=SchedulerConfig(**value.get("scheduler", {})),
        scoring=ScoringConfig(**value.get("scoring", {})),
        query_budget=QueryBudgetConfig(**value.get("query_budget", {})),
        proxy=ProxyConfig(
            **{
                **value.get("proxy", {}),
                "urls": tuple(value.get("proxy", {}).get("urls", ())),
            }
        ),
        rate_limits=rate_limits,
    )


def load_catalog(path: str | Path | None = None) -> Catalog:
    value = _read_yaml(Path(path).resolve() if path else DEFAULT_CATALOG_PATH)
    required = ("locations", "roles", "industries", "signals")
    for key in required:
        if not isinstance(value.get(key), dict) or not value[key]:
            raise ValueError(f"Catalog category {key!r} must be a non-empty mapping")
    return Catalog(
        version=int(value.get("version", 1)),
        locations={str(k): list(v) for k, v in value["locations"].items()},
        roles={str(k): list(v) for k, v in value["roles"].items()},
        industries={str(k): list(v) for k, v in value["industries"].items()},
        signals={str(k): list(v) for k, v in value["signals"].items()},
        role_labels={str(k): str(v) for k, v in value.get("role_labels", {}).items()},
        industry_labels={str(k): str(v) for k, v in value.get("industry_labels", {}).items()},
        query_templates={str(k): str(v) for k, v in value.get("query_templates", {}).items()},
    )
