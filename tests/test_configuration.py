"""Configuration persistence, validation and upgrade safety."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from colab_idle_guard.watchdog.configuration import ConfigError, ConfigManager, defaults
from colab_idle_guard.watchdog.paths import bundled_default_config


def test_shipped_default_json_matches_schema() -> None:
    assert json.loads(bundled_default_config().read_text(encoding="utf-8")) == defaults()


def test_spec_defaults() -> None:
    d = defaults()
    assert d["idle_timeout_minutes"] == 30
    assert d["warning_countdown_seconds"] == 60
    assert d["automatic_disconnect_enabled"] is False
    assert d["release_consent_acknowledged"] is False


def test_creates_file_with_defaults(tmp_path: Path) -> None:
    cfg = ConfigManager(tmp_path / "c" / "config.json")
    cfg.load()
    assert json.loads(cfg.path.read_text()) == defaults()


def test_user_values_preserved_and_invalid_values_fall_back(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"idle_timeout_minutes": 45, "warning_countdown_seconds": "soon",
                                "log_level": "LOUD", "future_setting": {"x": 1}}))
    cfg = ConfigManager(path)
    values = cfg.load()
    assert values["idle_timeout_minutes"] == 45
    assert values["warning_countdown_seconds"] == 60
    assert values["log_level"] == "INFO"
    assert len(cfg.load_warnings) == 2
    cfg.update({"idle_timeout_minutes": 50})
    saved = json.loads(path.read_text())
    assert saved["future_setting"] == {"x": 1}  # unknown keys survive upgrades/downgrades
    assert saved["idle_timeout_minutes"] == 50


def test_corrupt_file_is_backed_up(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{not json")
    cfg = ConfigManager(path)
    assert cfg.load() == defaults()
    assert (tmp_path / "config.invalid.json").read_text() == "{not json"


@pytest.mark.parametrize("change", [
    {"idle_timeout_minutes": 0},
    {"idle_timeout_minutes": True},
    {"idle_timeout_minutes": 2.5},
    {"automatic_disconnect_enabled": "yes"},
    {"no_such_key": 1},
    {"allowed_chrome_extension_ids": [1]},
])
def test_update_rejects_invalid(tmp_path: Path, change: dict) -> None:
    cfg = ConfigManager(tmp_path / "config.json")
    cfg.load()
    with pytest.raises(ConfigError):
        cfg.update(change)
    assert cfg.snapshot() == defaults()  # nothing partially applied


def test_update_is_all_or_nothing(tmp_path: Path) -> None:
    cfg = ConfigManager(tmp_path / "config.json")
    cfg.load()
    with pytest.raises(ConfigError):
        cfg.update({"idle_timeout_minutes": 20, "warning_countdown_seconds": 1})
    assert cfg.get("idle_timeout_minutes") == 30


def test_auto_release_requires_consent_and_revoking_disables(tmp_path: Path) -> None:
    cfg = ConfigManager(tmp_path / "config.json")
    cfg.load()
    cfg.update({"automatic_disconnect_enabled": True})
    assert cfg.auto_release_allowed is False
    cfg.update({"release_consent_acknowledged": True})
    assert cfg.auto_release_allowed is True
    cfg.update({"release_consent_acknowledged": False})
    assert cfg.get("automatic_disconnect_enabled") is False


def test_listeners_notified(tmp_path: Path) -> None:
    cfg = ConfigManager(tmp_path / "config.json")
    cfg.load()
    seen = []
    cfg.add_listener(lambda snap: seen.append(snap["idle_timeout_minutes"]))
    cfg.add_listener(lambda snap: 1 / 0)  # broken listener must not break updates
    cfg.update({"idle_timeout_minutes": 10})
    assert seen == [10]


def test_no_temp_files_left_behind(tmp_path: Path) -> None:
    cfg = ConfigManager(tmp_path / "config.json")
    cfg.load()
    for i in range(5):
        cfg.update({"idle_timeout_minutes": 10 + i})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["config.json"]
