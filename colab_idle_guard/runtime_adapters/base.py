"""Runtime adapter interface.

An adapter knows how to release ("disconnect") a runtime reached through one
kind of integration. Research (docs/COLAB_RUNTIME_RESEARCH.md) shows every
Colab release mechanism deletes VM state, so adapters declare ``destructive``
and the runtime manager only uses them when the user has opted in.
Adapters for other cloud GPU providers implement the same interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from ..watchdog.execution_monitor import ReleaseTarget


class ReleaseState(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True)
class ReleaseStatus:
    state: ReleaseState
    detail: str = ""


class RuntimeAdapter(ABC):
    #: Short identifier used in logs and the UI.
    name: str = "base"
    #: True when the adapter can actually release a runtime.
    supports_release: bool = False
    #: True when releasing loses VM state (true for every Colab mechanism found).
    destructive: bool = True

    def available(self, target: ReleaseTarget) -> tuple[bool, str]:
        """Whether release can be attempted for ``target`` right now."""
        if not self.supports_release:
            return False, f"{self.name}: no supported release mechanism"
        return True, ""

    @abstractmethod
    def begin_release(self, target: ReleaseTarget) -> str:
        """Start releasing ``target``; returns an opaque handle for :meth:`check`."""

    @abstractmethod
    def check(self, handle: str) -> ReleaseStatus:
        """Report progress of a release started by :meth:`begin_release`."""

    def cancel(self, handle: str) -> bool:
        """Try to abort a release that has not been carried out yet."""
        return False

    def manual_instructions(self, target: ReleaseTarget) -> str:
        return "Release the runtime manually from the Colab interface."
