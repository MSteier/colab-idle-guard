"""Watchdog service: wires the components together and runs the control loop."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from .. import __version__
from .activity_monitor import ActivityMonitor
from .api_server import ApiContext, ApiServer
from .commands import CommandQueue
from .configuration import ConfigManager
from .execution_monitor import ExecutionMonitor
from .foreground import ForegroundTracker
from .integrations import IntegrationRegistry
from .logging_manager import log_event, redactor, set_level, setup_logging
from .notification_manager import NotificationManager
from .paths import AppPaths, default_paths
from ..runtime_adapters.integration_command import ChromeColabUiAdapter, VSCodeColabUnassignAdapter
from .runtime_manager import RuntimeManager
from .security import TokenStore
from .shutdown_controller import ShutdownController

log = logging.getLogger("colab_idle_guard.service")


class ServiceStartError(RuntimeError):
    """The service could not start (typically: port in use by another instance)."""


class WatchdogService:
    """Owns all watchdog components. ``start()``/``stop()`` are idempotent."""

    def __init__(
        self,
        paths: AppPaths | None = None,
        clock: Callable[[], float] = time.monotonic,
        port: int | None = None,
        foreground_sampler: Callable[[], str | None] | None = None,
        configure_logging: bool = True,
        console_log: bool = False,
    ) -> None:
        self.paths = paths or default_paths()
        self.paths.ensure()
        self.clock = clock
        self.config = ConfigManager(self.paths.config_file)
        self.config.load()
        if configure_logging:
            setup_logging(self.paths.log_file, self.config.get("log_level"), console=console_log)
        for warning in self.config.load_warnings:
            log_event(log, "config_warning", logging.WARNING, detail=warning)

        self.token_store = TokenStore(self.paths.token_file)
        self._token = self.token_store.load_or_create()
        redactor.add_secret(self._token)
        if not self.token_store.protected:
            log_event(log, "token_storage_unprotected", logging.WARNING)

        if foreground_sampler is None:
            self.foreground = ForegroundTracker(clock)
        else:
            self.foreground = ForegroundTracker(clock, sampler=foreground_sampler)
        self.registry = IntegrationRegistry(clock)
        self.activity = ActivityMonitor(clock, corroborator=self._corroborate)
        self.execution = ExecutionMonitor(clock)
        self.commands = CommandQueue(clock)
        self.runtime = RuntimeManager(clock, self.execution)
        self.runtime.register("chrome", ChromeColabUiAdapter(self.commands, self.registry))
        self.runtime.register("vscode", VSCodeColabUnassignAdapter(self.commands, self.registry))
        self.notifier = NotificationManager(clock, enabled=lambda: self.config.get("notifications_enabled"))
        self.controller = ShutdownController(clock, self.config, self.registry, self.activity,
                                             self.execution, self.runtime, self.notifier)
        self._apply_config(self.config.snapshot())
        self.config.add_listener(self._apply_config)

        ctx = ApiContext(
            token=lambda: self._token,
            config=self.config,
            registry=self.registry,
            activity=self.activity,
            execution=self.execution,
            commands=self.commands,
            status=lambda: self.controller.status().to_dict(),
        )
        self.api = ApiServer(ctx, self.config.get("api_port") if port is None else port)
        self._stop = threading.Event()
        self._loop: threading.Thread | None = None
        self._started = False

    # ---------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._started:
            return
        log_event(log, "app_started", version=__version__, auto_release=self.config.auto_release_allowed,
                  token_protected=self.token_store.protected)
        self.foreground.start()
        try:
            self.api.start()
        except OSError as exc:
            self.foreground.stop()
            log_event(log, "api_server_start_failed", logging.ERROR, error=str(exc))
            raise ServiceStartError(
                f"Cannot listen on 127.0.0.1:{self.config.get('api_port')} - is Colab Idle Guard already running? ({exc})"
            ) from exc
        self._stop.clear()
        self._loop = threading.Thread(target=self._run, name="guard-loop", daemon=True)
        self._loop.start()
        self._started = True

    def stop(self) -> None:
        if not self._started:
            return
        self._stop.set()
        if self._loop is not None:
            self._loop.join(timeout=5)
        self.api.stop()
        self.foreground.stop()
        self._started = False
        log_event(log, "app_stopped")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.controller.tick()
            except Exception:
                # The loop must survive bugs; a failed tick never releases anything.
                log.exception("controller tick failed")
            self._stop.wait(1.0)

    # ---------------------------------------------------------------- helpers

    @property
    def token(self) -> str:
        """The pairing token. Only for explicit user display (tray "Copy pairing token")."""
        return self._token

    def rotate_token(self) -> str:
        self._token = self.token_store.rotate()
        redactor.add_secret(self._token)
        log_event(log, "token_rotated")
        return self._token

    def status(self) -> dict[str, Any]:
        return self.controller.status().to_dict()

    def _corroborate(self, integration_kind: str, at: float) -> bool | None:
        if not self.config.get("require_foreground_corroboration"):
            return None
        return self.foreground.was_foreground(integration_kind, at)

    def _apply_config(self, cfg: dict[str, Any]) -> None:
        set_level(cfg["log_level"])
        self.activity.monitored_kinds = {k for k in ("chrome", "vscode") if cfg[f"monitor_{k}"]}
