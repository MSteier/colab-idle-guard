"""Notebook execution and runtime-connection state, per integration and notebook.

Safety model (ARCHITECTURE ADR-2):

* State is three-valued. Only a *fresh* IDLE report counts as idle.
* A report older than ``telemetry_fresh_seconds`` counts as UNKNOWN.
* When an integration is lost (no heartbeat, no goodbye), every notebook it
  reported becomes stale UNKNOWN. That includes notebooks last seen idle,
  because a broken channel tells us nothing about what happened since.
* A stale entry is cleared only by positive evidence: a fresh report for the
  same notebook (from any instance of the same integration kind), a goodbye
  showing it idle, a runtime-side verification, or an explicit user
  acknowledgement. Time alone never clears it.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Callable, Iterable

from .logging_manager import log_event

log = logging.getLogger(__name__)

# Only executions at least this long restart the idle timer when they end
# (ADR-9). Kernels also go briefly busy for completions and hover help while
# the user types; those blips still block release while they last, but must not
# keep restarting the idle timer.
MIN_EXECUTION_FOR_IDLE_RESET = 10.0


class ExecState(StrEnum):
    IDLE = "idle"
    BUSY = "busy"
    UNKNOWN = "unknown"


class RuntimeConn(StrEnum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class NotebookReport:
    """One notebook's state as reported by an integration."""

    notebook_id: str
    execution: ExecState
    running_cells: int
    runtime: RuntimeConn

    def normalised(self) -> "NotebookReport":
        # Contradictory reports resolve towards the safe side.
        if self.running_cells > 0 and self.execution is ExecState.IDLE:
            return replace(self, execution=ExecState.BUSY)
        if self.execution is ExecState.BUSY and self.running_cells <= 0:
            return replace(self, running_cells=1)
        return self


@dataclass(frozen=True)
class NotebookEntry:
    instance_id: str
    integration_kind: str
    notebook_id: str
    execution: ExecState
    running_cells: int
    runtime: RuntimeConn
    reported_at: float
    busy_since: float | None = None
    last_execution_end: float | None = None
    stale: bool = False
    stale_since: float | None = None
    stale_reason: str = ""
    last_known_execution: ExecState | None = None
    last_known_runtime: RuntimeConn | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.instance_id, self.notebook_id)


@dataclass(frozen=True)
class ReleaseTarget:
    instance_id: str
    integration_kind: str
    notebook_id: str


@dataclass(frozen=True)
class ExecutionAssessment:
    execution: ExecState
    runtime: RuntimeConn
    running_cells: int
    busy: tuple[NotebookEntry, ...]
    unknown: tuple[tuple[NotebookEntry, str], ...]
    connected_targets: tuple[ReleaseTarget, ...]
    last_execution_end: float | None
    oldest_stale_since: float | None
    entries: int
    by_integration: dict[str, ExecState] = field(default_factory=dict)


def _worst(a: ExecState, b: ExecState) -> ExecState:
    order = {ExecState.IDLE: 0, ExecState.UNKNOWN: 1, ExecState.BUSY: 2}
    return a if order[a] >= order[b] else b


class ExecutionMonitor:
    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], NotebookEntry] = {}
        self._last_execution_end: float | None = None

    # ---------------------------------------------------------------- updates

    def report(self, instance_id: str, kind: str, reports: Iterable[NotebookReport], full_snapshot: bool) -> None:
        """Apply reports from a live integration.

        ``full_snapshot`` means the list is every notebook the instance currently
        has open; notebooks it previously reported but omitted were closed.
        """
        now = self._clock()
        reports = [r.normalised() for r in reports]
        with self._lock:
            seen: set[str] = set()
            for r in reports:
                seen.add(r.notebook_id)
                self._apply_locked(instance_id, kind, r, now)
                self._supersede_locked(instance_id, kind, r.notebook_id)
            if full_snapshot:
                for key, entry in list(self._entries.items()):
                    if entry.instance_id != instance_id or entry.notebook_id in seen:
                        continue
                    if entry.stale and entry.last_known_execution is ExecState.IDLE:
                        # The same integration is back (e.g. VS Code window reloaded,
                        # Chrome restarted) and no longer shows this notebook. It was
                        # idle when contact was lost and had no attached client since,
                        # so nothing can have started running in it.
                        del self._entries[key]
                        log_event(log, "stale_state_cleared", instance=instance_id, reason="integration_returned_notebook_closed")
                    else:
                        self._retire_locked(entry, now, reason="notebook_closed")

    def mark_lost(self, instance_id: str) -> int:
        """The integration stopped responding: everything it reported is uncertain."""
        now = self._clock()
        count = 0
        with self._lock:
            for key, entry in list(self._entries.items()):
                if entry.instance_id == instance_id and not entry.stale:
                    self._entries[key] = self._make_stale(entry, now, "integration_lost")
                    count += 1
        if count:
            log_event(log, "execution_state_uncertain", logging.WARNING, instance=instance_id, notebooks=count, reason="integration_lost")
        return count

    def goodbye(self, instance_id: str, kind: str, final: Iterable[NotebookReport] | None) -> None:
        """Graceful close. Idle notebooks are dropped; busy/unknown ones become stale."""
        if final is not None:
            self.report(instance_id, kind, final, full_snapshot=True)
        now = self._clock()
        with self._lock:
            for entry in [e for e in self._entries.values() if e.instance_id == instance_id]:
                self._retire_locked(entry, now, reason="integration_closed")

    def resolve_stale(self, instance_id: str, notebook_id: str, verified_idle: bool, source: str) -> bool:
        """Runtime-side verification result for a stale entry (Phase 6 adapters)."""
        with self._lock:
            entry = self._entries.get((instance_id, notebook_id))
            if entry is None or not entry.stale:
                return False
            if verified_idle:
                del self._entries[entry.key]
            else:
                self._entries[entry.key] = replace(entry, stale_reason=f"{entry.stale_reason}; verification_failed:{source}")
        log_event(log, "stale_state_verified", instance=instance_id, verified_idle=verified_idle, source=source)
        return True

    def acknowledge_stale(self, actor: str = "user") -> int:
        """Explicit user statement that nothing is running in stale notebooks."""
        with self._lock:
            stale = [k for k, e in self._entries.items() if e.stale]
            for key in stale:
                del self._entries[key]
        if stale:
            log_event(log, "stale_state_acknowledged", logging.WARNING, actor=actor, notebooks=len(stale))
        return len(stale)

    def clear_all(self) -> None:
        with self._lock:
            self._entries.clear()

    # ------------------------------------------------------------ assessment

    def assess(self, fresh_seconds: float, now: float | None = None) -> ExecutionAssessment:
        now = self._clock() if now is None else now
        with self._lock:
            entries = list(self._entries.values())
            last_end = self._last_execution_end
        busy: list[NotebookEntry] = []
        unknown: list[tuple[NotebookEntry, str]] = []
        connected: list[ReleaseTarget] = []
        runtime_fresh_states: list[RuntimeConn] = []
        any_runtime_uncertain = False
        oldest_stale: float | None = None
        running_cells = 0
        by_integration: dict[str, ExecState] = {}

        for e in entries:
            fresh = not e.stale and (now - e.reported_at) <= fresh_seconds
            if e.stale:
                eff = ExecState.UNKNOWN
                unknown.append((e, e.stale_reason or "stale"))
                oldest_stale = e.stale_since if oldest_stale is None else min(oldest_stale, e.stale_since or now)
            elif e.execution is ExecState.BUSY:
                # A non-fresh BUSY is still treated as busy, never as idle.
                eff = ExecState.BUSY
                busy.append(e)
                running_cells += e.running_cells
            elif e.execution is ExecState.UNKNOWN or not fresh:
                eff = ExecState.UNKNOWN
                unknown.append((e, "reported_unknown" if fresh else "telemetry_not_fresh"))
            else:
                eff = ExecState.IDLE
            by_integration[e.integration_kind] = _worst(by_integration.get(e.integration_kind, ExecState.IDLE), eff)

            if fresh and e.runtime is not RuntimeConn.UNKNOWN:
                runtime_fresh_states.append(e.runtime)
                if e.runtime is RuntimeConn.CONNECTED:
                    connected.append(ReleaseTarget(e.instance_id, e.integration_kind, e.notebook_id))
            else:
                any_runtime_uncertain = True

        if busy:
            execution = ExecState.BUSY
        elif unknown or not entries:
            execution = ExecState.UNKNOWN
        else:
            execution = ExecState.IDLE

        if connected:
            runtime = RuntimeConn.CONNECTED
        elif entries and not any_runtime_uncertain and all(s is RuntimeConn.DISCONNECTED for s in runtime_fresh_states):
            runtime = RuntimeConn.DISCONNECTED
        else:
            runtime = RuntimeConn.UNKNOWN

        return ExecutionAssessment(
            execution=execution,
            runtime=runtime,
            running_cells=running_cells,
            busy=tuple(busy),
            unknown=tuple(unknown),
            connected_targets=tuple(connected),
            last_execution_end=last_end,
            oldest_stale_since=oldest_stale,
            entries=len(entries),
            by_integration=by_integration,
        )

    def entries(self) -> list[NotebookEntry]:
        with self._lock:
            return list(self._entries.values())

    # -------------------------------------------------------------- internals

    def _apply_locked(self, instance_id: str, kind: str, r: NotebookReport, now: float) -> None:
        key = (instance_id, r.notebook_id)
        prev = self._entries.get(key)
        prev_exec = prev.last_known_execution if prev and prev.stale else (prev.execution if prev else None)
        busy_since = prev.busy_since if prev else None
        last_end = prev.last_execution_end if prev else None
        if r.execution is ExecState.BUSY and prev_exec is not ExecState.BUSY:
            busy_since = now
            log_event(log, "execution_started", instance=instance_id, integration=kind, running_cells=r.running_cells)
        elif r.execution is not ExecState.BUSY and prev_exec is ExecState.BUSY:
            duration = now - busy_since if busy_since is not None else 0.0
            last_end = now
            busy_since = None
            if duration >= MIN_EXECUTION_FOR_IDLE_RESET:
                self._last_execution_end = now
            log_event(log, "execution_finished", instance=instance_id, integration=kind, state=str(r.execution),
                      duration_s=round(duration, 1))
        if prev is not None and prev.stale:
            log_event(log, "stale_state_recovered", instance=instance_id, integration=kind, state=str(r.execution))
        if prev is not None and prev.runtime is not r.runtime and not prev.stale:
            log_event(log, "runtime_state_changed", instance=instance_id, integration=kind, runtime=str(r.runtime))
        self._entries[key] = NotebookEntry(
            instance_id=instance_id,
            integration_kind=kind,
            notebook_id=r.notebook_id,
            execution=r.execution,
            running_cells=max(0, r.running_cells),
            runtime=r.runtime,
            reported_at=now,
            busy_since=busy_since,
            last_execution_end=last_end,
        )

    def _supersede_locked(self, instance_id: str, kind: str, notebook_id: str) -> None:
        """A fresh report about a notebook replaces stale entries for it from dead instances."""
        for key, entry in list(self._entries.items()):
            if (entry.stale and entry.instance_id != instance_id and entry.integration_kind == kind
                    and entry.notebook_id == notebook_id):
                del self._entries[key]
                log_event(log, "stale_state_superseded", old_instance=entry.instance_id, new_instance=instance_id)

    def _retire_locked(self, entry: NotebookEntry, now: float, reason: str) -> None:
        if entry.stale:
            return
        if entry.execution is ExecState.IDLE:
            del self._entries[entry.key]
            return
        # Closing a notebook (or VS Code) does not stop a remote kernel.
        self._entries[entry.key] = self._make_stale(entry, now, f"{reason}_while_{entry.execution}")
        log_event(log, "execution_state_uncertain", logging.WARNING, instance=entry.instance_id, reason=reason, last_state=str(entry.execution))

    @staticmethod
    def _make_stale(entry: NotebookEntry, now: float, reason: str) -> NotebookEntry:
        return replace(
            entry,
            stale=True,
            stale_since=now,
            stale_reason=reason,
            last_known_execution=entry.execution,
            last_known_runtime=entry.runtime,
            execution=ExecState.UNKNOWN,
            runtime=RuntimeConn.UNKNOWN,
        )
