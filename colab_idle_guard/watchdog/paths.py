"""Filesystem locations used by the application.

Roaming settings live under %APPDATA%; machine-local secrets and logs live under
%LOCALAPPDATA%. Setting COLAB_IDLE_GUARD_HOME redirects everything to one folder
(used by tests and portable installs).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_DIR_NAME = "ColabIdleGuard"
HOME_ENV_VAR = "COLAB_IDLE_GUARD_HOME"


@dataclass(frozen=True)
class AppPaths:
    config_dir: Path
    data_dir: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def token_file(self) -> Path:
        return self.data_dir / "token.bin"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "colab_idle_guard.log"

    def ensure(self) -> None:
        for directory in (self.config_dir, self.data_dir, self.log_dir):
            directory.mkdir(parents=True, exist_ok=True)


def default_paths() -> AppPaths:
    override = os.environ.get(HOME_ENV_VAR)
    if override:
        root = Path(override)
        return AppPaths(config_dir=root / "config", data_dir=root / "data")
    appdata = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return AppPaths(config_dir=appdata / APP_DIR_NAME, data_dir=local / APP_DIR_NAME)


def bundled_default_config() -> Path:
    """Path to config/default.json shipped with the application (source or PyInstaller)."""
    import sys

    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2]))
    return base / "config" / "default.json"
