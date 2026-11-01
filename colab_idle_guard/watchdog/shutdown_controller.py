"""Idle-detection and release state machine (ARCHITECTURE §4).

``tick()`` is called about once per second. Every decision is made from a fresh
assessment of activity, execution and runtime state; nothing is cached between
ticks except the current phase. All release checks are evaluated when a warning
starts, on every tick of the warning, and once more immediately before the
release command is issued.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Callable

from .activity_monitor import ActivityMonitor
from .configuration import ConfigManager
from .execution_monitor import ExecState, ExecutionAssessment, ExecutionMonitor, ReleaseTarget, RuntimeConn
from .integrations import IntegrationRegistry
from .logging_manager import log_event
from .notification_manager import NotificationManager
from .runtime_manager import OperationPhase, ReleaseOperation, RuntimeManager

log = logging.getLogger(__name__)

ACTIVE_WINDOW_SECONDS = 60.0


class GuardState(StrEnum):
    MONITORING = "monitoring"            # waiting for integrations / no managed runtime
    ACTIVE = "active"                    # recent Colab interaction
    EXECUTING = "executing"              # a cell is running; release prohibited
    IDLE_COUNTDOWN = "idle_countdown"
    WARNING = "warning"                  # release pending, user can cancel
    DISCONNECTING = "disconnecting"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"                  # idle, but state cannot be verified: release blocked
    IDLE_NOTIFIED = "idle_notified"      # idle limit reached, user told (notify-only mode)
    RELEASE_FAILED = "release_failed"
    PAUSED = "paused"


STATE_LABELS = {
    GuardState.MONITORING: "Monitoring",
    GuardState.ACTIVE: "Active Colab session",
    GuardState.EXECUTING: "Notebook executing",
    GuardState.IDLE_COUNTDOWN: "Idle countdown",
    GuardState.WARNING: "Shutdown warning",
    GuardState.DISCONNECTING: "Disconnecting",
    GuardState.DISCONNECTED: "Disconnected",
    GuardState.UNKNOWN: "Runtime state unknown",
    GuardState.IDLE_NOTIFIED: "Idle - action needed",
    GuardState.RELEASE_FAILED: "Release failed",
    GuardState.PAUSED: "Monitoring paused",
}


@dataclass(frozen=True)
class GuardStatus:
    state: str
    label: str
    idle_seconds: float
    timeout_seconds: float
    remaining_seconds: float
    warning_remaining_seconds: float | None
    runtime: str
    execution: str
    running_cells: int
    blockers: tuple[str, ...]
    integrations: tuple[dict[str, Any], ...]
    auto_release_enabled: bool
    release_supported: bool
    last_operation: dict[str, Any] | None
    paused: bool
    stale_notebooks: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _Evaluation:
    now: float
    idle_ref: float
    idle_seconds: float
    timeout: float
    assessment: ExecutionAssessment
    targets: tuple[ReleaseTarget, ...]
    blockers: tuple[str, ...]


class ShutdownController:
    def __init__(
        self,
        clock: Callable[[], float],
        config: ConfigManager,
        registry: IntegrationRegistry,
        activity: ActivityMonitor,
        execution: ExecutionMonitor,
        runtime: RuntimeManager,
        notifier: NotificationManager,
    ) -> None:
        self._clock = clock
        self._config = config
        self._registry = registry
        self._activity = activity
        self._execution = execution
        self._runtime = runtime
        self._notifier = notifier
        self._lock = threading.RLock()
        self._state = GuardState.MONITORING
        self._paused = False
        self._warning_started: float | None = None
        self._warning_deadline: float | None = None
        self._warning_activity_ref: float | None = None
        self._operation: ReleaseOperation | None = None
        self._last_operation: ReleaseOperation | None = None
        self._episode_ref: float | None = None
        self._episode_flags: set[str] = set()
        # Idle reference of the episode in which a release failed. Kept apart from
        # the displayed state, which other events overwrite, so a failed release
        # is never retried automatically until a new idle episode begins.
        self._failed_episode_ref: float | None = None
        self._listeners: list[Callable[[GuardStatus], None]] = []
        self._last_eval: _Evaluation | None = None

    # ------------------------------------------------------------- public API

    @property
    def state(self) -> GuardState:
        with self._lock:
            return self._state

    def add_listener(self, listener: Callable[[GuardStatus], None]) -> None:
        self._listeners.append(listener)

    def tick(self) -> GuardStatus:
        with self._lock:
            before = self._state
            self._tick_locked()
            status = self._status_locked()
        if status.state != before:
            log_event(log, "guard_state_changed", previous=str(before), state=status.state, blockers=list(status.blockers))
        for listener in list(self._listeners):
            try:
                listener(status)
            except Exception:
                log.exception("status listener failed")
        return status

    def status(self) -> GuardStatus:
        with self._lock:
            if self._last_eval is None:
                self._last_eval = self._evaluate()
            return self._status_locked()

    def pause(self) -> None:
        with self._lock:
            self._paused = True
            self._cancel_warning_locked("monitoring_paused")
        log_event(log, "monitoring_paused")

    def resume(self) -> None:
        with self._lock:
            self._paused = False
        # A full timeout starts again after resuming.
        self._activity.reset("monitoring_resumed")
        log_event(log, "monitoring_resumed")

    def cancel_pending(self) -> bool:
        """User pressed Cancel during the warning (or while a release is queued)."""
        with self._lock:
            cancelled = False
            if self._state is GuardState.WARNING:
                self._cancel_warning_locked("user_cancelled")
                cancelled = True
            elif self._operation is not None and self._runtime.cancel(self._operation, "user_cancelled"):
                self._finish_operation_locked()
                cancelled = True
        if cancelled:
            self._activity.reset("user_cancelled_release")
        return cancelled

    def request_manual_release(self) -> tuple[bool, str]:
        """Release now at the user's explicit request. Execution safety still applies."""
        with self._lock:
            if self._operation is not None:
                return False, "a release is already in progress"
            ev = self._evaluate()
            self._last_eval = ev
            if ev.assessment.runtime is not RuntimeConn.CONNECTED or not ev.targets:
                return False, "no connected runtime is being reported"
            hard = [b for b in ev.blockers if not b.startswith("idle_")]
            if hard:
                return False, "release blocked: " + "; ".join(hard)
            ok, reasons = self._runtime.release_supported(ev.targets)
            if not ok:
                return False, "; ".join(reasons + self._runtime.manual_instructions(ev.targets))
            self._cancel_warning_locked("manual_release")
            self._start_release_locked(ev, manual=True)
            return True, "release started"

    def acknowledge_unknown_state(self) -> int:
        """User confirms nothing is running in notebooks whose state became stale."""
        return self._execution.acknowledge_stale(actor="user")

    # ------------------------------------------------------------- evaluation

    def _evaluate(self) -> _Evaluation:
        now = self._clock()
        cfg = self._config.snapshot()
        a = self._execution.assess(cfg["telemetry_fresh_seconds"], now)
        idle_ref = self._activity.last_activity
        if cfg["idle_timer_starts_after_execution"] and a.last_execution_end is not None:
            idle_ref = max(idle_ref, a.last_execution_end)
        idle = max(0.0, now - idle_ref)
        timeout = cfg["idle_timeout_minutes"] * 60.0
        monitored = {k for k in ("chrome", "vscode") if cfg[f"monitor_{k}"]}
        targets = tuple(t for t in a.connected_targets if t.integration_kind in monitored)

        blockers: list[str] = []
        # Running-cell protection and verified state are enforced unconditionally
        # (configuration.ALWAYS_ENFORCED); the settings are informational.
        if a.execution is ExecState.BUSY:
            blockers.append(f"execution_active ({a.running_cells} running cell(s))")
        if a.unknown:
            reasons = sorted({reason for _, reason in a.unknown})
            blockers.append("execution_state_unknown (" + ", ".join(reasons) + ")")
        elif a.execution is ExecState.UNKNOWN:
            blockers.append("execution_state_unknown (no telemetry)")
        if a.runtime is RuntimeConn.UNKNOWN:
            blockers.append("runtime_state_unknown")
        if idle < timeout:
            blockers.append("idle_time_not_reached")
        if self._paused:
            blockers.append("idle_monitoring_paused")
        return _Evaluation(now, idle_ref, idle, timeout, a, targets, tuple(blockers))

    def _tick_locked(self) -> None:
        cfg = self._config.snapshot()
        for lost in self._registry.expire(cfg["integration_timeout_seconds"]):
            log_event(log, "integration_lost", logging.WARNING, integration=lost.kind, instance=lost.instance_id)
            self._execution.mark_lost(lost.instance_id)

        ev = self._evaluate()
        self._last_eval = ev
        a = ev.assessment
        self._track_episode_locked(ev)

        if self._operation is not None:
            self._poll_operation_locked(ev)
            return
        if self._paused:
            self._state = GuardState.PAUSED
            return
        if a.runtime is RuntimeConn.DISCONNECTED:
            self._cancel_warning_locked("runtime_disconnected")
            self._state = GuardState.DISCONNECTED
            return
        if a.execution is ExecState.BUSY:
            self._cancel_warning_locked("execution_started")
            self._state = GuardState.EXECUTING
            return
        if self._state is GuardState.WARNING:
            self._warning_tick_locked(ev)
            return

        if ev.idle_seconds < ev.timeout:
            if a.entries == 0 or (a.runtime is RuntimeConn.CONNECTED and not ev.targets):
                self._state = GuardState.MONITORING
            elif a.runtime is RuntimeConn.UNKNOWN or a.execution is ExecState.UNKNOWN:
                self._state = GuardState.UNKNOWN
            else:
                self._state = GuardState.ACTIVE if ev.idle_seconds < ACTIVE_WINDOW_SECONDS else GuardState.IDLE_COUNTDOWN
            return

        # Idle limit reached.
        if self._failed_episode_ref is not None and ev.idle_ref <= self._failed_episode_ref:
            self._state = GuardState.RELEASE_FAILED  # no blind retries; a new idle episode is required
            return
        if a.entries == 0 or (a.runtime is RuntimeConn.CONNECTED and not ev.targets):
            self._state = GuardState.MONITORING  # nothing reported, or nothing we manage
            return
        if ev.blockers:
            self._state = GuardState.UNKNOWN
            self._notify_blocked_locked(ev, cfg)
            return
        ok, reasons = self._runtime.release_supported(ev.targets)
        if self._config.auto_release_allowed and ok:
            self._start_warning_locked(ev, cfg)
        else:
            self._state = GuardState.IDLE_NOTIFIED
            if self._once("idle_notified"):
                minutes = int(ev.timeout // 60)
                why = "Automatic release is disabled." if not self._config.auto_release_allowed else "; ".join(reasons)
                steps = " ".join(self._runtime.manual_instructions(ev.targets))
                self._notifier.notify("idle_notified", "Colab runtime idle",
                                      f"No Colab activity for {minutes} min and no cells running. {why} {steps}",
                                      level="warning")

    def _warning_tick_locked(self, ev: _Evaluation) -> None:
        assert self._warning_deadline is not None
        if self._warning_activity_ref is not None and ev.idle_ref > self._warning_activity_ref:
            self._cancel_warning_locked("activity")
            self._state = GuardState.ACTIVE
            return
        if ev.blockers or not ev.targets:
            self._cancel_warning_locked("conditions_changed: " + "; ".join(ev.blockers or ("no_target",)))
            self._state = GuardState.UNKNOWN
            return
        if ev.now < self._warning_deadline:
            return
        # Final recheck immediately before release, from a brand-new evaluation.
        final = self._evaluate()
        _, problems = self._runtime.release_supported(final.targets)
        problems = list(final.blockers) + problems
        if not self._config.auto_release_allowed:
            problems.append("automatic_release_disabled")
        if self._warning_activity_ref is not None and final.idle_ref > self._warning_activity_ref:
            problems.append("activity")
        if problems:
            self._cancel_warning_locked("final_check_failed: " + "; ".join(problems))
            self._state = GuardState.UNKNOWN
            return
        self._warning_started = self._warning_deadline = self._warning_activity_ref = None
        self._start_release_locked(final, manual=False)

    # --------------------------------------------------------------- helpers

    def _start_warning_locked(self, ev: _Evaluation, cfg: dict[str, Any]) -> None:
        seconds = cfg["warning_countdown_seconds"]
        self._state = GuardState.WARNING
        self._warning_started = ev.now
        self._warning_deadline = ev.now + seconds
        self._warning_activity_ref = ev.idle_ref
        log_event(log, "shutdown_warning", seconds=seconds, targets=len(ev.targets))
        self._notifier.notify("warning", "Colab runtime will be released",
                              f"No Colab activity for {int(ev.timeout // 60)} min. The runtime will be released in "
                              f"{seconds} s (variables and VM files will be lost). Interact with Colab or press Cancel to keep it.",
                              level="warning")

    def _cancel_warning_locked(self, reason: str) -> None:
        if self._state is not GuardState.WARNING:
            return
        self._warning_started = self._warning_deadline = self._warning_activity_ref = None
        self._state = GuardState.MONITORING
        log_event(log, "shutdown_cancelled", reason=reason)
        self._notifier.notify("cancelled", "Runtime release cancelled", f"Reason: {reason}", level="info")

    def _start_release_locked(self, ev: _Evaluation, manual: bool) -> None:
        self._state = GuardState.DISCONNECTING
        self._operation = self._runtime.begin(ev.targets, manual=manual)
        if self._operation.done:
            self._finish_operation_locked()

    def _poll_operation_locked(self, ev: _Evaluation) -> None:
        op = self._operation
        assert op is not None
        # Late activity or execution: abort if the destructive step has not been delivered yet.
        if op.phase is OperationPhase.RELEASING and (ev.assessment.execution is ExecState.BUSY or ev.idle_ref > op.started_at):
            if self._runtime.cancel(op, "activity_or_execution_during_release"):
                self._finish_operation_locked()
                return
        self._runtime.poll(op)
        if op.done:
            self._finish_operation_locked()
        else:
            self._state = GuardState.DISCONNECTING

    def _finish_operation_locked(self) -> None:
        op = self._operation
        assert op is not None
        self._operation, self._last_operation = None, op
        if op.phase is OperationPhase.SUCCEEDED:
            self._state = GuardState.DISCONNECTED
            self._notifier.notify("released", "Colab runtime released", "The idle runtime was released.", level="info")
        elif op.phase is OperationPhase.CANCELLED:
            self._state = GuardState.MONITORING
        else:
            self._state = GuardState.RELEASE_FAILED
            self._failed_episode_ref = self._episode_ref
            self._notifier.notify("release_failed", "Colab runtime release failed",
                                  f"{op.detail}. The runtime may still be using compute. "
                                  + " ".join(self._runtime.manual_instructions(op.targets)), level="error")

    def _notify_blocked_locked(self, ev: _Evaluation, cfg: dict[str, Any]) -> None:
        a = ev.assessment
        if a.oldest_stale_since is not None and ev.now - a.oldest_stale_since > cfg["stale_verification_max_minutes"] * 60:
            if self._once("stale_too_long"):
                self._notifier.notify("stale", "Colab state could not be verified",
                                      "An integration stopped reporting while a notebook may have been running. "
                                      "Release is blocked. Reopen the notebook, or confirm in the tray menu that nothing is running.",
                                      level="warning")
        elif self._once("blocked"):
            self._notifier.notify("blocked", "Colab runtime idle, but release is blocked",
                                  "; ".join(ev.blockers), level="warning")

    def _track_episode_locked(self, ev: _Evaluation) -> None:
        """An idle episode starts at each new activity; one-shot notifications reset then."""
        if self._episode_ref is None or ev.idle_ref > self._episode_ref:
            self._episode_ref = ev.idle_ref
            self._episode_flags.clear()
            if self._state in (GuardState.RELEASE_FAILED, GuardState.IDLE_NOTIFIED, GuardState.DISCONNECTED):
                self._state = GuardState.MONITORING

    def _once(self, flag: str) -> bool:
        if flag in self._episode_flags:
            return False
        self._episode_flags.add(flag)
        return True

    def _status_locked(self) -> GuardStatus:
        ev = self._last_eval or self._evaluate()
        a = ev.assessment
        warning_remaining = None
        if self._state is GuardState.WARNING and self._warning_deadline is not None:
            warning_remaining = max(0.0, self._warning_deadline - self._clock())
        supported, _ = self._runtime.release_supported(ev.targets)
        integrations = tuple(
            {"kind": i.kind, "instance": i.instance_id, "status": str(i.status), "version": i.version,
             "seconds_since_seen": round(ev.now - i.last_seen, 1)}
            for i in self._registry.all()
        )
        return GuardStatus(
            state=str(self._state),
            label=STATE_LABELS[self._state],
            idle_seconds=round(ev.idle_seconds, 1),
            timeout_seconds=ev.timeout,
            remaining_seconds=round(max(0.0, ev.timeout - ev.idle_seconds), 1),
            warning_remaining_seconds=None if warning_remaining is None else round(warning_remaining, 1),
            runtime=str(a.runtime),
            execution=str(a.execution),
            running_cells=a.running_cells,
            blockers=tuple(b for b in ev.blockers if b != "idle_time_not_reached"),
            integrations=integrations,
            auto_release_enabled=self._config.auto_release_allowed,
            release_supported=supported,
            last_operation=self._last_operation.summary() if self._last_operation else None,
            paused=self._paused,
            stale_notebooks=sum(1 for e, _ in a.unknown if e.stale),
        )
