"""Default adapter: never touches the runtime, only explains how to release it."""

from __future__ import annotations

from ..watchdog.execution_monitor import ReleaseTarget
from .base import ReleaseState, ReleaseStatus, RuntimeAdapter

MANUAL_INSTRUCTIONS = {
    "chrome": (
        "In the Colab browser tab choose Runtime > Disconnect and delete runtime. "
        "This deletes variables and files under /content; the notebook itself is kept in Drive."
    ),
    "vscode": (
        "In VS Code run 'Colab: Remove Server' from the Command Palette and pick the server. "
        "This deletes variables and files on the runtime; save the notebook first."
    ),
}


class NotifyOnlyAdapter(RuntimeAdapter):
    name = "notify_only"
    supports_release = False
    destructive = False

    def begin_release(self, target: ReleaseTarget) -> str:
        raise RuntimeError("notify-only adapter cannot release runtimes")

    def check(self, handle: str) -> ReleaseStatus:
        return ReleaseStatus(ReleaseState.FAILED, "notify-only adapter")

    def manual_instructions(self, target: ReleaseTarget) -> str:
        return MANUAL_INSTRUCTIONS.get(target.integration_kind, super().manual_instructions(target))
