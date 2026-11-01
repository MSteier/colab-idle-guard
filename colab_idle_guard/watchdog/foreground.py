"""Win32 foreground-application tracking (ADR-3).

Integrations report activity only from inside a Colab tab or a Colab-backed
notebook. As a second, independent check, the watchdog samples which process
owns the foreground window and rejects activity that arrives while the matching
application (Chrome / VS Code) was not in the foreground, e.g. a mouse passing
over a background Chrome window. No keyboard or mouse hooks are installed; only
the foreground window's process image name is read.
"""

from __future__ import annotations

import bisect
import ctypes
import logging
import sys
import threading
from collections import deque
from ctypes import wintypes
from pathlib import PureWindowsPath
from typing import Callable

log = logging.getLogger(__name__)

INTEGRATION_PROCESSES: dict[str, frozenset[str]] = {
    "chrome": frozenset({"chrome.exe"}),
    "vscode": frozenset({"code.exe", "code - insiders.exe"}),
}


def foreground_process_name() -> str | None:
    """Image name (lower case) of the process owning the foreground window, or None."""
    if sys.platform != "win32":
        return None
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return None
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return None
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return None
        return PureWindowsPath(buffer.value).name.lower()
    finally:
        kernel32.CloseHandle(handle)


class ForegroundTracker:
    """Keeps a short history of foreground-process samples for corroboration."""

    def __init__(
        self,
        clock: Callable[[], float],
        sampler: Callable[[], str | None] = foreground_process_name,
        interval: float = 1.0,
        history_seconds: float = 180.0,
        tolerance: float = 2.5,
    ) -> None:
        self._clock = clock
        self._sampler = sampler
        self._interval = interval
        self._tolerance = tolerance
        self._samples: deque[tuple[float, str | None]] = deque(maxlen=int(history_seconds / interval) + 1)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def sample(self) -> None:
        try:
            name = self._sampler()
        except Exception:  # never let a Win32 hiccup kill the sampler
            log.debug("foreground sample failed", exc_info=True)
            name = None
        with self._lock:
            self._samples.append((self._clock(), name))

    def was_foreground(self, integration_kind: str, at: float) -> bool | None:
        """True/False if the integration's app was (not) foreground around ``at``.

        Returns None when there is no sample close enough to decide. Any sample
        within ``tolerance`` seconds of ``at`` showing the app counts as True,
        which absorbs focus changes between sampling instants.
        """
        allowed = INTEGRATION_PROCESSES.get(integration_kind)
        if allowed is None:
            return False
        with self._lock:
            samples = list(self._samples)
        if not samples:
            return None
        times = [t for t, _ in samples]
        lo = bisect.bisect_left(times, at - self._tolerance)
        hi = bisect.bisect_right(times, at + self._tolerance)
        window = samples[lo:hi]
        if not window:
            return None
        return any(name in allowed for _, name in window if name)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="foreground-tracker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            self.sample()
            self._stop.wait(self._interval)
