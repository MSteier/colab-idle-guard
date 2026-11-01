"""Tk windows: status window, release warning dialog and settings.

All functions here run on the Tk (main) thread only; see tray.DesktopApp.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable

from ..watchdog.configuration import LOG_LEVELS, ConfigError, ConfigManager
from . import presenter as p

PAD = 12


def apply_theme(root: tk.Tk) -> None:
    style = ttk.Style(root)
    for theme in ("vista", "winnative", "clam"):
        if theme in style.theme_names():
            style.theme_use(theme)
            break
    style.configure("Headline.TLabel", font=("Segoe UI", 13, "bold"))
    style.configure("Big.TLabel", font=("Segoe UI", 22, "bold"))
    style.configure("Muted.TLabel", foreground="#5d6670")
    style.configure("Warn.TLabel", foreground="#9a6700")


class StatusWindow:
    """Main window. Closing it hides it to the tray instead of exiting (spec §8)."""

    def __init__(self, root: tk.Tk, actions: dict[str, Callable[[], None]]) -> None:
        self.win = tk.Toplevel(root)
        self.win.title("Colab Idle Guard")
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self.hide)
        frame = ttk.Frame(self.win, padding=PAD)
        frame.grid(sticky="nsew")

        self.headline = ttk.Label(frame, style="Headline.TLabel", width=46)
        self.headline.grid(row=0, column=0, columnspan=2, sticky="w")
        self.rows: dict[str, ttk.Label] = {}
        for i, (key, label) in enumerate((
            ("runtime", "Runtime"), ("execution", "Cells"), ("integrations", "Integrations"),
            ("idle", "Idle for"), ("mode", "Mode"),
        ), start=1):
            ttk.Label(frame, text=label + ":", style="Muted.TLabel").grid(row=i, column=0, sticky="nw", pady=2)
            self.rows[key] = ttk.Label(frame, wraplength=360, justify="left")
            self.rows[key].grid(row=i, column=1, sticky="w", pady=2, padx=(8, 0))
        self.blockers = ttk.Label(frame, style="Warn.TLabel", wraplength=440, justify="left")
        self.blockers.grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(PAD, 0))
        self.pause_btn = ttk.Button(buttons, command=actions["toggle_pause"])
        self.cancel_btn = ttk.Button(buttons, text="Cancel release", command=actions["cancel"])
        self.release_btn = ttk.Button(buttons, text="Release now…", command=actions["release_now"])
        self.ack_btn = ttk.Button(buttons, text="Nothing is running…", command=actions["acknowledge"])
        settings_btn = ttk.Button(buttons, text="Settings…", command=actions["settings"])
        for i, b in enumerate((self.pause_btn, self.cancel_btn, self.release_btn, self.ack_btn, settings_btn)):
            b.grid(row=0, column=i, padx=(0 if i == 0 else 6, 0))
        self.visible = False

    def show(self) -> None:
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
        self.visible = True

    def hide(self) -> None:
        self.win.withdraw()
        self.visible = False

    def update(self, status: dict[str, Any]) -> None:
        self.headline.configure(text=p.headline(status))
        self.rows["runtime"].configure(text=p.runtime_line(status))
        self.rows["execution"].configure(text=p.execution_line(status))
        self.rows["integrations"].configure(text=p.integrations_line(status))
        self.rows["idle"].configure(
            text=f"{p.fmt_duration(status['idle_seconds'])} of {p.fmt_duration(status['timeout_seconds'])}")
        self.rows["mode"].configure(text=p.release_mode_line(status))
        blockers = p.blocker_lines(status)
        self.blockers.configure(text="\n".join("• " + b for b in blockers))
        flags = p.menu_flags(status)
        self.pause_btn.configure(text="Resume monitoring" if status["paused"] else "Pause monitoring")
        for button, enabled in ((self.cancel_btn, flags.cancel), (self.release_btn, flags.release_now),
                                (self.ack_btn, flags.acknowledge_unknown)):
            button.state(["!disabled"] if enabled else ["disabled"])


class WarningDialog:
    """Always-on-top countdown shown during the release warning (spec §7 steps 6-8)."""

    def __init__(self, root: tk.Tk, on_cancel: Callable[[], None]) -> None:
        self.win = tk.Toplevel(root)
        self.win.title("Colab Idle Guard - runtime release")
        self.win.attributes("-topmost", True)
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", on_cancel)  # closing the warning keeps the runtime
        frame = ttk.Frame(self.win, padding=PAD * 2)
        frame.grid()
        ttk.Label(frame, text="Your idle Colab runtime will be released in", style="Headline.TLabel").grid(row=0, sticky="w")
        self.countdown = ttk.Label(frame, style="Big.TLabel")
        self.countdown.grid(row=1, sticky="w", pady=(4, 8))
        ttk.Label(
            frame, wraplength=420, justify="left",
            text=("There has been no Colab activity and no cells are running. Releasing deletes the runtime's "
                  "variables and files (for example under /content). Your notebook itself is kept.\n\n"
                  "Interact with Colab, or press the button below, to keep the runtime."),
        ).grid(row=2, sticky="w")
        keep = ttk.Button(frame, text="Keep runtime (cancel)", command=on_cancel)
        keep.grid(row=3, sticky="e", pady=(PAD, 0))
        keep.focus_set()
        self.win.bind("<Escape>", lambda _e: on_cancel())
        self._center()

    def _center(self) -> None:
        self.win.update_idletasks()
        w, h = self.win.winfo_width(), self.win.winfo_height()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        self.win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 3}")

    def update(self, status: dict[str, Any]) -> None:
        self.countdown.configure(text=p.fmt_duration(status.get("warning_remaining_seconds")))

    def close(self) -> None:
        self.win.destroy()


CONSENT_TEXT = (
    "Automatic release will disconnect AND DELETE an idle Colab runtime once all safety checks pass.\n\n"
    "• Variables, loaded models and files on the runtime (e.g. /content) are lost.\n"
    "• Your notebook file is kept (Drive / your local .ipynb).\n"
    "• It never happens while a cell is running or while the state cannot be verified, "
    "and you always get a warning you can cancel first.\n\n"
    "Colab offers no gentler way to stop a runtime from using compute units.\n\n"
    "Enable automatic release?"
)


class SettingsWindow:
    def __init__(self, root: tk.Tk, config: ConfigManager, on_close: Callable[[], None]) -> None:
        self.config = config
        self.on_close = on_close
        cfg = config.snapshot()
        self.win = tk.Toplevel(root)
        self.win.title("Colab Idle Guard - Settings")
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        f = ttk.Frame(self.win, padding=PAD)
        f.grid()

        self.vars: dict[str, tk.Variable] = {}
        row = 0

        def section(text: str) -> None:
            nonlocal row
            ttk.Label(f, text=text, style="Headline.TLabel").grid(row=row, column=0, columnspan=2, sticky="w", pady=(PAD if row else 0, 4))
            row += 1

        def entry(key: str, label: str, hint: str = "") -> None:
            nonlocal row
            self.vars[key] = tk.StringVar(value=str(cfg[key]))
            ttk.Label(f, text=label).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(f, textvariable=self.vars[key], width=10).grid(row=row, column=1, sticky="w", pady=2)
            if hint:
                ttk.Label(f, text=hint, style="Muted.TLabel").grid(row=row, column=2, sticky="w", padx=6)
            row += 1

        def check(key: str, label: str, enabled: bool = True) -> ttk.Checkbutton:
            nonlocal row
            self.vars[key] = tk.BooleanVar(value=bool(cfg[key]))
            cb = ttk.Checkbutton(f, text=label, variable=self.vars[key])
            cb.grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
            if not enabled:
                cb.state(["disabled"])
            row += 1
            return cb

        section("Idle detection")
        entry("idle_timeout_minutes", "Idle timeout (minutes)", "1-1440")
        entry("warning_countdown_seconds", "Warning countdown (seconds)", "10-600")
        check("monitor_chrome", "Monitor Colab in Chrome")
        check("monitor_vscode", "Monitor Colab notebooks in VS Code")

        section("Release")
        self.auto_cb = check("automatic_disconnect_enabled", "Release idle runtimes automatically (deletes runtime state)")
        self.vars["automatic_disconnect_enabled"].set(config.auto_release_allowed)
        ttk.Label(f, style="Muted.TLabel", wraplength=420, justify="left",
                  text="Always on and cannot be disabled: never release while a cell runs, "
                       "or while execution state cannot be verified.").grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1

        section("General")
        check("notifications_enabled", "Show Windows notifications")
        check("start_with_windows", "Start with Windows (minimised to tray)")
        ttk.Label(f, text="Log level").grid(row=row, column=0, sticky="w", pady=2)
        self.vars["log_level"] = tk.StringVar(value=cfg["log_level"])
        ttk.Combobox(f, textvariable=self.vars["log_level"], values=LOG_LEVELS, state="readonly", width=9).grid(row=row, column=1, sticky="w")
        row += 1
        entry("api_port", "Local port", "restart required")
        pinned = cfg["allowed_chrome_extension_ids"]
        if pinned:
            ttk.Label(f, text="Paired custom Chrome extension").grid(row=row, column=0, sticky="w", pady=2)
            self.forget_btn = ttk.Button(f, text="Forget", command=self.forget_extension)
            self.forget_btn.grid(row=row, column=1, sticky="w", pady=2)
            ttk.Label(f, text=pinned[0][:12] + "…", style="Muted.TLabel").grid(row=row, column=2, sticky="w", padx=6)
            row += 1

        buttons = ttk.Frame(f)
        buttons.grid(row=row, column=0, columnspan=3, sticky="e", pady=(PAD, 0))
        ttk.Button(buttons, text="Cancel", command=self.close).grid(row=0, column=0)
        ttk.Button(buttons, text="Save", command=self.save).grid(row=0, column=1, padx=(6, 0))

    def show(self) -> None:
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def close(self) -> None:
        self.win.destroy()
        self.on_close()

    def forget_extension(self) -> None:
        """Allow a different (e.g. reinstalled) custom Chrome extension to pair."""
        if messagebox.askyesno("Forget paired extension",
                               "Forget the paired custom Chrome extension? The next extension that connects "
                               "with the pairing token will be paired instead.\n\n"
                               "(The official Colab Idle Guard extension is always accepted.)", parent=self.win):
            self.config.update({"allowed_chrome_extension_ids": []})
            self.forget_btn.state(["disabled"])

    def save(self) -> None:
        form = {k: v.get() for k, v in self.vars.items()}
        changes, errors = p.parse_settings_form(form)
        if errors:
            messagebox.showerror("Invalid settings", "\n".join(errors), parent=self.win)
            return
        wants_auto = changes.pop("automatic_disconnect_enabled", False)
        if wants_auto and not self.config.auto_release_allowed:
            if not messagebox.askyesno("Enable automatic release?", CONSENT_TEXT, icon="warning", parent=self.win):
                wants_auto = False
                self.vars["automatic_disconnect_enabled"].set(False)
            else:
                changes["release_consent_acknowledged"] = True
        changes["automatic_disconnect_enabled"] = wants_auto
        try:
            self.config.update(changes)
        except ConfigError as exc:
            messagebox.showerror("Invalid settings", str(exc).replace("; ", "\n"), parent=self.win)
            return
        self.close()
