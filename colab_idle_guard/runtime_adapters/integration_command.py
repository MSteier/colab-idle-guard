"""Adapters that release a runtime by asking the integration that shows it.

The watchdog cannot reach Colab itself (no public API, research §3.2). The
integration that displays the runtime performs the release and reports back
over the at-most-once command channel (watchdog/commands.py). Each integration
re-checks execution state immediately before acting. The runtime manager then
independently confirms the result from later state reports.
"""

from __future__ import annotations

from ..watchdog.commands import CommandQueue, CommandStatus
from ..watchdog.execution_monitor import ReleaseTarget
from ..watchdog.integrations import IntegrationRegistry, IntegrationStatus
from .base import ReleaseState, ReleaseStatus, RuntimeAdapter
from .notify_only import MANUAL_INSTRUCTIONS

COMMAND_TYPE = "release_runtime"


class IntegrationCommandAdapter(RuntimeAdapter):
    supports_release = True
    destructive = True
    integration_kind = ""

    def __init__(self, commands: CommandQueue, registry: IntegrationRegistry,
                 deliver_within: float = 45.0, result_within: float = 120.0) -> None:
        self._commands = commands
        self._registry = registry
        self._deliver_within = deliver_within
        self._result_within = result_within

    def available(self, target: ReleaseTarget) -> tuple[bool, str]:
        integration = self._registry.get(target.instance_id)
        if integration is None or integration.status is not IntegrationStatus.ONLINE:
            return False, f"{self.name}: the {target.integration_kind} integration showing this runtime is not connected"
        return True, ""

    def begin_release(self, target: ReleaseTarget) -> str:
        cmd = self._commands.enqueue(target.instance_id, COMMAND_TYPE, {"notebook_id": target.notebook_id},
                                     deliver_within=self._deliver_within, result_within=self._result_within)
        return cmd.id

    def check(self, handle: str) -> ReleaseStatus:
        cmd = self._commands.get(handle)
        if cmd is None:
            return ReleaseStatus(ReleaseState.FAILED, "release command was lost")
        if cmd.status in (CommandStatus.QUEUED, CommandStatus.DELIVERED):
            return ReleaseStatus(ReleaseState.PENDING)
        if cmd.status is CommandStatus.SUCCEEDED:
            return ReleaseStatus(ReleaseState.SUCCEEDED, cmd.detail)
        return ReleaseStatus(ReleaseState.FAILED, cmd.detail or str(cmd.status))

    def cancel(self, handle: str) -> bool:
        # Possible only before the integration has collected the command.
        return self._commands.cancel(handle)

    def manual_instructions(self, target: ReleaseTarget) -> str:
        return MANUAL_INSTRUCTIONS.get(target.integration_kind, super().manual_instructions(target))


class ChromeColabUiAdapter(IntegrationCommandAdapter):
    """Chrome: the extension uses Colab's own Runtime > Disconnect and delete runtime."""

    name = "chrome_colab_ui"
    integration_kind = "chrome"


class VSCodeColabUnassignAdapter(IntegrationCommandAdapter):
    """VS Code: the extension calls google.colab.runtime.unassign() in the Colab kernel."""

    name = "vscode_colab_unassign"
    integration_kind = "vscode"
