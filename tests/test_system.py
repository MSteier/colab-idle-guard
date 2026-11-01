"""Full-stack system tests: real WatchdogService + real HTTP API + real release
adapters, driven by simulated Chrome / VS Code integrations that behave like the
extensions (heartbeats, activity, executing release commands).
"""

from __future__ import annotations

import http.client
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from colab_idle_guard.watchdog.main import WatchdogService
from colab_idle_guard.watchdog.paths import AppPaths
from colab_idle_guard.watchdog.shutdown_controller import GuardState

from conftest import FakeClock


@dataclass
class SimNotebook:
    execution: str = "idle"
    runtime: str = "connected"
    running_cells: int = 0


@dataclass
class SimIntegration:
    """Behaves like an extension: reports state, collects and executes commands."""

    service: WatchdogService
    instance: str
    kind: str
    notebooks: dict[str, SimNotebook] = field(default_factory=dict)
    release_behaviour: str = "obey"  # obey | refuse | ignore
    executed: list[dict[str, Any]] = field(default_factory=list)

    def _post(self, path: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        conn = http.client.HTTPConnection("127.0.0.1", self.service.api.port, timeout=5)
        data = json.dumps({"instance_id": self.instance, "integration": self.kind, "version": "sim", **body})
        conn.request("POST", path, body=data, headers={
            "Authorization": f"Bearer {self.service.token}", "Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, payload

    def wire_notebooks(self) -> list[dict[str, Any]]:
        return [{"notebook_id": nb, "execution": n.execution, "running_cells": n.running_cells, "runtime": n.runtime}
                for nb, n in self.notebooks.items()]

    def heartbeat(self) -> None:
        status, body = self._post("/v1/heartbeat", {"notebooks": self.wire_notebooks()})
        assert status == 200, body
        for cmd in body["commands"]:
            self._execute(cmd)

    def _execute(self, cmd: dict[str, Any]) -> None:
        self.executed.append(cmd)
        if self.release_behaviour == "ignore":
            return  # integration died before answering
        nb = self.notebooks.get(cmd["payload"]["notebook_id"])
        if self.release_behaviour == "refuse" or nb is None or nb.execution != "idle":
            ok, detail = False, "refused: execution is busy"
        else:
            nb.runtime = "disconnected"
            ok, detail = True, "released"
        status, _ = self._post("/v1/command_result", {"command_id": cmd["id"], "nonce": cmd["nonce"], "ok": ok, "detail": detail})
        assert status == 200

    def activity(self, kind: str = "click") -> None:
        assert self._post("/v1/activity", {"events": [{"kind": kind, "age_ms": 0}]})[0] == 200

    def goodbye(self) -> None:
        assert self._post("/v1/goodbye", {"notebooks": self.wire_notebooks()})[0] == 200


class System:
    def __init__(self, root: Path, clock: FakeClock, auto: bool = True, **cfg: Any) -> None:
        self.clock = clock
        self.service = WatchdogService(paths=AppPaths(root / "c", root / "d"), clock=clock, port=0,
                                       foreground_sampler=lambda: None)
        self.service.config.update({"automatic_disconnect_enabled": auto, "release_consent_acknowledged": auto,
                                    "require_foreground_corroboration": False, **cfg})
        self.service.start()
        self.sims: list[SimIntegration] = []

    def sim(self, instance: str, kind: str, **notebooks: SimNotebook) -> SimIntegration:
        s = SimIntegration(self.service, instance, kind, dict(notebooks))
        self.sims.append(s)
        s.heartbeat()
        return s

    def run(self, seconds: float, alive: list[SimIntegration] | None = None, beat_every: float = 10.0) -> None:
        """Advance simulated time; live integrations heartbeat like the real extensions."""
        alive = self.sims if alive is None else alive
        t = 0.0
        since_beat = 0.0
        while t < seconds:
            self.clock.advance(1.0)
            t += 1.0
            since_beat += 1.0
            if since_beat >= beat_every:
                since_beat = 0.0
                for s in alive:
                    s.heartbeat()
            self.service.controller.tick()

    @property
    def state(self) -> GuardState:
        return self.service.controller.state

    def status(self) -> dict[str, Any]:
        return self.service.status()

    def stop(self) -> None:
        self.service.stop()


@pytest.fixture
def system(tmp_path: Path):
    s = System(tmp_path, FakeClock())
    yield s
    s.stop()


TIMEOUT = 30 * 60


def test_automatic_release_end_to_end_over_http(system: System) -> None:
    chrome = system.sim("c1", "chrome", nb1=SimNotebook())
    chrome.activity()
    system.run(TIMEOUT - 5)
    assert system.state is GuardState.IDLE_COUNTDOWN and chrome.executed == []
    system.run(10)
    assert system.state is GuardState.WARNING
    system.run(55)
    assert chrome.executed == []  # nothing before the countdown ends
    system.run(30)
    assert [c["type"] for c in chrome.executed] == ["release_runtime"]
    assert system.state is GuardState.DISCONNECTED
    assert system.status()["last_operation"]["phase"] == "succeeded"
    system.run(TIMEOUT * 2)
    assert len(chrome.executed) == 1  # already disconnected: no further operations (TEST 11)


def test_integration_refusal_is_reported_and_not_retried(system: System) -> None:
    chrome = system.sim("c1", "chrome", nb1=SimNotebook())
    chrome.release_behaviour = "refuse"
    system.run(TIMEOUT + 120)
    assert system.state is GuardState.RELEASE_FAILED
    assert "refused" in system.status()["last_operation"]["detail"]
    system.run(TIMEOUT * 2)
    assert len(chrome.executed) == 1


def test_integration_dies_after_receiving_command(system: System) -> None:
    chrome = system.sim("c1", "chrome", nb1=SimNotebook())
    chrome.release_behaviour = "ignore"
    system.run(TIMEOUT + 90)
    assert len(chrome.executed) == 1 and system.state is GuardState.DISCONNECTING
    system.run(200, alive=[])  # extension gone: no result, no heartbeats
    assert system.state is GuardState.RELEASE_FAILED
    assert len(chrome.executed) == 1  # at-most-once: never re-sent


def test_cell_starts_after_command_delivered_extension_refuses(system: System) -> None:
    """The final say belongs to the integration's own re-check right before acting."""
    chrome = system.sim("c1", "chrome", nb1=SimNotebook())
    system.run(TIMEOUT + 60)
    assert system.state in (GuardState.WARNING, GuardState.DISCONNECTING)
    chrome.notebooks["nb1"].execution = "busy"  # cell started at the last moment
    chrome.notebooks["nb1"].running_cells = 1
    system.run(60)
    assert all(n.runtime == "connected" for n in chrome.notebooks.values())
    assert system.state in (GuardState.EXECUTING, GuardState.RELEASE_FAILED)


def test_both_integrations_released_together(system: System) -> None:
    chrome = system.sim("c1", "chrome", nb1=SimNotebook())
    vscode = system.sim("v1", "vscode", nb2=SimNotebook())
    system.run(TIMEOUT + 120)
    assert system.state is GuardState.DISCONNECTED
    assert len(chrome.executed) == len(vscode.executed) == 1


def test_one_of_two_releases_fails(system: System) -> None:
    chrome = system.sim("c1", "chrome", nb1=SimNotebook())
    vscode = system.sim("v1", "vscode", nb2=SimNotebook())
    vscode.release_behaviour = "refuse"
    system.run(TIMEOUT + 120)
    assert system.state is GuardState.RELEASE_FAILED
    assert chrome.notebooks["nb1"].runtime == "disconnected"  # reported honestly as a failed operation


def test_executing_cell_blocks_over_http(system: System) -> None:
    vscode = system.sim("v1", "vscode", nb1=SimNotebook(execution="busy", running_cells=1))
    system.run(TIMEOUT * 3)
    assert system.state is GuardState.EXECUTING and vscode.executed == []


def test_vscode_closes_while_busy_then_returns(system: System) -> None:
    chrome = system.sim("c1", "chrome", nb1=SimNotebook())
    vscode = system.sim("v1", "vscode", nb2=SimNotebook(execution="busy", running_cells=1))
    vscode.goodbye()
    system.run(TIMEOUT * 2, alive=[chrome])
    assert system.state is GuardState.UNKNOWN and chrome.executed == []
    vscode.notebooks["nb2"] = SimNotebook()  # reopened: kernel idle
    vscode.heartbeat()
    system.run(TIMEOUT + 120)
    assert system.state is GuardState.DISCONNECTED


def test_watchdog_restart_mid_warning(tmp_path: Path) -> None:
    clock = FakeClock()
    first = System(tmp_path, clock)
    chrome = first.sim("c1", "chrome", nb1=SimNotebook())
    first.run(TIMEOUT + 10)
    assert first.state is GuardState.WARNING
    first.stop()  # crash/exit during the warning

    second = System(tmp_path, clock)
    try:
        chrome.service = second.service
        second.sims.append(chrome)
        chrome.heartbeat()
        second.run(5 * 60)
        assert second.state is GuardState.IDLE_COUNTDOWN  # idle clock restarted, no release
        assert chrome.executed == []
        second.run(TIMEOUT)
        assert chrome.executed and second.state is GuardState.DISCONNECTED
    finally:
        second.stop()


def test_stale_command_result_after_restart_is_rejected(tmp_path: Path) -> None:
    clock = FakeClock()
    first = System(tmp_path, clock)
    chrome = first.sim("c1", "chrome", nb1=SimNotebook())
    chrome.release_behaviour = "ignore"
    first.run(TIMEOUT + 90)
    [cmd] = chrome.executed
    first.stop()
    second = System(tmp_path, clock)
    try:
        chrome.service = second.service
        status, _ = chrome._post("/v1/command_result", {"command_id": cmd["id"], "nonce": cmd["nonce"], "ok": True})
        assert status == 409
    finally:
        second.stop()


def test_notify_only_never_sends_commands(tmp_path: Path) -> None:
    s = System(tmp_path, FakeClock(), auto=False)
    try:
        chrome = s.sim("c1", "chrome", nb1=SimNotebook())
        s.run(TIMEOUT * 3)
        assert s.state is GuardState.IDLE_NOTIFIED and chrome.executed == []
        titles = [t for _k, t, _m, _l in s.service.notifier.history]
        assert titles.count("Colab runtime idle") == 1
    finally:
        s.stop()
