"""System-tray application: tray icon + Tk windows around the watchdog service.

Threads: Tk runs on the main thread; pystray runs its own message loop on a
worker thread; the watchdog's control loop and HTTP server run on theirs.
Tk is not thread-safe, so everything that touches the UI is posted to a
queue that the Tk thread drains every 100 ms.
"""

from __future__ import annotations

import ctypes
import logging
import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox
from typing import Any, Callable

import pystray

from ..watchdog.diagnostics import export_diagnostics
from ..watchdog.logging_manager import log_event
from ..watchdog.main import ServiceStartError, WatchdogService
from . import autostart
from . import presenter as p
from .icons import state_icon
from .windows import SettingsWindow, StatusWindow, WarningDialog, apply_theme

log = logging.getLogger("colab_idle_guard.desktop")

RELEASE_CONFIRM = (
    "Release the Colab runtime now?\n\n"
    "This disconnects and deletes the runtime: variables, loaded models and files on it "
    "(e.g. /content) are lost. Your notebook file is kept."
)
ACK_CONFIRM = (
    "An integration stopped reporting while a notebook may have been running, so Colab Idle Guard "
    "cannot tell whether a cell is still executing. Release is blocked until this is resolved.\n\n"
    "Reopening the notebook resolves it automatically. Only confirm if you are sure NO cell is still "
    "running in any Colab notebook.\n\nConfirm that nothing is running?"
)


class TrayNotifier:
    """Notification backend that shows Windows notifications through the tray icon."""

    def __init__(self) -> None:
        self.icon: pystray.Icon | None = None

    def show(self, title: str, message: str, level: str) -> None:
        if self.icon is not None and self.icon.visible:
            self.icon.notify(message[:255], title[:63])


class DesktopApp:
    def __init__(self, service: WatchdogService, start_minimized: bool = False) -> None:
        self.service = service
        self.start_minimized = start_minimized
        self.ui_queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.status: dict[str, Any] = service.status()
        self.notifier = TrayNotifier()
        self.icon: pystray.Icon | None = None
        self.root: tk.Tk | None = None
        self.status_window: StatusWindow | None = None
        self.warning: WarningDialog | None = None
        self.settings: SettingsWindow | None = None
        self._last_icon_state = ""
        self._last_menu_refresh = 0.0
        self._exiting = False

    # ------------------------------------------------------------------ lifecycle

    def run(self) -> int:
        _enable_dpi_awareness()
        self.root = tk.Tk()
        self.root.withdraw()
        apply_theme(self.root)
        try:
            self.service.start()
        except ServiceStartError as exc:
            messagebox.showerror("Colab Idle Guard", str(exc))
            self.root.destroy()
            return 3

        self.status_window = StatusWindow(self.root, {
            "toggle_pause": self.toggle_pause, "cancel": self.cancel_pending, "release_now": self.release_now,
            "acknowledge": self.acknowledge_unknown, "settings": self.open_settings,
        })
        self.status_window.hide()
        self.service.notifier.add_backend(self.notifier)
        self.service.controller.add_listener(lambda status: self.post("status", status.to_dict()))
        self.service.config.add_listener(lambda cfg: self.post("call", lambda: self._sync_autostart(cfg)))
        self._sync_autostart(self.service.config.snapshot())

        self.icon = pystray.Icon("ColabIdleGuard", state_icon(self.status["state"]), p.tooltip(self.status), self._menu())
        self.notifier.icon = self.icon
        threading.Thread(target=self.icon.run, name="tray", daemon=True).start()

        if not self.start_minimized:
            self.show_status()
        self.root.after(100, self._pump)
        log_event(log, "desktop_started", minimized=self.start_minimized)
        try:
            self.root.mainloop()
        finally:
            self._shutdown()
        return 0

    def post(self, kind: str, payload: Any) -> None:
        """Thread-safe: schedule work on the Tk thread."""
        self.ui_queue.put((kind, payload))

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                try:
                    if kind == "status":
                        self._apply_status(payload)
                    elif kind == "call":
                        payload()
                except Exception:
                    log.exception("UI update failed")
        except queue.Empty:
            pass
        if not self._exiting and self.root is not None:
            self.root.after(100, self._pump)

    def _shutdown(self) -> None:
        self._exiting = True
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass
        self.service.stop()

    # ------------------------------------------------------------------ status

    def _apply_status(self, status: dict[str, Any]) -> None:
        self.status = status
        state = status["state"]
        if self.status_window and self.status_window.visible:
            self.status_window.update(status)
        if state == "warning":
            if self.warning is None:
                self.warning = WarningDialog(self._tk(), self.cancel_pending)
            self.warning.update(status)
        elif self.warning is not None:
            self.warning.close()
            self.warning = None
        if self.icon is not None:
            if state != self._last_icon_state:
                self.icon.icon = state_icon(state)
                self._last_icon_state = state
            self.icon.title = p.tooltip(status)
            now = time.monotonic()
            if now - self._last_menu_refresh >= 1.0:
                self._last_menu_refresh = now
                self.icon.update_menu()

    # ------------------------------------------------------------------ menu

    def _menu(self) -> pystray.Menu:
        flags = lambda: p.menu_flags(self.status)  # noqa: E731
        on_ui = self._on_ui
        return pystray.Menu(
            pystray.MenuItem(lambda _i: p.headline(self.status), on_ui(self.show_status), default=True),
            pystray.MenuItem(lambda _i: p.runtime_line(self.status) + " · " + p.execution_line(self.status), None, enabled=False),
            pystray.MenuItem(lambda _i: p.integrations_line(self.status), None, enabled=False),
            pystray.MenuItem(lambda _i: p.release_mode_line(self.status), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open status window", on_ui(self.show_status)),
            pystray.MenuItem(lambda _i: "Resume monitoring" if self.status["paused"] else "Pause monitoring",
                             on_ui(self.toggle_pause)),
            pystray.MenuItem("Cancel pending release", on_ui(self.cancel_pending), enabled=lambda _i: flags().cancel),
            pystray.MenuItem("Release runtime now…", on_ui(self.release_now), enabled=lambda _i: flags().release_now),
            pystray.MenuItem("Nothing is running - clear unknown state…", on_ui(self.acknowledge_unknown),
                             visible=lambda _i: flags().acknowledge_unknown),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Copy pairing token", on_ui(self.copy_token)),
            pystray.MenuItem("Settings…", on_ui(self.open_settings)),
            pystray.MenuItem("Open logs folder", on_ui(self.open_logs)),
            pystray.MenuItem("Export diagnostic report…", on_ui(self.export_report)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", on_ui(self.exit)),
        )

    def _on_ui(self, fn: Callable[[], None]) -> Callable[..., None]:
        """Wrap a menu action (called on the pystray thread) to run on the Tk thread."""
        return lambda *_args: self.post("call", fn)

    # ------------------------------------------------------------------ actions (Tk thread)

    def show_status(self) -> None:
        assert self.status_window is not None
        self.status_window.update(self.status)
        self.status_window.show()

    def toggle_pause(self) -> None:
        if self.status["paused"]:
            self.service.controller.resume()
        else:
            self.service.controller.pause()
        self._refresh_now()

    def cancel_pending(self) -> None:
        if self.service.controller.cancel_pending():
            log_event(log, "release_cancelled_by_user")
        self._refresh_now()

    def release_now(self) -> None:
        if not messagebox.askyesno("Release runtime", RELEASE_CONFIRM, icon="warning", parent=self._parent()):
            return
        ok, message = self.service.controller.request_manual_release()
        (messagebox.showinfo if ok else messagebox.showwarning)("Release runtime", message, parent=self._parent())
        self._refresh_now()

    def acknowledge_unknown(self) -> None:
        if messagebox.askyesno("Clear unknown execution state", ACK_CONFIRM, icon="warning", parent=self._parent()):
            cleared = self.service.controller.acknowledge_unknown_state()
            messagebox.showinfo("Colab Idle Guard", f"Cleared {cleared} notebook(s) with unknown state.", parent=self._parent())
            self._refresh_now()

    def copy_token(self) -> None:
        assert self.root is not None
        self.root.clipboard_clear()
        self.root.clipboard_append(self.service.token)
        self.root.update()
        self.service.notifier.notify("token_copied", "Pairing token copied",
                                     "Paste it into the Colab Idle Guard Chrome extension settings.", force=True)

    def open_settings(self) -> None:
        if self.settings is None:
            self.settings = SettingsWindow(self._tk(), self.service.config, on_close=self._settings_closed)
        self.settings.show()

    def _settings_closed(self) -> None:
        self.settings = None
        self._refresh_now()

    def open_logs(self) -> None:
        os.startfile(self.service.paths.log_dir)  # noqa: S606 - opens Explorer on our own folder

    def export_report(self) -> None:
        default = f"colab-idle-guard-diagnostics-{datetime.now():%Y%m%d-%H%M%S}.zip"
        target = filedialog.asksaveasfilename(
            parent=self._parent(), title="Export diagnostic report", defaultextension=".zip",
            initialfile=default, initialdir=str(Path.home() / "Desktop"), filetypes=[("ZIP archive", "*.zip")])
        if not target:
            return
        path = export_diagnostics(self.service, Path(target))
        messagebox.showinfo("Colab Idle Guard",
                            f"Diagnostic report saved to:\n{path}\n\nIt contains no tokens or notebook content.",
                            parent=self._parent())

    def exit(self) -> None:
        log_event(log, "desktop_exit_requested")
        assert self.root is not None
        self.root.quit()

    # ------------------------------------------------------------------ helpers

    def _tk(self) -> tk.Tk:
        assert self.root is not None, "UI not started"
        return self.root

    def _parent(self) -> tk.Misc:
        if self.status_window and self.status_window.visible:
            return self.status_window.win
        return self._tk()

    def _refresh_now(self) -> None:
        self._apply_status(self.service.controller.tick().to_dict())

    def _sync_autostart(self, cfg: dict[str, Any]) -> None:
        try:
            if autostart.sync(cfg["start_with_windows"]):
                log_event(log, "autostart_changed", enabled=cfg["start_with_windows"])
        except OSError as exc:
            log_event(log, "autostart_failed", logging.ERROR, error=str(exc))


def _enable_dpi_awareness() -> None:
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass


def run_tray(service: WatchdogService, start_minimized: bool = False) -> int:
    return DesktopApp(service, start_minimized=start_minimized).run()
