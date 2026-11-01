"""Shared Colab-interaction timestamp across all integrations.

Only events that integrations report from a monitored Colab surface reach this
class, so activity elsewhere in Windows never resets the timer. Events carry a
kind (never any content) and an age, so batched events are dated correctly.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

from .logging_manager import log_event

log = logging.getLogger(__name__)

ACTIVITY_KINDS = frozenset({"pointer", "click", "key", "scroll", "edit", "execute", "focus", "notebook_ui"})
MAX_EVENT_AGE_SECONDS = 120.0

# (integration_kind, at_monotonic) -> True / False / None (undecidable)
Corroborator = Callable[[str, float], "bool | None"]


@dataclass(frozen=True)
class ActivityResult:
    accepted: int
    rejected: int
    reasons: tuple[str, ...]


class ActivityMonitor:
    def __init__(self, clock: Callable[[], float], corroborator: Corroborator | None = None) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self.corroborator = corroborator
        self.monitored_kinds: set[str] = {"chrome", "vscode"}
        # Restart rule: the idle clock starts at startup, so a restart can never
        # make a release happen sooner (ARCHITECTURE §5).
        self._last_activity = clock()
        self._last_by_kind: dict[str, float] = {}
        self._last_rejected_log = 0.0

    @property
    def last_activity(self) -> float:
        with self._lock:
            return self._last_activity

    def last_activity_for(self, integration_kind: str) -> float | None:
        with self._lock:
            return self._last_by_kind.get(integration_kind)

    def idle_seconds(self, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        with self._lock:
            return max(0.0, now - self._last_activity)

    def reset(self, reason: str) -> None:
        """Treat ``now`` as activity (e.g. the user cancelled a pending release)."""
        now = self._clock()
        with self._lock:
            self._last_activity = max(self._last_activity, now)
        log_event(log, "activity_timer_reset", reason=reason)

    def record(self, integration_kind: str, events: list[tuple[str, float]]) -> ActivityResult:
        """Record ``(kind, age_seconds)`` events from one integration."""
        now = self._clock()
        accepted = rejected = 0
        reasons: list[str] = []
        newest: float | None = None
        for kind, age in events:
            if integration_kind not in self.monitored_kinds:
                rejected += 1
                reasons.append("integration_not_monitored")
                continue
            if kind not in ACTIVITY_KINDS:
                rejected += 1
                reasons.append("unknown_kind")
                continue
            if not (0 <= age <= MAX_EVENT_AGE_SECONDS):
                rejected += 1
                reasons.append("bad_age")
                continue
            at = now - age
            if self.corroborator is not None:
                verdict = self.corroborator(integration_kind, at)
                # None (no sample) is accepted: wrongly rejecting real activity would
                # risk releasing a runtime the user is working in, whereas wrongly
                # accepting only delays a release.
                if verdict is False:
                    rejected += 1
                    reasons.append("app_not_foreground")
                    continue
            accepted += 1
            newest = at if newest is None else max(newest, at)
        if newest is not None:
            with self._lock:
                self._last_activity = max(self._last_activity, newest)
                self._last_by_kind[integration_kind] = max(self._last_by_kind.get(integration_kind, newest), newest)
        if accepted:
            log.debug("activity", extra={"event": "activity", "integration": integration_kind, "accepted": accepted})
        if rejected and now - self._last_rejected_log > 30:
            self._last_rejected_log = now
            log_event(log, "activity_rejected", integration=integration_kind, rejected=rejected, reasons=sorted(set(reasons)))
        return ActivityResult(accepted, rejected, tuple(reasons))
