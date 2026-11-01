"""Shutdown state-machine tests, including spec TEST 4-11."""

from __future__ import annotations

from colab_idle_guard.runtime_adapters.notify_only import NotifyOnlyAdapter
from colab_idle_guard.watchdog.shutdown_controller import GuardState

from conftest import BUSY, CONNECTED, DISCONNECTED, IDLE, UNKNOWN as UNKNOWN_EXEC, Harness


def _idle_session(h: Harness) -> None:
    h.connect("c1", "chrome", nb1=(IDLE, CONNECTED))
    h.interact("c1")
    h.tick()


def _reach_warning(h: Harness) -> None:
    _idle_session(h)
    h.run(h.timeout - 5)
    assert h.state is not GuardState.WARNING
    h.run_until(GuardState.WARNING, limit=10)


# --- spec TEST 6 ---------------------------------------------------------------

def test_06_both_inactive_for_timeout_shows_warning(harness: Harness) -> None:
    h = harness
    h.connect("c1", "chrome", nb1=(IDLE, CONNECTED))
    h.connect("v1", "vscode", nb2=(IDLE, CONNECTED))
    h.interact("c1")
    h.run(h.timeout - 5)
    assert h.state is GuardState.IDLE_COUNTDOWN
    h.run_until(GuardState.WARNING, limit=10)
    assert any("will be released" in title for title, _, _ in h.backend.shown)
    status = h.controller.status()
    assert 0 < status.warning_remaining_seconds <= 60
    assert h.adapter.calls == []  # nothing happens before the countdown ends


def test_full_release_flow_requires_confirmation(harness: Harness) -> None:
    h = harness
    _reach_warning(h)
    h.run(59)
    assert h.state is GuardState.WARNING and h.adapter.calls == []
    h.run_until(GuardState.DISCONNECTING, limit=3)
    assert len(h.adapter.calls) == 1
    h.adapter.finish(ok=True)
    h.run(3)
    # The adapter said "done", but no integration has confirmed the runtime is gone.
    assert h.state is GuardState.DISCONNECTING
    h.set_state("c1", nb1=(IDLE, DISCONNECTED))
    h.run(1)
    assert h.state is GuardState.DISCONNECTING  # disconnected must hold for a few seconds
    h.run(6)
    assert h.state is GuardState.DISCONNECTED
    assert h.controller.status().last_operation["phase"] == "succeeded"


def test_unconfirmed_release_fails_and_is_not_retried(harness: Harness) -> None:
    h = harness
    _reach_warning(h)
    h.run_until(GuardState.DISCONNECTING, limit=65)
    h.adapter.finish(ok=True)
    h.run(70)  # runtime keeps being reported as connected
    assert h.state is GuardState.RELEASE_FAILED
    assert any("failed" in title for title, _, _ in h.backend.shown)
    h.run(h.timeout * 2)
    assert len(h.adapter.calls) == 1  # no blind retry


def test_adapter_failure_reported(harness: Harness) -> None:
    h = harness
    _reach_warning(h)
    h.run_until(GuardState.DISCONNECTING, limit=65)
    h.adapter.finish(ok=False, detail="menu item not found")
    h.run(1)
    assert h.state is GuardState.RELEASE_FAILED
    assert h.controller.status().last_operation["detail"] == "menu item not found"


# --- spec TEST 7 / 8 -----------------------------------------------------------

def test_07_running_cell_prohibits_shutdown(harness: Harness) -> None:
    h = harness
    h.connect("c1", "chrome", nb1=(BUSY, CONNECTED, 1))
    h.run(h.timeout * 3)
    assert h.state is GuardState.EXECUTING
    assert h.adapter.calls == []
    assert not any("will be released" in t for t, _, _ in h.backend.shown)
    assert h.controller.request_manual_release()[0] is False


def test_idle_timer_restarts_after_execution_finishes(harness: Harness) -> None:
    h = harness
    h.connect("c1", "chrome", nb1=(BUSY, CONNECTED, 1))
    h.run(h.timeout * 2)  # a long training run with nobody watching
    h.set_state("c1", nb1=(IDLE, CONNECTED))
    h.run(60)
    # Without the restart rule this would already be in WARNING.
    assert h.state is GuardState.IDLE_COUNTDOWN
    h.run(h.timeout - 65)
    assert h.state is not GuardState.WARNING
    h.run_until(GuardState.WARNING, limit=15)


def test_08_cell_starts_during_warning_cancels(harness: Harness) -> None:
    h = harness
    _reach_warning(h)
    h.run(20)
    h.set_state("c1", nb1=(BUSY, CONNECTED, 1))
    h.tick()
    assert h.state is GuardState.EXECUTING
    h.run(120)
    assert h.adapter.calls == []
    assert any("cancelled" in t for t, _, _ in h.backend.shown)


def test_busy_in_other_integration_blocks(harness: Harness) -> None:
    h = harness
    h.connect("c1", "chrome", nb1=(IDLE, CONNECTED))
    h.connect("v1", "vscode", nb2=(BUSY, CONNECTED, 2))
    h.run(h.timeout * 2)
    assert h.state is GuardState.EXECUTING
    assert h.controller.status().running_cells == 2
    assert h.adapter.calls == []


# --- spec TEST 9 ---------------------------------------------------------------

def test_09_interaction_during_warning_cancels_and_resets(harness: Harness) -> None:
    h = harness
    _reach_warning(h)
    h.run(30)
    h.interact("c1", "key")
    h.tick()
    assert h.state is GuardState.ACTIVE
    assert h.controller.status().idle_seconds < 2
    h.run(h.timeout - 10)
    assert h.adapter.calls == []
    assert h.state is not GuardState.WARNING


def test_user_cancel_resets_timer(harness: Harness) -> None:
    h = harness
    _reach_warning(h)
    assert h.controller.cancel_pending() is True
    h.tick()
    assert h.state is GuardState.ACTIVE
    h.run(h.timeout - 10)
    assert h.adapter.calls == []


# --- spec TEST 4 / 5 -----------------------------------------------------------

def test_04_chrome_closes_vscode_monitoring_continues(harness: Harness) -> None:
    h = harness
    h.connect("c1", "chrome", nb1=(IDLE, CONNECTED))
    h.connect("v1", "vscode", nb2=(IDLE, CONNECTED))
    h.close("c1")
    for _ in range(4):
        h.run(h.timeout / 2)
        h.interact("v1", "edit")  # VS Code activity keeps resetting the shared timer
    h.tick()
    assert h.state is GuardState.ACTIVE
    h.run_until(GuardState.WARNING, limit=h.timeout + 10)
    h.run_until(GuardState.DISCONNECTING, limit=65)
    assert [t.integration_kind for t in h.adapter.calls] == ["vscode"]


def test_05_vscode_closes_chrome_monitoring_continues(harness: Harness) -> None:
    h = harness
    h.connect("c1", "chrome", nb1=(IDLE, CONNECTED))
    h.connect("v1", "vscode", nb2=(IDLE, CONNECTED))
    h.close("v1")
    for _ in range(4):
        h.run(h.timeout / 2)
        h.interact("c1", "scroll")
    h.tick()
    assert h.state is GuardState.ACTIVE
    h.run_until(GuardState.WARNING, limit=h.timeout + 10)
    h.run_until(GuardState.DISCONNECTING, limit=65)
    assert [t.integration_kind for t in h.adapter.calls] == ["chrome"]


def test_activity_in_one_integration_not_overridden_by_other_idle(harness: Harness) -> None:
    h = harness
    h.connect("c1", "chrome", nb1=(IDLE, CONNECTED))
    h.connect("v1", "vscode", nb2=(IDLE, CONNECTED))
    h.run(h.timeout - 30)
    h.interact("c1")  # VS Code has been idle all along; Chrome activity still counts
    h.run(60)
    assert h.state is GuardState.IDLE_COUNTDOWN


# --- spec TEST 10 --------------------------------------------------------------

def test_10_lost_integration_blocks_automatic_release(harness: Harness) -> None:
    h = harness
    h.connect("c1", "chrome", nb1=(IDLE, CONNECTED))
    h.connect("v1", "vscode", nb2=(IDLE, CONNECTED))
    h.crash("c1")  # no goodbye: communication simply stops
    h.run(h.timeout * 2)
    assert h.state is GuardState.UNKNOWN
    assert any("execution_state_unknown" in b for b in h.controller.status().blockers)
    assert h.adapter.calls == []
    assert any("blocked" in t or "could not be verified" in t for t, _, _ in h.backend.shown)


def test_lost_while_busy_never_assumed_idle(harness: Harness) -> None:
    h = harness
    h.connect("v1", "vscode", nb1=(BUSY, CONNECTED, 1))
    h.connect("c1", "chrome", nb2=(IDLE, CONNECTED))
    h.crash("v1")  # VS Code killed while a cell runs
    h.run(h.timeout * 6)  # far beyond stale_verification_max_minutes
    assert h.adapter.calls == []
    assert h.state is GuardState.UNKNOWN
    assert any("could not be verified" in t for t, _, _ in h.backend.shown)


def test_stale_state_cleared_by_fresh_report_from_new_instance(harness: Harness) -> None:
    h = harness
    h.connect("v1", "vscode", nb1=(BUSY, CONNECTED, 1))
    h.crash("v1")
    h.run(120)
    assert h.controller.status().stale_notebooks == 1
    h.connect("v2", "vscode", nb1=(IDLE, CONNECTED))  # VS Code reopened the same notebook
    h.tick()
    assert h.controller.status().stale_notebooks == 0
    h.run_until(GuardState.WARNING, limit=h.timeout + 30)


def test_user_acknowledgement_clears_stale_state(harness: Harness) -> None:
    h = harness
    h.connect("c1", "chrome", nb1=(IDLE, CONNECTED))
    h.connect("v1", "vscode", nb2=(IDLE, CONNECTED))
    h.crash("v1")
    h.run(h.timeout + 60)
    assert h.state is GuardState.UNKNOWN
    assert h.controller.acknowledge_unknown_state() == 1
    h.run_until(GuardState.WARNING, limit=5)


# --- spec TEST 11 --------------------------------------------------------------

def test_11_already_disconnected_runtime_no_operation(harness: Harness) -> None:
    h = harness
    h.connect("c1", "chrome", nb1=(IDLE, DISCONNECTED))
    h.run(h.timeout * 2)
    assert h.state is GuardState.DISCONNECTED
    assert h.adapter.calls == []
    assert h.backend.shown == []


def test_runtime_disconnects_during_warning_cancels(harness: Harness) -> None:
    h = harness
    _reach_warning(h)
    h.set_state("c1", nb1=(IDLE, DISCONNECTED))
    h.run(90)
    assert h.state is GuardState.DISCONNECTED
    assert h.adapter.calls == []


# --- configuration-driven behaviour -------------------------------------------

def test_notify_only_by_default(make_harness) -> None:
    h = make_harness(auto_release=False)
    _idle_session(h)
    h.run(h.timeout * 3)
    assert h.state is GuardState.IDLE_NOTIFIED
    assert h.adapter.calls == []
    idle_notes = [t for t, _, _ in h.backend.shown if t == "Colab runtime idle"]
    assert len(idle_notes) == 1  # notified once per idle episode


def test_auto_release_without_consent_is_not_honoured(make_harness) -> None:
    h = make_harness(auto_release=False, automatic_disconnect_enabled=True)
    assert h.config.auto_release_allowed is False
    _idle_session(h)
    h.run(h.timeout * 2)
    assert h.state is GuardState.IDLE_NOTIFIED
    assert h.adapter.calls == []


def test_unsupported_adapter_falls_back_to_notification(harness: Harness) -> None:
    h = harness
    h.runtime.register("chrome", NotifyOnlyAdapter())
    _idle_session(h)
    h.run(h.timeout + 30)
    assert h.state is GuardState.IDLE_NOTIFIED
    message = next(m for t, m, _ in h.backend.shown if t == "Colab runtime idle")
    assert "Disconnect and delete runtime" in message


def test_pause_blocks_and_resume_restarts_timer(harness: Harness) -> None:
    h = harness
    _idle_session(h)
    h.controller.pause()
    h.run(h.timeout * 2)
    assert h.state is GuardState.PAUSED and h.adapter.calls == []
    h.controller.resume()
    h.run(h.timeout - 10)
    assert h.state is GuardState.IDLE_COUNTDOWN
    h.run_until(GuardState.WARNING, limit=20)


def test_pause_during_warning_cancels(harness: Harness) -> None:
    h = harness
    _reach_warning(h)
    h.controller.pause()
    h.run(120)
    assert h.state is GuardState.PAUSED and h.adapter.calls == []


def test_unmonitored_integration_not_released(make_harness) -> None:
    h = make_harness(monitor_chrome=False)
    h.connect("c1", "chrome", nb1=(IDLE, CONNECTED))
    h.run(h.timeout * 2)
    assert h.adapter.calls == []
    assert h.state is GuardState.MONITORING


def test_custom_timeout_respected(make_harness) -> None:
    h = make_harness(idle_timeout_minutes=5, warning_countdown_seconds=10)
    _idle_session(h)
    h.run(5 * 60 - 3)
    assert h.state is not GuardState.WARNING
    h.run_until(GuardState.WARNING, limit=5)
    h.run_until(GuardState.DISCONNECTING, limit=12)


# --- manual actions ------------------------------------------------------------

def test_manual_release_while_idle(harness: Harness) -> None:
    h = harness
    _idle_session(h)
    h.run(30)
    ok, _ = h.controller.request_manual_release()
    assert ok and h.state is GuardState.DISCONNECTING
    assert len(h.adapter.calls) == 1


def test_manual_release_blocked_when_state_unknown(harness: Harness) -> None:
    h = harness
    h.connect("c1", "chrome", nb1=(IDLE, CONNECTED))
    h.connect("v1", "vscode", nb2=(IDLE, CONNECTED))
    h.crash("v1")
    h.run(120)
    ok, reason = h.controller.request_manual_release()
    assert not ok and "execution_state_unknown" in reason


def test_activity_during_release_cancels_undelivered_command(harness: Harness) -> None:
    h = harness
    _reach_warning(h)
    h.run_until(GuardState.DISCONNECTING, limit=65)
    h.clock.advance(1)
    h.interact("c1")
    h.tick()
    assert h.state is GuardState.MONITORING
    h.tick()
    assert h.state is GuardState.ACTIVE
    assert len(h.adapter.calls) == 1
    assert h.controller.status().last_operation["phase"] == "cancelled"


def test_no_telemetry_at_all_never_releases(harness: Harness) -> None:
    h = harness
    h.run(h.timeout * 3)
    assert h.state is GuardState.MONITORING
    assert h.adapter.calls == []


# --- paths found uncovered in Phase 7 -----------------------------------------------

def test_integration_lost_during_warning_cancels(harness: Harness) -> None:
    h = harness
    h.connect("c1", "chrome", nb1=(IDLE, CONNECTED))
    h.connect("v1", "vscode", nb2=(IDLE, CONNECTED))
    h.run(h.timeout + 5)
    assert h.state is GuardState.WARNING
    h.crash("v1")  # VS Code stops reporting mid-countdown
    h.run(120)
    assert h.adapter.calls == []
    assert h.state is GuardState.UNKNOWN
    assert any("conditions_changed" in m or "Reason" in m for _, m, _ in h.backend.shown)


def test_final_check_fails_when_auto_release_disabled_during_warning(harness: Harness) -> None:
    h = harness
    _reach_warning(h)
    h.config.update({"automatic_disconnect_enabled": False})
    h.run(70)
    assert h.adapter.calls == []
    assert h.state in (GuardState.UNKNOWN, GuardState.IDLE_NOTIFIED)
    assert any("automatic_release_disabled" in m for _, m, _ in h.backend.shown)


def test_user_cancel_while_release_queued(harness: Harness) -> None:
    h = harness
    _reach_warning(h)
    h.run_until(GuardState.DISCONNECTING, limit=65)
    assert h.controller.cancel_pending() is True  # mock adapter: not yet carried out
    assert h.controller.status().last_operation["phase"] == "cancelled"
    h.tick()
    assert h.state is GuardState.ACTIVE


def test_manual_release_refusals(harness: Harness) -> None:
    h = harness
    assert h.controller.request_manual_release() == (False, "no connected runtime is being reported")
    from colab_idle_guard.runtime_adapters.notify_only import NotifyOnlyAdapter

    h.connect("c1", "chrome", nb1=(IDLE, CONNECTED))
    h.runtime.register("chrome", NotifyOnlyAdapter())
    ok, reason = h.controller.request_manual_release()
    assert not ok and "Disconnect and delete runtime" in reason
    h.runtime.register("chrome", h.adapter)
    assert h.controller.request_manual_release()[0] is True
    assert h.controller.request_manual_release() == (False, "a release is already in progress")


def test_failing_status_listener_does_not_break_ticks(harness: Harness) -> None:
    h = harness
    seen = []
    h.controller.add_listener(lambda s: 1 / 0)
    h.controller.add_listener(lambda s: seen.append(s.state))
    h.tick()
    h.tick()
    assert len(seen) == 2


def test_blocked_idle_notifies_once(harness: Harness) -> None:
    h = harness
    h.connect("c1", "chrome", nb1=(IDLE, CONNECTED))
    h.connect("v1", "vscode", nb2=(UNKNOWN_EXEC, CONNECTED))
    h.run(h.timeout * 3)
    blocked = [t for t, _, _ in h.backend.shown if t == "Colab runtime idle, but release is blocked"]
    assert len(blocked) == 1 and h.adapter.calls == []


def test_failed_release_not_retried_after_runtime_flicker(harness: Harness) -> None:
    """Regression (found by the randomised property test): other states used to
    overwrite RELEASE_FAILED, allowing a retry within the same idle episode."""
    h = harness
    _reach_warning(h)
    h.run_until(GuardState.DISCONNECTING, limit=65)
    h.adapter.finish(ok=False, detail="refused")
    h.run(2)
    assert h.state is GuardState.RELEASE_FAILED
    h.set_state("c1", nb1=(IDLE, DISCONNECTED))  # page briefly shows disconnected
    h.run(2)
    h.set_state("c1", nb1=(BUSY, CONNECTED, 1))  # short busy blip (< 10 s)
    h.run(3)
    h.set_state("c1", nb1=(IDLE, CONNECTED))
    h.run(h.timeout)
    assert len(h.adapter.calls) == 1
    assert h.state is GuardState.RELEASE_FAILED
    h.interact("c1")  # new idle episode
    h.run(h.timeout + 70)
    assert len(h.adapter.calls) == 2
