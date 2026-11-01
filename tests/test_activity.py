"""Activity tracking tests, including spec TEST 1-3."""

from __future__ import annotations

from colab_idle_guard.watchdog.activity_monitor import ActivityMonitor
from colab_idle_guard.watchdog.foreground import ForegroundTracker
from colab_idle_guard.watchdog.shutdown_controller import GuardState

from conftest import CONNECTED, IDLE, FakeClock, Harness


def test_01_chrome_colab_activity_resets_timer(harness: Harness) -> None:
    h = harness
    h.connect("c1", "chrome", nb1=(IDLE, CONNECTED))
    h.run(600)
    assert h.controller.status().idle_seconds >= 600
    h.interact("c1", "click")
    h.tick()
    assert h.controller.status().idle_seconds < 1
    assert h.state is GuardState.ACTIVE


def test_02_vscode_colab_activity_resets_timer(harness: Harness) -> None:
    h = harness
    h.connect("v1", "vscode", nb1=(IDLE, CONNECTED))
    h.run(900)
    h.interact("v1", "edit")
    h.tick()
    assert h.controller.status().idle_seconds < 1


def test_03_activity_in_other_windows_apps_does_not_reset(harness: Harness) -> None:
    """Other applications produce no Colab events; the foreground check also
    rejects events that arrive while Chrome / VS Code is not in front."""
    h = harness
    samples = {"name": "notepad.exe"}
    tracker = ForegroundTracker(h.clock, sampler=lambda: samples["name"])
    h.activity.corroborator = tracker.was_foreground
    h.connect("c1", "chrome", nb1=(IDLE, CONNECTED))
    for _ in range(600):
        h.clock.advance(1)
        tracker.sample()
        if h.clock.t % 15 == 0:
            h.beat("c1")
        # A mouse passing over a background Chrome window still fires DOM events:
        result = h.activity.record("chrome", [("pointer", 0.0)])
        assert result.accepted == 0 and result.reasons == ("app_not_foreground",)
        h.tick()
    assert h.controller.status().idle_seconds >= 600

    samples["name"] = "chrome.exe"
    tracker.sample()
    assert h.activity.record("chrome", [("click", 0.0)]).accepted == 1
    h.tick()
    assert h.controller.status().idle_seconds < 1


def test_unknown_kind_and_bad_age_rejected() -> None:
    clock = FakeClock()
    monitor = ActivityMonitor(clock)
    start = monitor.last_activity
    clock.advance(100)
    result = monitor.record("chrome", [("keystroke_text", 0.0), ("click", -1.0), ("click", 9999.0)])
    assert result.accepted == 0 and result.rejected == 3
    assert monitor.last_activity == start


def test_batched_event_age_is_respected() -> None:
    clock = FakeClock()
    monitor = ActivityMonitor(clock)
    clock.advance(100)
    monitor.record("chrome", [("scroll", 30.0), ("click", 10.0)])
    assert monitor.idle_seconds() == 10.0


def test_activity_never_moves_backwards() -> None:
    clock = FakeClock()
    monitor = ActivityMonitor(clock)
    clock.advance(100)
    monitor.record("vscode", [("edit", 0.0)])
    monitor.record("vscode", [("edit", 50.0)])
    assert monitor.idle_seconds() == 0.0


def test_unmonitored_integration_ignored() -> None:
    clock = FakeClock()
    monitor = ActivityMonitor(clock)
    monitor.monitored_kinds = {"vscode"}
    clock.advance(100)
    assert monitor.record("chrome", [("click", 0.0)]).accepted == 0
    assert monitor.idle_seconds() == 100


def test_corroboration_unavailable_accepts_activity() -> None:
    """With no foreground samples the event is accepted (fail towards keeping the runtime)."""
    clock = FakeClock()
    tracker = ForegroundTracker(clock, sampler=lambda: None)
    monitor = ActivityMonitor(clock, corroborator=tracker.was_foreground)
    clock.advance(100)
    assert monitor.record("chrome", [("click", 0.0)]).accepted == 1


def test_foreground_tracker_tolerance_and_mapping() -> None:
    clock = FakeClock()
    names = iter(["code.exe", "explorer.exe", "explorer.exe", "explorer.exe", "explorer.exe", "explorer.exe"])
    tracker = ForegroundTracker(clock, sampler=lambda: next(names), tolerance=2.5)
    for _ in range(6):
        tracker.sample()
        clock.advance(1)
    t0 = clock.t - 6
    assert tracker.was_foreground("vscode", t0 + 1) is True   # within tolerance of the code.exe sample
    assert tracker.was_foreground("vscode", t0 + 5) is False
    assert tracker.was_foreground("chrome", t0) is False
    assert tracker.was_foreground("vscode", t0 + 100) is None  # no nearby sample


def test_real_foreground_query_does_not_crash() -> None:
    from colab_idle_guard.watchdog.foreground import foreground_process_name

    name = foreground_process_name()
    assert name is None or name.endswith(".exe")
