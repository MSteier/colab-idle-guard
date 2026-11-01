"""Command-line entry point: ``python -m colab_idle_guard [--headless]``."""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="colab-idle-guard", description="Releases idle Google Colab runtimes.")
    parser.add_argument("--headless", action="store_true", help="run the watchdog without the tray UI")
    parser.add_argument("--minimized", action="store_true", help="start in the tray without opening the status window")
    parser.add_argument("--console-log", action="store_true", help="also write logs to the console")
    parser.add_argument("--show-pairing-token", action="store_true",
                        help="print the pairing token for the extensions and exit")
    parser.add_argument("--version", action="version", version=__version__)
    args = parser.parse_args(argv)

    from .watchdog.main import ServiceStartError, WatchdogService

    service = WatchdogService(console_log=args.console_log)
    if args.show_pairing_token:
        print(service.token)
        return 0

    if not args.headless:
        try:
            from .desktop.tray import run_tray
        except ImportError as exc:
            # A windowed build has no console: log the reason and tell the user.
            import logging
            import traceback

            logging.getLogger("colab_idle_guard").error(
                "desktop_import_failed", extra={"event": "desktop_import_failed", "error": traceback.format_exc()})
            print(f"Desktop UI not available ({exc}); use --headless.", file=sys.stderr)
            try:
                import ctypes

                ctypes.windll.user32.MessageBoxW(None, f"The Colab Idle Guard window could not start:\n{exc}",
                                                 "Colab Idle Guard", 0x10)
            except Exception:
                pass
            return 2
        return run_tray(service, start_minimized=args.minimized)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        service.start()
    except ServiceStartError as exc:
        print(exc, file=sys.stderr)
        return 3
    print(f"Colab Idle Guard {__version__} running on 127.0.0.1:{service.api.port} (Ctrl+C to stop)")
    try:
        while not stop.wait(0.5):
            pass
    finally:
        service.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
