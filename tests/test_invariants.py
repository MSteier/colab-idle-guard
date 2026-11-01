"""Randomised simulation of the watchdog core with safety checks at each release.

A seeded random sequence of events (activity, cell runs, integration crashes,
reconnects, goodbyes, runtime changes, pause/resume, cancel, configuration
changes and adapter outcomes) runs over many simulated hours. Each time a
release starts, these properties are checked:

  P1  no notebook is busy;
  P2  no notebook is stale or unknown;
  P3  each target was reported connected, recently, by a live integration;
  P4  automatic releases only with automatic release and consent enabled,
      monitoring not paused, and idle time at or above the timeout;
  P5  at most one release operation at a time;
  P6  after a failed release, no automatic retry until a new idle episode.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from colab_idle_guard.runtime_adapters.base import ReleaseState
from colab_idle_guard.watchdog.execution_monitor import RuntimeConn

from conftest import BUSY, CONNECTED, DISCONNECTED, IDLE, Harness, MockAdapter

SEEDS = range(60)
STEPS = 250

EVENTS = [
    ("wait", 30), ("activity", 8), ("start_cell", 6), ("end_cell", 6), ("crash", 2), ("reconnect", 4),
    ("goodbye", 2), ("disconnect_runtime", 2), ("reconnect_runtime", 3), ("pause", 1), ("resume", 2),
    ("cancel", 2), ("toggle_auto", 1), ("adapter_ok", 4), ("adapter_fail", 2), ("report_released", 4),
    ("manual", 1), ("ack", 1),
]


class PropertyViolation(AssertionError):
    pass


class CheckingAdapter(MockAdapter):
    def __init__(self, sim: "Simulation") -> None:
        super().__init__()
        self.sim = sim

    def begin_release(self, target):  # noqa: ANN001 - base-class signature
        # Violations are recorded, not raised: the runtime manager deliberately
        # turns adapter exceptions into failed releases, which would hide them.
        try:
            self.sim.check_release(target)
        except PropertyViolation as exc:
            self.sim.violations.append(str(exc))
        return super().begin_release(target)


class Simulation:
    def __init__(self, h: Harness, rng: random.Random) -> None:
        self.h = h
        self.rng = rng
        self.manual_pending = False
        self.releases = 0
        self.violations: list[str] = []
        self.seen_failures: set[str] = set()
        self.last_failure_ref: float | None = None

    def independent_idle(self) -> tuple[float, float, float]:
        """Idle reference, idle seconds and timeout computed from the raw monitors
        and configuration, not from the controller under test."""
        h = self.h
        cfg = h.config.snapshot()
        ref = h.activity.last_activity
        last_end = h.execution.assess(cfg["telemetry_fresh_seconds"]).last_execution_end
        if cfg["idle_timer_starts_after_execution"] and last_end is not None:
            ref = max(ref, last_end)
        return ref, h.clock() - ref, cfg["idle_timeout_minutes"] * 60.0

    def check_release(self, target) -> None:  # noqa: ANN001
        h = self.h
        cfg = h.config.snapshot()
        a = h.execution.assess(cfg["telemetry_fresh_seconds"])
        if a.busy:
            raise PropertyViolation(f"P1: release while busy: {a.busy}")
        if a.unknown:
            raise PropertyViolation(f"P2: release while unknown: {[r for _, r in a.unknown]}")
        entry = next((e for e in h.execution.entries()
                      if e.instance_id == target.instance_id and e.notebook_id == target.notebook_id), None)
        if entry is None or entry.stale or entry.runtime is not RuntimeConn.CONNECTED:
            raise PropertyViolation(f"P3: target not connected: {entry}")
        if h.clock() - entry.reported_at > cfg["telemetry_fresh_seconds"]:
            raise PropertyViolation("P3: target telemetry not fresh")
        idle_ref, idle_seconds, timeout = self.independent_idle()
        if not self.manual_pending:
            if not h.config.auto_release_allowed:
                raise PropertyViolation("P4: automatic release while disabled")
            if h.controller.status().paused:
                raise PropertyViolation("P4: automatic release while paused")
            if idle_seconds < timeout:
                raise PropertyViolation(f"P4: idle {idle_seconds} below timeout {timeout}")
            if self.last_failure_ref is not None and idle_ref <= self.last_failure_ref:
                raise PropertyViolation("P6: automatic retry after failure in the same idle episode")
        op = h.controller._operation
        if op is not None and not op.done:
            raise PropertyViolation("P5: second release while one is in progress")
        self.releases += 1

    def step(self) -> None:
        h, rng = self.h, self.rng
        action = rng.choices([e for e, _ in EVENTS], weights=[w for _, w in EVENTS])[0]
        live = list(h.live)
        if action == "activity" and live:
            h.interact(rng.choice(live), rng.choice(["click", "key", "scroll", "edit"]))
        elif action == "start_cell" and live:
            inst = rng.choice(live)
            nb = rng.choice(list(h.live[inst].notebooks) or ["nbX"])
            h.set_state(inst, **{nb: (BUSY, CONNECTED, rng.randint(1, 3))})
        elif action == "end_cell" and live:
            inst = rng.choice(live)
            busy = [k for k, v in h.live[inst].notebooks.items() if v[0] is BUSY]
            if busy:
                h.set_state(inst, **{rng.choice(busy): (IDLE, CONNECTED)})
        elif action == "crash" and live:
            h.crash(rng.choice(live))
        elif action == "goodbye" and live:
            h.close(rng.choice(live))
        elif action == "reconnect":
            kind = rng.choice(["chrome", "vscode"])
            inst = rng.choice([f"{kind}-a", f"{kind}-b"])
            if inst not in h.live:
                h.connect(inst, kind, **{f"{kind}-nb{rng.randint(1, 2)}": (rng.choice([IDLE, IDLE, BUSY]), CONNECTED)})
        elif action == "disconnect_runtime" and live:
            inst = rng.choice(live)
            for nb in list(h.live[inst].notebooks):
                h.set_state(inst, **{nb: (IDLE, DISCONNECTED)})
        elif action == "reconnect_runtime" and live:
            inst = rng.choice(live)
            for nb, (e, _r, c) in list(h.live[inst].notebooks.items()):
                h.set_state(inst, **{nb: (e, CONNECTED, c)})
        elif action == "pause":
            h.controller.pause()
        elif action == "resume":
            h.controller.resume()
        elif action == "cancel":
            h.controller.cancel_pending()
        elif action == "toggle_auto":
            on = rng.random() < 0.7
            h.config.update({"automatic_disconnect_enabled": on, "release_consent_acknowledged": on})
        elif action in ("adapter_ok", "adapter_fail"):
            h.adapter.finish(ok=action == "adapter_ok", detail="sim")
        elif action == "report_released":
            for t, s in zip(h.adapter.calls, h.adapter.statuses.values()):
                if s.state is ReleaseState.SUCCEEDED and t.instance_id in h.live:
                    h.set_state(t.instance_id, **{t.notebook_id: (IDLE, DISCONNECTED)})
        elif action == "manual":
            self.manual_pending = True
            try:
                h.controller.request_manual_release()
            finally:
                self.manual_pending = False
        elif action == "ack":
            h.controller.acknowledge_unknown_state()
        h.run(rng.choice([1, 5, 15, 60, 300, 900]), step=1 if rng.random() < 0.3 else 5)
        op = h.controller._last_operation
        if op is not None and op.phase.value == "failed" and op.id not in self.seen_failures:
            # Record each failed operation once, with the idle episode it failed in.
            self.seen_failures.add(op.id)
            ref, _, _ = self.independent_idle()
            self.last_failure_ref = ref


def run_simulation(root: Path, seed: int) -> Simulation:
    rng = random.Random(seed)
    h = Harness(root, idle_timeout_minutes=rng.choice([1, 5, 30]), warning_countdown_seconds=rng.choice([10, 60]))
    sim = Simulation(h, rng)
    adapter = CheckingAdapter(sim)
    h.adapter = adapter
    h.runtime.register("chrome", adapter)
    h.runtime.register("vscode", adapter)
    h.connect("chrome-a", "chrome", **{"chrome-nb1": (IDLE, CONNECTED)})
    for _ in range(STEPS):
        sim.step()
    return sim


@pytest.mark.parametrize("seed", SEEDS)
def test_release_properties_under_random_events(tmp_path: Path, seed: int) -> None:
    sim = run_simulation(tmp_path, seed)
    assert sim.violations == []


def test_simulation_covers_many_releases(tmp_path: Path) -> None:
    total = sum(run_simulation(tmp_path / f"s{seed}", seed).releases for seed in SEEDS)
    assert total >= 50, f"only {total} releases exercised"
