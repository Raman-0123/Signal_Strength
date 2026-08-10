"""Structured events and JSON logging without UI dependencies."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Protocol

from speedy_scraper.domain import utc_now

_CREDENTIAL_RE = re.compile(r"(?P<scheme>https?|socks5h?|socks4)://[^@\s]+@", re.IGNORECASE)


def redact(value: Any) -> Any:
    if isinstance(value, str):
        return _CREDENTIAL_RE.sub(r"\g<scheme>://***@", value)
    if isinstance(value, dict):
        return {
            key: "***" if any(part in key.lower() for part in ("password", "api_key", "token", "secret")) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    return value


class EventSink(Protocol):
    def emit(
        self,
        level: str,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> None: ...


class NullEventSink:
    def emit(self, level: str, event_type: str, message: str, payload=None) -> None:
        return None


@dataclass(slots=True)
class RepositoryEventSink:
    repository: Any
    job_id: str
    logger: logging.Logger | None = None

    def emit(self, level: str, event_type: str, message: str, payload=None) -> None:
        safe_payload = redact(payload or {})
        safe_message = str(redact(message))
        self.repository.add_event(self.job_id, level, event_type, safe_message, safe_payload)
        if event_type == "provider_retry":
            self.repository.increment_metric(self.job_id, "provider_retries")
        elif level.lower() == "error":
            self.repository.increment_metric(self.job_id, "errors")
        if self.logger:
            log = getattr(self.logger, level.lower(), self.logger.info)
            log(safe_message, extra={"job_id": self.job_id, "event_type": event_type, "payload": safe_payload})

    def write(self, message: str) -> None:
        self.emit("info", "status", message)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": utc_now(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        for key in ("job_id", "workflow", "provider", "event_type", "payload"):
            if hasattr(record, key):
                payload[key] = redact(getattr(record, key))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def configure_logging(data_dir: str | Path, level: str = "INFO") -> logging.Logger:
    logger = logging.getLogger("speedy_scraper")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    if logger.handlers:
        return logger
    formatter = JsonFormatter()
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    log_dir = Path(data_dir).expanduser().resolve() / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        log_dir / "speedy-scraper.jsonl", maxBytes=10 * 1024 * 1024, backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger
