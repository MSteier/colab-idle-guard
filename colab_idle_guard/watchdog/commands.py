"""Pull-based command channel to integrations (ADR-4).

Integrations collect pending commands in their heartbeat response and report
results back. Commands are delivered **at most once** because some of them are
destructive; a command that is delivered but never acknowledged times out as
failed instead of being re-sent.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable


class CommandStatus(StrEnum):
    QUEUED = "queued"
    DELIVERED = "delivered"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


@dataclass
class Command:
    id: str
    nonce: str
    instance_id: str
    type: str
    payload: dict[str, Any]
    created_at: float
    deliver_by: float
    result_by: float
    status: CommandStatus = CommandStatus.QUEUED
    detail: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    finished_at: float | None = None

    @property
    def done(self) -> bool:
        return self.status not in (CommandStatus.QUEUED, CommandStatus.DELIVERED)

    def wire(self) -> dict[str, Any]:
        return {"id": self.id, "nonce": self.nonce, "type": self.type, "payload": self.payload}


class CommandQueue:
    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._commands: dict[str, Command] = {}

    def enqueue(self, instance_id: str, type_: str, payload: dict[str, Any],
                deliver_within: float = 45.0, result_within: float = 90.0) -> Command:
        now = self._clock()
        cmd = Command(
            id=secrets.token_hex(8),
            nonce=secrets.token_urlsafe(16),
            instance_id=instance_id,
            type=type_,
            payload=payload,
            created_at=now,
            deliver_by=now + deliver_within,
            result_by=now + result_within,
        )
        with self._lock:
            self._commands[cmd.id] = cmd
        return cmd

    def pull(self, instance_id: str) -> list[dict[str, Any]]:
        now = self._clock()
        out: list[dict[str, Any]] = []
        with self._lock:
            self._expire_locked(now)
            for cmd in self._commands.values():
                if cmd.instance_id == instance_id and cmd.status is CommandStatus.QUEUED:
                    cmd.status = CommandStatus.DELIVERED
                    out.append(cmd.wire())
        return out

    def complete(self, instance_id: str, command_id: str, nonce: str, ok: bool,
                 detail: str = "", result: dict[str, Any] | None = None) -> Command | None:
        now = self._clock()
        with self._lock:
            cmd = self._commands.get(command_id)
            if cmd is None or cmd.instance_id != instance_id or cmd.nonce != nonce:
                return None
            if cmd.status is not CommandStatus.DELIVERED:
                return None
            cmd.status = CommandStatus.SUCCEEDED if ok else CommandStatus.FAILED
            cmd.detail = detail[:500]
            cmd.result = result or {}
            cmd.finished_at = now
            return cmd

    def cancel(self, command_id: str) -> bool:
        """Cancel a command that has not been delivered yet."""
        with self._lock:
            cmd = self._commands.get(command_id)
            if cmd is None or cmd.status is not CommandStatus.QUEUED:
                return False
            cmd.status = CommandStatus.CANCELLED
            cmd.finished_at = self._clock()
            return True

    def get(self, command_id: str) -> Command | None:
        with self._lock:
            self._expire_locked(self._clock())
            return self._commands.get(command_id)

    def _expire_locked(self, now: float) -> None:
        for cmd in self._commands.values():
            if cmd.status is CommandStatus.QUEUED and now > cmd.deliver_by:
                cmd.status, cmd.detail, cmd.finished_at = CommandStatus.EXPIRED, "integration did not collect the command", now
            elif cmd.status is CommandStatus.DELIVERED and now > cmd.result_by:
                cmd.status, cmd.detail, cmd.finished_at = CommandStatus.EXPIRED, "integration did not report a result", now
        # Bound memory: drop finished commands older than ten minutes.
        for cid in [c.id for c in self._commands.values() if c.done and c.finished_at and now - c.finished_at > 600]:
            del self._commands[cid]
