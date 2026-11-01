"""User notifications with pluggable backends and de-duplication.

The core only knows the backend protocol; the desktop layer adds a Windows
tray/toast backend. Notifications are always logged even when disabled.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Protocol

from .logging_manager import log_event

log = logging.getLogger(__name__)


class NotificationBackend(Protocol):
    def show(self, title: str, message: str, level: str) -> None: ...


class NotificationManager:
    def __init__(self, clock: Callable[[], float], enabled: Callable[[], bool]) -> None:
        self._clock = clock
        self._enabled = enabled
        self._backends: list[NotificationBackend] = []
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()
        self.history: list[tuple[str, str, str, str]] = []  # (key, title, message, level), bounded

    def add_backend(self, backend: NotificationBackend) -> None:
        self._backends.append(backend)

    def notify(self, key: str, title: str, message: str, level: str = "info",
               dedupe_seconds: float | None = None, force: bool = False) -> bool:
        """Show a notification. ``dedupe_seconds`` suppresses repeats of the same key."""
        now = self._clock()
        with self._lock:
            last = self._last_sent.get(key)
            if dedupe_seconds is not None and last is not None and now - last < dedupe_seconds:
                return False
            self._last_sent[key] = now
            self.history.append((key, title, message, level))
            del self.history[:-50]
        log_event(log, "notification", key=key, title=title, severity=level)
        if not (force or self._enabled()):
            return False
        for backend in list(self._backends):
            try:
                backend.show(title, message, level)
            except Exception:
                log.exception("notification backend failed")
        return True

    def forget(self, key: str) -> None:
        with self._lock:
            self._last_sent.pop(key, None)
