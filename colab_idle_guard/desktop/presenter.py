"""Pure presentation logic for the tray and windows (unit-tested; no GUI imports).

Everything shown to the user is derived here from a status dict
(``GuardStatus.to_dict()``), so the GUI modules only lay things out.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

KIND_LABELS = {"chrome": "Chrome", "vscode": "VS Code"}

# state -> (fill colour, glyph drawn on the tray icon)
STATE_STYLE: dict[str, tuple[str, str]] = {
    "monitoring": ("#6e7781", ""),
    "active": ("#1a7f37", ""),
    "executing": ("#0969da", "play"),
    "idle_countdown": ("#bf8700", ""),
    "warning": ("#cf222e", "!"),
    "disconnecting": ("#8250df", ""),
    "disconnected": ("#424a53", ""),
    "unknown": ("#d4580c", "?"),
    "idle_notified": ("#bf8700", "!"),
    "release_failed": ("#cf222e", "x"),
    "paused": ("#6e7781", "pause"),
}

_BLOCKER_TEXT = (
    ("execution_active", "A notebook cell is running"),
    ("execution_state_unknown (no telemetry)", "No Colab notebook is reporting yet"),
    ("execution_state_unknown", "Execution state could not be verified (an integration stopped reporting, or a notebook closed while busy)"),
    ("runtime_state_unknown", "Runtime connection state is unknown"),
    ("idle_monitoring_paused", "Monitoring is paused"),
)


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def headline(status: dict[str, Any]) -> str:
    state = status["state"]
    if state == "idle_countdown":
        return f"Idle - release check in {fmt_duration(status['remaining_seconds'])}"
    if state == "warning":
        return f"Releasing runtime in {fmt_duration(status['warning_remaining_seconds'])}"
    if state == "executing":
        n = status.get("running_cells", 0)
        return f"Notebook executing ({n} cell{'s' if n != 1 else ''})"
    if state == "idle_notified":
        return "Idle - release the runtime manually"
    return status["label"]


def integrations_summary(status: dict[str, Any]) -> dict[str, str]:
    """Per integration kind: 'connected', 'disconnected' (said goodbye), 'lost' or 'not connected'."""
    rank = {"online": 3, "lost": 2, "closed": 1}
    best: dict[str, str] = {}
    for item in status.get("integrations", ()):
        current = best.get(item["kind"])
        if current is None or rank.get(item["status"], 0) > rank.get(current, 0):
            best[item["kind"]] = item["status"]
    words = {"online": "connected", "lost": "lost contact", "closed": "closed"}
    return {kind: words.get(best.get(kind, ""), "not connected") for kind in KIND_LABELS}


def integrations_line(status: dict[str, Any]) -> str:
    summary = integrations_summary(status)
    return " · ".join(f"{KIND_LABELS[k]}: {v}" for k, v in summary.items())


def execution_line(status: dict[str, Any]) -> str:
    execution = status["execution"]
    if execution == "busy":
        return f"Cells running: {status.get('running_cells', 0)}"
    if execution == "idle":
        return "No cells running"
    return "Cell execution state unknown"


def runtime_line(status: dict[str, Any]) -> str:
    return {"connected": "Runtime connected", "disconnected": "No runtime connected"}.get(
        status["runtime"], "Runtime state unknown")


def blocker_lines(status: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for raw in status.get("blockers", ()):
        text = next((t for prefix, t in _BLOCKER_TEXT if raw.startswith(prefix)), raw)
        if text not in out:
            out.append(text)
    return out


def tooltip(status: dict[str, Any]) -> str:
    """Tray tooltip; Windows truncates at 127 characters."""
    text = f"Colab Idle Guard - {headline(status)}"
    return text if len(text) <= 127 else text[:124] + "..."


@dataclass(frozen=True)
class MenuFlags:
    pause: bool
    resume: bool
    cancel: bool
    release_now: bool
    acknowledge_unknown: bool


def menu_flags(status: dict[str, Any]) -> MenuFlags:
    state = status["state"]
    return MenuFlags(
        pause=not status["paused"],
        resume=status["paused"],
        cancel=state in ("warning", "disconnecting"),
        release_now=(status["runtime"] == "connected" and status["execution"] == "idle"
                     and status["release_supported"] and state not in ("disconnecting", "warning")),
        acknowledge_unknown=status.get("stale_notebooks", 0) > 0,
    )


def release_mode_line(status: dict[str, Any]) -> str:
    if status["auto_release_enabled"]:
        return "Automatic release: on"
    return "Automatic release: off (notify only)"


# ------------------------------------------------------------------ settings

SETTINGS_INT_FIELDS = ("idle_timeout_minutes", "warning_countdown_seconds", "api_port")
SETTINGS_BOOL_FIELDS = (
    "monitor_chrome", "monitor_vscode", "notifications_enabled", "start_with_windows", "automatic_disconnect_enabled",
)


def parse_settings_form(form: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Convert raw form values (strings from entry widgets, bools from checkboxes)
    into config changes. Range checks are left to ConfigManager.update."""
    changes: dict[str, Any] = {}
    errors: list[str] = []
    for key in SETTINGS_INT_FIELDS:
        if key not in form:
            continue
        raw = str(form[key]).strip()
        try:
            changes[key] = int(raw)
        except ValueError:
            errors.append(f"{key.replace('_', ' ')}: '{raw}' is not a whole number")
    for key in SETTINGS_BOOL_FIELDS:
        if key in form:
            changes[key] = bool(form[key])
    if "log_level" in form:
        changes["log_level"] = str(form["log_level"])
    return changes, errors
