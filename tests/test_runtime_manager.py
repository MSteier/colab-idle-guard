"""Runtime manager, adapters and command queue."""

from __future__ import annotations

from colab_idle_guard.runtime_adapters.notify_only import NotifyOnlyAdapter
from colab_idle_guard.watchdog.commands import CommandQueue, CommandStatus
from colab_idle_guard.watchdog.execution_monitor import ExecutionMonitor, NotebookReport, ReleaseTarget
from colab_idle_guard.watchdog.runtime_manager import OperationPhase, RuntimeManager

from conftest import CONNECTED, DISCONNECTED, IDLE, FakeClock, MockAdapter

TARGET = ReleaseTarget("c1", "chrome", "nb1")


def setup() -> tuple[FakeClock, ExecutionMonitor, RuntimeManager, MockAdapter]:
    clock = FakeClock()
    execution = ExecutionMonitor(clock)
    manager = RuntimeManager(clock, execution)
    adapter = MockAdapter()
    manager.register("chrome", adapter)
    execution.report("c1", "chrome", [NotebookReport("nb1", IDLE, 0, CONNECTED)], True)
    return clock, execution, manager, adapter


def test_default_adapter_is_notify_only() -> None:
    clock = FakeClock()
    manager = RuntimeManager(clock, ExecutionMonitor(clock))
    assert isinstance(manager.adapter_for("vscode"), NotifyOnlyAdapter)
    ok, reasons = manager.release_supported((ReleaseTarget("v1", "vscode", "x"),))
    assert not ok and "no supported release mechanism" in reasons[0]
    assert "Colab: Remove Server" in manager.manual_instructions((ReleaseTarget("v1", "vscode", "x"),))[0]


def test_no_targets_not_supported() -> None:
    _, _, manager, _ = setup()
    assert manager.release_supported(()) == (False, ["no connected runtime to release"])


def test_success_requires_fresh_disconnected_report() -> None:
    clock, execution, manager, adapter = setup()
    op = manager.begin((TARGET,), manual=False)
    adapter.finish(ok=True)
    clock.advance(1)
    manager.poll(op)
    assert op.phase is OperationPhase.CONFIRMING
    execution.report("c1", "chrome", [NotebookReport("nb1", IDLE, 0, DISCONNECTED)], True)
    manager.poll(op)
    assert op.phase is OperationPhase.CONFIRMING  # must stay disconnected for a while
    clock.advance(5)
    manager.poll(op)
    assert op.phase is OperationPhase.SUCCEEDED


def test_flapping_disconnected_state_is_not_confirmation() -> None:
    clock, execution, manager, adapter = setup()
    op = manager.begin((TARGET,), manual=False)
    adapter.finish(ok=True)
    clock.advance(1)
    manager.poll(op)
    for runtime in (DISCONNECTED, CONNECTED, DISCONNECTED, CONNECTED):  # Colab reconnecting
        execution.report("c1", "chrome", [NotebookReport("nb1", IDLE, 0, runtime)], True)
        manager.poll(op)
        clock.advance(3)
        manager.poll(op)
    assert op.phase is OperationPhase.CONFIRMING
    clock.advance(60)
    manager.poll(op)
    assert op.phase is OperationPhase.FAILED


def test_disconnected_report_before_adapter_done_does_not_count() -> None:
    clock, execution, manager, adapter = setup()
    op = manager.begin((TARGET,), manual=False)
    execution.report("c1", "chrome", [NotebookReport("nb1", IDLE, 0, DISCONNECTED)], True)
    clock.advance(1)
    adapter.finish(ok=True)
    manager.poll(op)
    assert op.phase is OperationPhase.CONFIRMING  # report predates the adapter result


def test_confirmation_timeout_fails() -> None:
    clock, _, manager, adapter = setup()
    op = manager.begin((TARGET,), manual=False)
    adapter.finish(ok=True)
    manager.poll(op)
    clock.advance(61)
    manager.poll(op)
    assert op.phase is OperationPhase.FAILED and "not confirmed" in op.detail


def test_overall_timeout_fails() -> None:
    clock, _, manager, _ = setup()
    op = manager.begin((TARGET,), manual=False)
    clock.advance(181)
    manager.poll(op)
    assert op.phase is OperationPhase.FAILED and "timed out" in op.detail


def test_cancel_only_before_execution() -> None:
    _, _, manager, adapter = setup()
    op = manager.begin((TARGET,), manual=False)
    adapter.cancellable = False
    assert manager.cancel(op, "test") is False
    adapter.cancellable = True
    assert manager.cancel(op, "test") is True and op.phase is OperationPhase.CANCELLED


def test_adapter_exception_fails_operation() -> None:
    clock = FakeClock()
    manager = RuntimeManager(clock, ExecutionMonitor(clock))
    op = manager.begin((ReleaseTarget("v1", "vscode", "x"),), manual=True)  # notify-only raises
    assert op.phase is OperationPhase.FAILED


# --- command queue -------------------------------------------------------------

def test_commands_delivered_at_most_once() -> None:
    clock = FakeClock()
    q = CommandQueue(clock)
    cmd = q.enqueue("c1", "release_runtime", {"notebook_id": "nb1"})
    assert q.pull("c2") == []
    first = q.pull("c1")
    assert [c["id"] for c in first] == [cmd.id]
    assert q.pull("c1") == []  # never redelivered


def test_command_result_requires_matching_instance_and_nonce() -> None:
    clock = FakeClock()
    q = CommandQueue(clock)
    cmd = q.enqueue("c1", "release_runtime", {})
    q.pull("c1")
    assert q.complete("c2", cmd.id, cmd.nonce, True) is None
    assert q.complete("c1", cmd.id, "wrong", True) is None
    assert q.complete("c1", cmd.id, cmd.nonce, True).status is CommandStatus.SUCCEEDED
    assert q.complete("c1", cmd.id, cmd.nonce, False) is None  # cannot be completed twice


def test_command_expiry_and_cancel() -> None:
    clock = FakeClock()
    q = CommandQueue(clock)
    undelivered = q.enqueue("c1", "x", {}, deliver_within=10)
    clock.advance(11)
    assert q.get(undelivered.id).status is CommandStatus.EXPIRED
    assert q.pull("c1") == []
    delivered = q.enqueue("c1", "x", {}, result_within=20)
    q.pull("c1")
    assert q.cancel(delivered.id) is False  # already delivered: too late to cancel
    clock.advance(21)
    assert q.get(delivered.id).status is CommandStatus.EXPIRED
    queued = q.enqueue("c1", "x", {})
    assert q.cancel(queued.id) is True and q.pull("c1") == []


# --- integration command adapters -----------------------------------------------

def _adapter_setup():
    from colab_idle_guard.runtime_adapters.integration_command import ChromeColabUiAdapter
    from colab_idle_guard.watchdog.integrations import IntegrationRegistry

    clock = FakeClock()
    commands = CommandQueue(clock)
    registry = IntegrationRegistry(clock)
    return clock, commands, registry, ChromeColabUiAdapter(commands, registry)


def test_command_adapter_requires_online_integration() -> None:
    clock, commands, registry, adapter = _adapter_setup()
    assert adapter.available(TARGET)[0] is False
    registry.touch("c1", "chrome")
    assert adapter.available(TARGET) == (True, "")
    registry.close("c1")
    assert adapter.available(TARGET)[0] is False


def test_command_adapter_lifecycle() -> None:
    from colab_idle_guard.runtime_adapters.base import ReleaseState

    clock, commands, registry, adapter = _adapter_setup()
    handle = adapter.begin_release(TARGET)
    assert adapter.check(handle).state is ReleaseState.PENDING
    [wire] = commands.pull("c1")
    assert wire["type"] == "release_runtime" and wire["payload"] == {"notebook_id": "nb1"}
    assert adapter.cancel(handle) is False  # already delivered: cannot be cancelled
    commands.complete("c1", wire["id"], wire["nonce"], ok=True, detail="released")
    status = adapter.check(handle)
    assert status.state is ReleaseState.SUCCEEDED and status.detail == "released"


def test_command_adapter_failure_expiry_and_cancel() -> None:
    from colab_idle_guard.runtime_adapters.base import ReleaseState

    clock, commands, registry, adapter = _adapter_setup()
    h1 = adapter.begin_release(TARGET)
    [wire] = commands.pull("c1")
    commands.complete("c1", wire["id"], wire["nonce"], ok=False, detail="refused: execution is busy")
    assert adapter.check(h1) .state is ReleaseState.FAILED and "busy" in adapter.check(h1).detail
    h2 = adapter.begin_release(TARGET)
    assert adapter.cancel(h2) is True and adapter.check(h2).state is ReleaseState.FAILED
    h3 = adapter.begin_release(TARGET)
    clock.advance(46)  # never collected
    assert adapter.check(h3).state is ReleaseState.FAILED


def test_service_registers_real_adapters(tmp_path) -> None:
    from colab_idle_guard.watchdog.main import WatchdogService
    from colab_idle_guard.watchdog.paths import AppPaths

    service = WatchdogService(paths=AppPaths(tmp_path / "c", tmp_path / "d"), port=0)
    assert service.runtime.adapter_for("chrome").name == "chrome_colab_ui"
    assert service.runtime.adapter_for("vscode").name == "vscode_colab_unassign"
    assert all(service.runtime.adapter_for(k).destructive for k in ("chrome", "vscode"))
