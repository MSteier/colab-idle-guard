"""Shared test fixtures: fake clock, mock runtime adapter and a simulation harness.

No test touches a real Colab runtime. The MockAdapter stands in for every
release mechanism and records whether a release was ever attempted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from colab_idle_guard.runtime_adapters.base import ReleaseState, ReleaseStatus, RuntimeAdapter
from colab_idle_guard.watchdog.activity_monitor import ActivityMonitor
from colab_idle_guard.watchdog.configuration import ConfigManager
from colab_idle_guard.watchdog.execution_monitor import (
    ExecState, ExecutionMonitor, NotebookReport, ReleaseTarget, RuntimeConn,
)
from colab_idle_guard.watchdog.integrations import IntegrationRegistry
from colab_idle_guard.watchdog.notification_manager import NotificationManager
from colab_idle_guard.watchdog.runtime_manager import RuntimeManager
from colab_idle_guard.watchdog.shutdown_controller import GuardState, ShutdownController

IDLE, BUSY, UNKNOWN = ExecState.IDLE, ExecState.BUSY, ExecState.UNKNOWN
CONNECTED, DISCONNECTED = RuntimeConn.CONNECTED, RuntimeConn.DISCONNECTED


class FakeClock:
    def __init__(self, start: float = 10_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class MockAdapter(RuntimeAdapter):
    name = "mock"
    supports_release = True
    destructive = True

    def __init__(self) -> None:
        self.calls: list[ReleaseTarget] = []
        self.statuses: dict[str, ReleaseStatus] = {}
        self.cancellable = True

    def begin_release(self, target: ReleaseTarget) -> str:
        handle = f"h{len(self.calls)}"
        self.calls.append(target)
        self.statuses[handle] = ReleaseStatus(ReleaseState.PENDING)
        return handle

    def check(self, handle: str) -> ReleaseStatus:
        return self.statuses[handle]

    def cancel(self, handle: str) -> bool:
        if self.cancellable and self.statuses[handle].state is ReleaseState.PENDING:
            self.statuses[handle] = ReleaseStatus(ReleaseState.FAILED, "cancelled")
            return True
        return False

    def finish(self, ok: bool = True, detail: str = "") -> None:
        for h, s in self.statuses.items():
            if s.state is ReleaseState.PENDING:
                self.statuses[h] = ReleaseStatus(ReleaseState.SUCCEEDED if ok else ReleaseState.FAILED, detail)


class RecordingBackend:
    def __init__(self) -> None:
        self.shown: list[tuple[str, str, str]] = []

    def show(self, title: str, message: str, level: str) -> None:
        self.shown.append((title, message, level))


@dataclass
class Live:
    kind: str
    notebooks: dict[str, tuple[ExecState, RuntimeConn, int]] = field(default_factory=dict)


class Harness:
    """Drives the watchdog core with simulated integrations and a fake clock."""

    def __init__(self, tmp_path: Path, auto_release: bool = True, **config: object) -> None:
        self.clock = FakeClock()
        self.config = ConfigManager(tmp_path / "config.json")
        self.config.load()
        self.config.update({"automatic_disconnect_enabled": auto_release,
                            "release_consent_acknowledged": auto_release, **config})
        self.registry = IntegrationRegistry(self.clock)
        self.activity = ActivityMonitor(self.clock)
        self.execution = ExecutionMonitor(self.clock)
        self.runtime = RuntimeManager(self.clock, self.execution)
        self.backend = RecordingBackend()
        self.notifier = NotificationManager(self.clock, enabled=lambda: True)
        self.notifier.add_backend(self.backend)
        self.adapter = MockAdapter()
        self.runtime.register("chrome", self.adapter)
        self.runtime.register("vscode", self.adapter)
        self.controller = ShutdownController(self.clock, self.config, self.registry, self.activity,
                                             self.execution, self.runtime, self.notifier)
        self.live: dict[str, Live] = {}
        self._since_beat = 0.0

    # -- integration simulation

    def connect(self, instance: str, kind: str, **notebooks: tuple) -> None:
        self.live[instance] = Live(kind)
        self.set_state(instance, **notebooks)

    def set_state(self, instance: str, **notebooks: tuple) -> None:
        live = self.live[instance]
        for nb, value in notebooks.items():
            execution, runtime, *cells = value
            live.notebooks[nb] = (execution, runtime, cells[0] if cells else (1 if execution is BUSY else 0))
        self.beat(instance)

    def beat(self, instance: str) -> None:
        live = self.live[instance]
        self.registry.touch(instance, live.kind)
        reports = [NotebookReport(nb, e, c, r) for nb, (e, r, c) in live.notebooks.items()]
        self.execution.report(instance, live.kind, reports, full_snapshot=True)

    def interact(self, instance: str, kind: str = "click") -> None:
        live = self.live[instance]
        self.registry.touch(instance, live.kind)
        self.activity.record(live.kind, [(kind, 0.0)])

    def close(self, instance: str) -> None:
        """Graceful close (goodbye)."""
        live = self.live.pop(instance)
        self.execution.goodbye(instance, live.kind, None)
        self.registry.close(instance)

    def crash(self, instance: str) -> None:
        """Integration disappears without goodbye; it simply stops sending heartbeats."""
        self.live.pop(instance)

    # -- time

    def tick(self):
        return self.controller.tick()

    def run(self, seconds: float, step: float = 1.0, heartbeat: float = 15.0) -> None:
        elapsed = 0.0
        while elapsed < seconds:
            self.clock.advance(step)
            elapsed += step
            self._since_beat += step
            if self._since_beat >= heartbeat:
                self._since_beat = 0.0
                for instance in list(self.live):
                    self.beat(instance)
            self.controller.tick()

    def run_until(self, state: GuardState, limit: float, step: float = 1.0) -> float:
        waited = 0.0
        while self.controller.state is not state:
            if waited >= limit:
                raise AssertionError(f"state {state} not reached within {limit}s (now {self.controller.state})")
            self.run(step, step=step)
            waited += step
        return waited

    @property
    def state(self) -> GuardState:
        return self.controller.state

    @property
    def timeout(self) -> float:
        return self.config.get("idle_timeout_minutes") * 60.0


@pytest.fixture
def harness(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


@pytest.fixture
def make_harness(tmp_path: Path):
    counter = {"n": 0}

    def factory(**kwargs) -> Harness:
        counter["n"] += 1
        d = tmp_path / f"h{counter['n']}"
        d.mkdir()
        return Harness(d, **kwargs)

    return factory
