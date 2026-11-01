"""Structured JSON-lines logging with secret redaction.

Every record is one JSON object per line. Callers pass structured fields with
``extra={"event": "...", ...}`` (or use :func:`log_event`). A redaction filter
removes registered secrets (the API token) and any ``Bearer`` credential from
both messages and fields before anything reaches a handler.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

ROOT_LOGGER = "colab_idle_guard"
REDACTED = "[REDACTED]"
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
# Attributes present on every LogRecord; anything else came from ``extra``.
_STANDARD_ATTRS = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime"}


class SecretRedactor(logging.Filter):
    def __init__(self) -> None:
        super().__init__()
        self._secrets: set[str] = set()
        self._lock = threading.Lock()

    def add_secret(self, secret: str) -> None:
        if secret and len(secret) >= 8:
            with self._lock:
                self._secrets.add(secret)

    def redact(self, value: Any) -> Any:
        if isinstance(value, str):
            value = _BEARER_RE.sub("Bearer " + REDACTED, value)
            with self._lock:
                for secret in self._secrets:
                    if secret in value:
                        value = value.replace(secret, REDACTED)
            return value
        if isinstance(value, dict):
            return {k: (REDACTED if str(k).lower() in {"token", "authorization"} else self.redact(v)) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(v) for v in value]
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = None
        for key, value in list(record.__dict__.items()):
            if key not in _STANDARD_ATTRS:
                record.__dict__[key] = REDACTED if key.lower() in {"token", "authorization"} else self.redact(value)
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


redactor = SecretRedactor()


def setup_logging(log_file: Path | None, level: str = "INFO", console: bool = False) -> logging.Logger:
    """Configure the application logger. Safe to call more than once."""
    logger = logging.getLogger(ROOT_LOGGER)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    formatter = JsonFormatter()
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redactor)
        logger.addHandler(file_handler)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        stream.addFilter(redactor)
        logger.addHandler(stream)
    return logger


def set_level(level: str) -> None:
    logging.getLogger(ROOT_LOGGER).setLevel(level)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, event, extra={"event": event, **fields})
