"""Start-with-Windows via the per-user Run key (HKCU; no admin rights needed)."""

from __future__ import annotations

import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ColabIdleGuard"


def launch_command() -> str:
    """Command line Windows runs at sign-in (starts minimised to the tray)."""
    if getattr(sys, "frozen", False):  # PyInstaller build
        return f'"{sys.executable}" --minimized'
    exe = Path(sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    launcher = Path(__file__).resolve().parents[2] / "run_colab_idle_guard.pyw"
    return f'"{pythonw if pythonw.exists() else exe}" "{launcher}" --minimized'


def current_command(key_path: str = RUN_KEY, value_name: str = VALUE_NAME) -> str | None:
    """The registered start-up command line, or None if not registered."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            value, _type = winreg.QueryValueEx(key, value_name)
            return str(value)
    except FileNotFoundError:
        return None


def is_enabled(key_path: str = RUN_KEY, value_name: str = VALUE_NAME) -> bool:
    return current_command(key_path, value_name) is not None


def sync(enabled: bool) -> bool:
    """Make the Run entry match ``enabled`` and point at this installation.
    Returns True if anything changed (e.g. after moving from source to the .exe)."""
    current = current_command()
    if enabled and current != launch_command():
        set_enabled(True)
        return True
    if not enabled and current is not None:
        set_enabled(False)
        return True
    return False


def set_enabled(enabled: bool, key_path: str = RUN_KEY, value_name: str = VALUE_NAME,
                command: str | None = None) -> None:
    if sys.platform != "win32":
        return
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, command or launch_command())
        else:
            try:
                winreg.DeleteValue(key, value_name)
            except FileNotFoundError:
                pass
