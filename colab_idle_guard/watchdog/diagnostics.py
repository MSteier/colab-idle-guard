"""Exportable diagnostic report (spec §11).

A ZIP with the current status, configuration, environment details and the
(already redacted) logs. Every text file is passed through the redactor again
and checked for the live token before it is written, so the token can never
end up in a report even if a log line escaped redaction.
"""

from __future__ import annotations

import json
import platform
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .. import __version__
from .logging_manager import REDACTED, redactor

if TYPE_CHECKING:
    from .main import WatchdogService


def build_report(service: "WatchdogService") -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "app_version": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "token_storage_protected": service.token_store.protected,
        "config": service.config.snapshot(),
        "config_warnings": service.config.load_warnings,
        "status": service.status(),
        "notebooks": [
            {"integration": e.integration_kind, "execution": str(e.execution), "runtime": str(e.runtime),
             "stale": e.stale, "stale_reason": e.stale_reason}
            for e in service.execution.entries()
        ],
        "recent_notifications": [{"key": k, "title": t, "level": lvl} for k, t, _m, lvl in service.notifier.history[-20:]],
    }


def export_diagnostics(service: "WatchdogService", destination: Path) -> Path:
    token = service.token
    report = json.dumps(build_report(service), indent=2, default=str)

    def clean(text: str) -> str:
        text = redactor.redact(text)
        return text.replace(token, REDACTED) if token else text

    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("report.json", clean(report))
        log_dir = service.paths.log_dir
        if log_dir.exists():
            for log_file in sorted(log_dir.glob("*.log*")):
                try:
                    zf.writestr(f"logs/{log_file.name}", clean(log_file.read_text(encoding="utf-8", errors="replace")))
                except OSError:
                    continue
    return destination
