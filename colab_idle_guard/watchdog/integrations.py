"""Registry of connected integrations (Chrome extension instances, VS Code windows)."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Callable

INTEGRATION_KINDS = ("chrome", "vscode")


class IntegrationStatus(StrEnum):
    ONLINE = "online"
    LOST = "lost"  # stopped sending heartbeats without saying goodbye
    CLOSED = "closed"  # said goodbye


@dataclass(frozen=True)
class Integration:
    instance_id: str
    kind: str
    version: str
    first_seen: float
    last_seen: float
    status: IntegrationStatus


class IntegrationRegistry:
    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._items: dict[str, Integration] = {}

    def touch(self, instance_id: str, kind: str, version: str = "") -> tuple[Integration, bool]:
        """Record contact. Returns (integration, came_online) where came_online is
        True for a new or previously lost/closed instance."""
        now = self._clock()
        with self._lock:
            existing = self._items.get(instance_id)
            if existing is not None and existing.kind != kind:
                raise ValueError("instance id already registered for a different integration kind")
            came_online = existing is None or existing.status is not IntegrationStatus.ONLINE
            item = Integration(
                instance_id=instance_id,
                kind=kind,
                version=version or (existing.version if existing else ""),
                first_seen=existing.first_seen if existing and not came_online else now,
                last_seen=now,
                status=IntegrationStatus.ONLINE,
            )
            self._items[instance_id] = item
            return item, came_online

    def get(self, instance_id: str) -> Integration | None:
        with self._lock:
            return self._items.get(instance_id)

    def close(self, instance_id: str) -> Integration | None:
        with self._lock:
            item = self._items.get(instance_id)
            if item is None:
                return None
            item = replace(item, status=IntegrationStatus.CLOSED, last_seen=self._clock())
            self._items[instance_id] = item
            return item

    def expire(self, timeout: float) -> list[Integration]:
        """Mark ONLINE integrations silent for longer than ``timeout`` as LOST."""
        now = self._clock()
        lost: list[Integration] = []
        with self._lock:
            for key, item in self._items.items():
                if item.status is IntegrationStatus.ONLINE and now - item.last_seen > timeout:
                    item = replace(item, status=IntegrationStatus.LOST)
                    self._items[key] = item
                    lost.append(item)
        return lost

    def all(self) -> list[Integration]:
        with self._lock:
            return list(self._items.values())

    def online(self, kind: str | None = None) -> list[Integration]:
        return [i for i in self.all() if i.status is IntegrationStatus.ONLINE and (kind is None or i.kind == kind)]
