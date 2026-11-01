"""Execution-state tracking and stale-state recovery."""

from __future__ import annotations

from colab_idle_guard.watchdog.execution_monitor import ExecutionMonitor, NotebookReport, RuntimeConn

from conftest import BUSY, CONNECTED, DISCONNECTED, IDLE, UNKNOWN, FakeClock

FRESH = 45
RT_UNKNOWN = RuntimeConn.UNKNOWN


def nb(notebook_id: str, execution=IDLE, runtime=CONNECTED, cells: int | None = None) -> NotebookReport:
    return NotebookReport(notebook_id, execution, (1 if execution is BUSY else 0) if cells is None else cells, runtime)


def monitor() -> tuple[FakeClock, ExecutionMonitor]:
    clock = FakeClock()
    return clock, ExecutionMonitor(clock)


def test_no_reports_is_unknown_not_idle() -> None:
    _, m = monitor()
    a = m.assess(FRESH)
    assert a.execution is UNKNOWN and a.runtime is RT_UNKNOWN


def test_fresh_idle_reports_are_idle() -> None:
    _, m = monitor()
    m.report("c1", "chrome", [nb("a")], True)
    m.report("v1", "vscode", [nb("b")], True)
    a = m.assess(FRESH)
    assert a.execution is IDLE and a.runtime is CONNECTED and len(a.connected_targets) == 2


def test_busy_anywhere_wins_and_counts_cells() -> None:
    _, m = monitor()
    m.report("c1", "chrome", [nb("a"), nb("b", BUSY, cells=3)], True)
    m.report("v1", "vscode", [nb("c", BUSY, cells=2)], True)
    a = m.assess(FRESH)
    assert a.execution is BUSY and a.running_cells == 5
    assert a.by_integration == {"chrome": BUSY, "vscode": BUSY}


def test_contradictory_report_resolves_to_busy() -> None:
    _, m = monitor()
    m.report("c1", "chrome", [nb("a", IDLE, cells=2)], True)
    assert m.assess(FRESH).execution is BUSY


def test_old_idle_report_becomes_unknown() -> None:
    clock, m = monitor()
    m.report("c1", "chrome", [nb("a")], True)
    clock.advance(FRESH + 1)
    a = m.assess(FRESH)
    assert a.execution is UNKNOWN and a.runtime is RT_UNKNOWN
    assert a.unknown[0][1] == "telemetry_not_fresh"


def test_old_busy_report_stays_busy() -> None:
    clock, m = monitor()
    m.report("c1", "chrome", [nb("a", BUSY)], True)
    clock.advance(10 * 60)
    assert m.assess(FRESH).execution is BUSY


def test_lost_integration_marks_everything_stale() -> None:
    clock, m = monitor()
    m.report("c1", "chrome", [nb("a"), nb("b", BUSY)], True)
    assert m.mark_lost("c1") == 2
    clock.advance(24 * 3600)  # time alone never clears stale state
    a = m.assess(FRESH)
    assert a.execution is UNKNOWN and not a.connected_targets
    assert {reason for _, reason in a.unknown} == {"integration_lost"}
    assert a.oldest_stale_since is not None


def test_goodbye_drops_idle_keeps_busy_as_stale() -> None:
    _, m = monitor()
    m.report("v1", "vscode", [nb("a"), nb("b", BUSY)], True)
    m.goodbye("v1", "vscode", None)
    entries = m.entries()
    assert len(entries) == 1 and entries[0].notebook_id == "b" and entries[0].stale
    assert entries[0].last_known_execution is BUSY


def test_goodbye_with_final_idle_snapshot_is_clean() -> None:
    _, m = monitor()
    m.report("v1", "vscode", [nb("a", BUSY)], True)
    m.goodbye("v1", "vscode", [nb("a", IDLE)])
    assert m.entries() == []


def test_notebook_closed_while_busy_becomes_stale() -> None:
    _, m = monitor()
    m.report("v1", "vscode", [nb("a", BUSY), nb("b")], True)
    m.report("v1", "vscode", [nb("b")], True)  # "a" no longer in the snapshot
    stale = [e for e in m.entries() if e.stale]
    assert [e.notebook_id for e in stale] == ["a"]
    assert "notebook_closed_while_busy" in stale[0].stale_reason


def test_partial_report_does_not_retire_others() -> None:
    _, m = monitor()
    m.report("c1", "chrome", [nb("a"), nb("b")], True)
    m.report("c1", "chrome", [nb("a", BUSY)], False)
    assert {e.notebook_id for e in m.entries()} == {"a", "b"}


def test_same_instance_reconnect_recovers() -> None:
    _, m = monitor()
    m.report("c1", "chrome", [nb("a", BUSY)], True)
    m.mark_lost("c1")
    m.report("c1", "chrome", [nb("a", IDLE)], True)
    a = m.assess(FRESH)
    assert a.execution is IDLE and not a.unknown


def test_returning_instance_clears_its_stale_idle_notebooks_only() -> None:
    _, m = monitor()
    m.report("v1", "vscode", [nb("a"), nb("b", BUSY)], True)
    m.mark_lost("v1")  # e.g. window reload without a goodbye
    m.report("v1", "vscode", [], True)  # same window back; kernels not attached yet
    entries = m.entries()
    assert [e.notebook_id for e in entries] == ["b"]  # the busy one stays uncertain
    assert entries[0].stale and entries[0].last_known_execution is BUSY


def test_other_instance_does_not_clear_stale_entries() -> None:
    _, m = monitor()
    m.report("v1", "vscode", [nb("a")], True)
    m.mark_lost("v1")
    m.report("v2", "vscode", [], True)
    assert m.entries()[0].stale


def test_new_instance_supersedes_stale_entry_of_same_kind_only() -> None:
    _, m = monitor()
    m.report("v1", "vscode", [nb("a", BUSY)], True)
    m.mark_lost("v1")
    m.report("c9", "chrome", [nb("a")], True)  # different integration kind: no effect
    assert any(e.stale for e in m.entries())
    m.report("v2", "vscode", [nb("a")], True)
    assert not any(e.stale for e in m.entries())


def test_resolve_stale_verification() -> None:
    _, m = monitor()
    m.report("v1", "vscode", [nb("a", BUSY)], True)
    m.mark_lost("v1")
    assert m.resolve_stale("v1", "a", verified_idle=False, source="kernel") is True
    assert m.entries()[0].stale
    assert m.resolve_stale("v1", "a", verified_idle=True, source="kernel") is True
    assert m.entries() == []


def test_acknowledge_stale_only_removes_stale() -> None:
    _, m = monitor()
    m.report("v1", "vscode", [nb("a", BUSY)], True)
    m.mark_lost("v1")
    m.report("c1", "chrome", [nb("b")], True)
    assert m.acknowledge_stale() == 1
    assert [e.notebook_id for e in m.entries()] == ["b"]


def test_execution_timestamps_tracked() -> None:
    clock, m = monitor()
    m.report("c1", "chrome", [nb("a", BUSY)], True)
    start = m.entries()[0].busy_since
    clock.advance(30)
    m.report("c1", "chrome", [nb("a", IDLE)], True)
    entry = m.entries()[0]
    assert start is not None and entry.busy_since is None
    assert entry.last_execution_end == clock.t
    assert m.assess(FRESH).last_execution_end == clock.t


def test_short_busy_blips_do_not_restart_idle_timer() -> None:
    clock, m = monitor()
    m.report("v1", "vscode", [nb("a", BUSY)], True)
    clock.advance(2)  # e.g. a completion request
    m.report("v1", "vscode", [nb("a", IDLE)], True)
    assert m.assess(FRESH).last_execution_end is None
    m.report("v1", "vscode", [nb("a", BUSY)], True)
    clock.advance(10)
    m.report("v1", "vscode", [nb("a", IDLE)], True)
    assert m.assess(FRESH).last_execution_end == clock.t


def test_runtime_disconnected_only_when_all_fresh_agree() -> None:
    clock, m = monitor()
    m.report("c1", "chrome", [nb("a", runtime=DISCONNECTED)], True)
    assert m.assess(FRESH).runtime is DISCONNECTED
    m.report("v1", "vscode", [nb("b", runtime=RT_UNKNOWN)], True)
    assert m.assess(FRESH).runtime is RT_UNKNOWN
