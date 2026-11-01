"""Coordinates runtime release through adapters and confirms the outcome.

A release succeeds only when (1) every adapter reports success and (2) a fresh
report from the integration then shows the runtime disconnected. A success
claim from an adapter alone is not trusted.
"""

from __future__ import annotations

import logging
import secrets
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable

from ..runtime_adapters.base import ReleaseState, RuntimeAdapter
from ..runtime_adapters.notify_only import NotifyOnlyAdapter
from .execution_monitor import ExecutionMonitor, ReleaseTarget, RuntimeConn
from .logging_manager import log_event

log = logging.getLogger(__name__)

CONFIRM_TIMEOUT_SECONDS = 60.0
# Colab briefly shows "disconnected" while it reconnects (research §7.5), so a
# release only counts once every target has stayed disconnected this long.
CONFIRM_STABLE_SECONDS = 5.0
OPERATION_TIMEOUT_SECONDS = 180.0


class OperationPhase(StrEnum):
    RELEASING = "releasing"
    CONFIRMING = "confirming"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class ReleaseOperation:
    id: str
    targets: tuple[ReleaseTarget, ...]
    manual: bool
    started_at: float
    handles: dict[ReleaseTarget, str] = field(default_factory=dict)
    adapter_done_at: float | None = None
    disconnected_since: float | None = None
    phase: OperationPhase = OperationPhase.RELEASING
    detail: str = ""
    finished_at: float | None = None

    @property
    def done(self) -> bool:
        return self.phase in (OperationPhase.SUCCEEDED, OperationPhase.FAILED, OperationPhase.CANCELLED)

    def summary(self) -> dict:
        return {
            "id": self.id,
            "phase": str(self.phase),
            "manual": self.manual,
            "targets": len(self.targets),
            "detail": self.detail,
        }


class RuntimeManager:
    def __init__(self, clock: Callable[[], float], execution: ExecutionMonitor) -> None:
        self._clock = clock
        self._execution = execution
        self._lock = threading.Lock()
        self._default = NotifyOnlyAdapter()
        self._adapters: dict[str, RuntimeAdapter] = {}

    def register(self, integration_kind: str, adapter: RuntimeAdapter) -> None:
        with self._lock:
            self._adapters[integration_kind] = adapter
        log_event(log, "runtime_adapter_registered", integration=integration_kind, adapter=adapter.name,
                  supports_release=adapter.supports_release, destructive=adapter.destructive)

    def adapter_for(self, integration_kind: str) -> RuntimeAdapter:
        with self._lock:
            return self._adapters.get(integration_kind, self._default)

    def release_supported(self, targets: tuple[ReleaseTarget, ...]) -> tuple[bool, list[str]]:
        if not targets:
            return False, ["no connected runtime to release"]
        reasons = []
        for t in targets:
            ok, why = self.adapter_for(t.integration_kind).available(t)
            if not ok:
                reasons.append(why)
        return not reasons, reasons

    def manual_instructions(self, targets: tuple[ReleaseTarget, ...]) -> list[str]:
        seen: dict[str, str] = {}
        for t in targets:
            seen.setdefault(t.integration_kind, self.adapter_for(t.integration_kind).manual_instructions(t))
        return list(seen.values())

    def begin(self, targets: tuple[ReleaseTarget, ...], manual: bool) -> ReleaseOperation:
        op = ReleaseOperation(id=secrets.token_hex(6), targets=targets, manual=manual, started_at=self._clock())
        log_event(log, "runtime_release_attempt", operation=op.id, manual=manual, targets=len(targets),
                  adapters=sorted({self.adapter_for(t.integration_kind).name for t in targets}))
        for t in targets:
            try:
                op.handles[t] = self.adapter_for(t.integration_kind).begin_release(t)
            except Exception as exc:
                self._finish(op, OperationPhase.FAILED, f"adapter failed to start: {exc}")
                # Abort anything already started that has not been executed yet.
                self._cancel_handles(op)
                return op
        return op

    def cancel(self, op: ReleaseOperation, reason: str) -> bool:
        """Cancel only if no adapter has started the destructive step yet."""
        if op.done or op.phase is not OperationPhase.RELEASING:
            return False
        if all(self.adapter_for(t.integration_kind).cancel(h) for t, h in op.handles.items()):
            self._finish(op, OperationPhase.CANCELLED, reason)
            return True
        return False

    def poll(self, op: ReleaseOperation) -> ReleaseOperation:
        if op.done:
            return op
        now = self._clock()
        if now - op.started_at > OPERATION_TIMEOUT_SECONDS:
            self._finish(op, OperationPhase.FAILED, "timed out; runtime state could not be confirmed")
            return op
        if op.phase is OperationPhase.RELEASING:
            failures, pending = [], 0
            for t, h in op.handles.items():
                status = self.adapter_for(t.integration_kind).check(h)
                if status.state is ReleaseState.FAILED:
                    failures.append(status.detail or "failed")
                elif status.state is ReleaseState.PENDING:
                    pending += 1
            if failures:
                self._finish(op, OperationPhase.FAILED, "; ".join(failures))
            elif pending == 0:
                op.phase, op.adapter_done_at = OperationPhase.CONFIRMING, now
                log_event(log, "runtime_release_confirming", operation=op.id)
        if op.phase is OperationPhase.CONFIRMING:
            if not self._confirmed(op):
                op.disconnected_since = None
            elif op.disconnected_since is None:
                op.disconnected_since = now
            if op.disconnected_since is not None and now - op.disconnected_since >= CONFIRM_STABLE_SECONDS:
                self._finish(op, OperationPhase.SUCCEEDED, "runtime reported disconnected")
            elif now - (op.adapter_done_at or now) > CONFIRM_TIMEOUT_SECONDS:
                self._finish(op, OperationPhase.FAILED, "adapter reported success but disconnection was not confirmed")
        return op

    def _confirmed(self, op: ReleaseOperation) -> bool:
        entries = {(e.instance_id, e.notebook_id): e for e in self._execution.entries()}
        for t in op.targets:
            e = entries.get((t.instance_id, t.notebook_id))
            if e is None or e.stale or e.runtime is not RuntimeConn.DISCONNECTED:
                return False
            if op.adapter_done_at is None or e.reported_at < op.adapter_done_at:
                return False
        return True

    def _cancel_handles(self, op: ReleaseOperation) -> None:
        for t, h in op.handles.items():
            try:
                self.adapter_for(t.integration_kind).cancel(h)
            except Exception:
                log.exception("adapter cancel failed")

    def _finish(self, op: ReleaseOperation, phase: OperationPhase, detail: str) -> None:
        op.phase, op.detail, op.finished_at = phase, detail, self._clock()
        level = logging.INFO if phase is not OperationPhase.FAILED else logging.ERROR
        log_event(log, "runtime_release_result", level, operation=op.id, result=str(phase), detail=detail)
