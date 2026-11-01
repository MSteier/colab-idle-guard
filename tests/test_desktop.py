"""Desktop layer: presentation logic, icons, autostart, diagnostics (no windows opened)."""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

from colab_idle_guard.desktop import presenter as p
from colab_idle_guard.desktop.icons import state_icon
from colab_idle_guard.watchdog.shutdown_controller import GuardState

from conftest import BUSY, CONNECTED, IDLE, Harness


def status(**overrides):
    base = {
        "state": "active", "label": "Active Colab session", "idle_seconds": 12.0, "timeout_seconds": 1800.0,
        "remaining_seconds": 1788.0, "warning_remaining_seconds": None, "runtime": "connected", "execution": "idle",
        "running_cells": 0, "blockers": [], "integrations": [], "auto_release_enabled": False,
        "release_supported": False, "last_operation": None, "paused": False, "stale_notebooks": 0,
    }
    base.update(overrides)
    return base


def test_fmt_duration() -> None:
    assert p.fmt_duration(0) == "0:00"
    assert p.fmt_duration(59.6) == "1:00"
    assert p.fmt_duration(1800) == "30:00"
    assert p.fmt_duration(3725) == "1:02:05"
    assert p.fmt_duration(None) == "-"
    assert p.fmt_duration(-5) == "0:00"


def test_headlines() -> None:
    assert p.headline(status(state="idle_countdown", remaining_seconds=754)) == "Idle - release check in 12:34"
    assert p.headline(status(state="warning", warning_remaining_seconds=42)) == "Releasing runtime in 0:42"
    assert p.headline(status(state="executing", running_cells=1)) == "Notebook executing (1 cell)"
    assert p.headline(status(state="executing", running_cells=3)) == "Notebook executing (3 cells)"
    assert p.headline(status(state="paused", label="Monitoring paused")) == "Monitoring paused"


def test_integrations_line_prefers_online_instance() -> None:
    s = status(integrations=[
        {"kind": "chrome", "status": "lost"}, {"kind": "chrome", "status": "online"},
        {"kind": "vscode", "status": "closed"},
    ])
    assert p.integrations_line(s) == "Chrome: connected · VS Code: closed"
    assert p.integrations_line(status()) == "Chrome: not connected · VS Code: not connected"


def test_blockers_are_humanised_and_deduplicated() -> None:
    s = status(blockers=["execution_state_unknown (integration_lost)", "execution_state_unknown (no telemetry)",
                         "runtime_state_unknown", "something_new"])
    lines = p.blocker_lines(s)
    assert lines[0].startswith("Execution state could not be verified")
    assert "No Colab notebook is reporting yet" in lines
    assert "Runtime connection state is unknown" in lines
    assert lines[-1] == "something_new"


def test_tooltip_fits_windows_limit() -> None:
    assert len(p.tooltip(status(label="x" * 300, state="monitoring"))) <= 127


def test_menu_flags() -> None:
    f = p.menu_flags(status())
    assert (f.pause, f.resume, f.cancel, f.release_now, f.acknowledge_unknown) == (True, False, False, False, False)
    assert p.menu_flags(status(release_supported=True)).release_now is True
    assert p.menu_flags(status(release_supported=True, execution="busy")).release_now is False
    assert p.menu_flags(status(release_supported=True, state="warning")).release_now is False
    assert p.menu_flags(status(state="warning")).cancel is True
    assert p.menu_flags(status(paused=True)).resume is True
    assert p.menu_flags(status(stale_notebooks=2)).acknowledge_unknown is True


def test_parse_settings_form() -> None:
    changes, errors = p.parse_settings_form({
        "idle_timeout_minutes": " 45 ", "warning_countdown_seconds": "abc", "api_port": "47821",
        "monitor_chrome": False, "log_level": "DEBUG",
    })
    assert changes == {"idle_timeout_minutes": 45, "api_port": 47821, "monitor_chrome": False, "log_level": "DEBUG"}
    assert errors and "warning countdown seconds" in errors[0]


def test_every_state_has_an_icon() -> None:
    for state in GuardState:
        img = state_icon(str(state))
        assert img.size == (64, 64) and img.mode == "RGBA"
        assert str(state) in p.STATE_STYLE
    assert state_icon("unknown") is state_icon("unknown")  # cached


def test_presenter_against_live_controller(harness: Harness) -> None:
    h = harness
    h.connect("c1", "chrome", nb1=(BUSY, CONNECTED, 2))
    s = h.tick().to_dict()
    assert p.headline(s) == "Notebook executing (2 cells)"
    assert p.execution_line(s) == "Cells running: 2"
    h.set_state("c1", nb1=(IDLE, CONNECTED))
    h.run(h.timeout + 30)
    s = h.controller.status().to_dict()
    assert s["state"] == "warning"
    assert p.headline(s).startswith("Releasing runtime in")
    assert p.menu_flags(s).cancel


@pytest.mark.skipif(sys.platform != "win32", reason="Windows registry")
def test_autostart_roundtrip_on_test_key() -> None:
    from colab_idle_guard.desktop import autostart
    import winreg

    key = r"Software\ColabIdleGuardTest\Run"
    try:
        assert autostart.is_enabled(key) is False
        autostart.set_enabled(True, key, command='"x.exe" --minimized')
        assert autostart.is_enabled(key) is True
        autostart.set_enabled(False, key)
        assert autostart.is_enabled(key) is False
        autostart.set_enabled(False, key)  # idempotent
    finally:
        for sub in (key, r"Software\ColabIdleGuardTest"):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
            except OSError:
                pass


def test_launch_command_points_at_windowless_launcher() -> None:
    from colab_idle_guard.desktop.autostart import launch_command

    cmd = launch_command()
    assert "run_colab_idle_guard.pyw" in cmd and cmd.endswith("--minimized")
    launcher = Path(__file__).resolve().parents[1] / "run_colab_idle_guard.pyw"
    assert launcher.exists()


def test_diagnostic_report_contains_no_token(tmp_path: Path) -> None:
    from colab_idle_guard.watchdog.diagnostics import export_diagnostics
    from colab_idle_guard.watchdog.main import WatchdogService
    from colab_idle_guard.watchdog.paths import AppPaths

    service = WatchdogService(paths=AppPaths(tmp_path / "c", tmp_path / "d"), port=0)
    try:
        # Simulate a log line that escaped redaction.
        service.paths.log_file.write_text(f'{{"msg": "leak {service.token}"}}\n', encoding="utf-8")
        out = export_diagnostics(service, tmp_path / "out" / "report.zip")
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            blob = b"".join(zf.read(n) for n in names)
            report = json.loads(zf.read("report.json"))
        assert "report.json" in names and any(n.startswith("logs/") for n in names)
        assert service.token.encode() not in blob
        assert report["config"]["idle_timeout_minutes"] == 30
        assert report["status"]["state"] == "monitoring"
    finally:
        service.stop()


def test_every_module_imports() -> None:
    """Catches syntax/import errors in modules no other test imports (e.g. the Tk windows),
    which would otherwise only surface when the packaged app starts."""
    import importlib
    import pkgutil

    import colab_idle_guard

    names = [m.name for m in pkgutil.walk_packages(colab_idle_guard.__path__, "colab_idle_guard.")]
    assert "colab_idle_guard.desktop.windows" in names and "colab_idle_guard.desktop.tray" in names
    for name in names:
        importlib.import_module(name)
