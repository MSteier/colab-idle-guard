"""Persistent, validated application configuration.

The schema below is the single source of truth for keys, types, defaults and
ranges; config/default.json must mirror it (enforced by a test). User values are
merged over defaults, invalid values fall back to their defaults with a logged
warning, unknown keys are preserved so that upgrades/downgrades never lose user
settings, and saves are atomic.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


@dataclass(frozen=True)
class Setting:
    type: type
    default: Any
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[Any, ...] | None = None

    def validate(self, value: Any) -> Any:
        """Return the normalised value or raise ValueError."""
        if self.type is bool:
            if not isinstance(value, bool):
                raise ValueError(f"expected true/false, got {value!r}")
            return value
        if self.type is int:
            # bool is an int subclass; reject it explicitly.
            if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
                raise ValueError(f"expected an integer, got {value!r}")
            value = int(value)
        elif self.type is str:
            if not isinstance(value, str):
                raise ValueError(f"expected text, got {value!r}")
        elif self.type is list:
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError(f"expected a list of strings, got {value!r}")
            return list(value)
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"must be >= {self.minimum}")
        if self.maximum is not None and value > self.maximum:
            raise ValueError(f"must be <= {self.maximum}")
        if self.choices is not None and value not in self.choices:
            raise ValueError(f"must be one of {', '.join(map(str, self.choices))}")
        return value


SCHEMA: dict[str, Setting] = {
    "idle_timeout_minutes": Setting(int, 30, 1, 24 * 60),
    "warning_countdown_seconds": Setting(int, 60, 10, 600),
    "monitor_chrome": Setting(bool, True),
    "monitor_vscode": Setting(bool, True),
    "protect_running_cells": Setting(bool, True),
    "require_verified_execution_state": Setting(bool, True),
    "automatic_disconnect_enabled": Setting(bool, False),
    "release_consent_acknowledged": Setting(bool, False),
    "start_with_windows": Setting(bool, False),
    "notifications_enabled": Setting(bool, True),
    "log_level": Setting(str, "INFO", choices=LOG_LEVELS),
    "api_port": Setting(int, 47821, 1024, 65535),
    "heartbeat_interval_seconds": Setting(int, 15, 5, 120),
    "integration_timeout_seconds": Setting(int, 60, 15, 600),
    "telemetry_fresh_seconds": Setting(int, 45, 10, 600),
    "stale_verification_max_minutes": Setting(int, 10, 1, 24 * 60),
    "idle_timer_starts_after_execution": Setting(bool, True),
    "require_foreground_corroboration": Setting(bool, True),
    "allowed_chrome_extension_ids": Setting(list, []),
}

# Safety-critical protections that the settings UI must not switch off silently.
# They exist as keys for transparency but are enforced regardless of value.
ALWAYS_ENFORCED = ("protect_running_cells", "require_verified_execution_state")


class ConfigError(ValueError):
    """Raised when an update contains invalid values."""


def defaults() -> dict[str, Any]:
    return {key: (list(s.default) if isinstance(s.default, list) else s.default) for key, s in SCHEMA.items()}


class ConfigManager:
    """Thread-safe holder of the current configuration with atomic persistence."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._values: dict[str, Any] = defaults()
        self._extra: dict[str, Any] = {}
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self.load_warnings: list[str] = []

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, Any]:
        """Load from disk, creating the file with defaults if it does not exist."""
        with self._lock:
            self.load_warnings = []
            raw: dict[str, Any] = {}
            if self._path.exists():
                try:
                    raw = json.loads(self._path.read_text(encoding="utf-8"))
                    if not isinstance(raw, dict):
                        raise ValueError("top level must be an object")
                except (OSError, ValueError) as exc:
                    # Keep the unreadable file for the user; run on defaults.
                    backup = self._path.with_suffix(".invalid.json")
                    try:
                        self._path.replace(backup)
                    except OSError:
                        pass
                    self.load_warnings.append(f"config file unreadable ({exc}); moved to {backup.name}, using defaults")
                    raw = {}
            values = defaults()
            extra: dict[str, Any] = {}
            for key, value in raw.items():
                setting = SCHEMA.get(key)
                if setting is None:
                    extra[key] = value
                    continue
                try:
                    values[key] = setting.validate(value)
                except ValueError as exc:
                    self.load_warnings.append(f"{key}: {exc}; using default {setting.default!r}")
            self._values, self._extra = values, extra
            for warning in self.load_warnings:
                log.warning("config_invalid_value", extra={"event": "config_warning", "detail": warning})
            if not self._path.exists():
                self._save_locked()
            return dict(self._values)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {k: (list(v) if isinstance(v, list) else v) for k, v in self._values.items()}

    def get(self, key: str) -> Any:
        with self._lock:
            return self._values[key]

    def __getitem__(self, key: str) -> Any:
        return self.get(key)

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        """Validate all changes first, then apply and persist them atomically."""
        with self._lock:
            errors: list[str] = []
            validated: dict[str, Any] = {}
            for key, value in changes.items():
                setting = SCHEMA.get(key)
                if setting is None:
                    errors.append(f"{key}: unknown setting")
                    continue
                try:
                    validated[key] = setting.validate(value)
                except ValueError as exc:
                    errors.append(f"{key}: {exc}")
            if errors:
                raise ConfigError("; ".join(errors))
            self._values.update(validated)
            # Enabling automatic release without consent is stored but not honoured
            # (see auto_release_allowed); revoking consent always disables it.
            if validated.get("release_consent_acknowledged") is False:
                self._values["automatic_disconnect_enabled"] = False
            self._save_locked()
            snapshot = self.snapshot()
        for listener in list(self._listeners):
            try:
                listener(snapshot)
            except Exception:  # a broken listener must not break configuration
                log.exception("config listener failed")
        return snapshot

    def add_listener(self, listener: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(listener)

    @property
    def auto_release_allowed(self) -> bool:
        with self._lock:
            return bool(self._values["automatic_disconnect_enabled"] and self._values["release_consent_acknowledged"])

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {**self._extra, **self._values}
        fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=False)
                handle.write("\n")
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
