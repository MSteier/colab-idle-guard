# Changelog

## [0.1.0] - 2026-10-31
### Added
- Phase 8: packaging and documentation:
  - PyInstaller build of `ColabIdleGuard.exe` (windowed, one-folder) and
    `scripts/build.ps1` (static checks, all tests, exe, VSIX, Chrome ZIP, PDF
    manual, release ZIP with SHA-256);
  - per-user `install.ps1` / `uninstall.ps1` (Start menu, Settings > Apps,
    VS Code extension, user data preserved);
  - README, USER_MANUAL (+ PDF), INSTALLATION, TROUBLESHOOTING, SECURITY,
    IMPLEMENTATION_REPORT;
  - fixed Chrome extension ID via a manifest key (always accepted);
    "Forget paired extension" in Settings;
  - start-with-Windows entry follows the program when it moves;
  - packaged app reports desktop start-up errors instead of exiting silently.
- Phase 7: testing pass (docs/TEST_REPORT.md): full-stack system tests over
  HTTP with simulated integrations (11), API fuzzing and concurrency stress,
  tests for previously uncovered safety paths, a randomised safety-property
  simulation (60 seeds, 299 releases, validated by mutation testing), mypy/tsc clean, branch coverage
  (94% on safety-critical modules), fresh-venv install check. Dev-only
  dependencies: mypy, coverage.
- Phase 6: runtime release, validated live on CPU runtimes (research §9):
  - Chrome: the extension performs Colab's own *Runtime → Disconnect and
    delete runtime* (stable command ID and dialog-action selectors), re-checks
    execution state right before the destructive click, and reports success
    only after 5 s of a disconnected state;
  - VS Code: `google.colab.runtime.unassign()` via the Jupyter kernel API, only
    for kernels verified as Colab and idle;
  - watchdog `ChromeColabUiAdapter` / `VSCodeColabUnassignAdapter` over the
    command channel; confirmation requires the runtime to stay disconnected
    for 5 s in fresh reports.
- Phase 5: Windows desktop app (`colab_idle_guard/desktop/`):
  - system-tray icon that changes with state, a live tooltip, and a menu
    (status, pause/resume, cancel release, release now, clear unknown state,
    copy pairing token, settings, logs, diagnostic report, exit);
  - status window that hides to the tray when closed;
  - always-on-top release warning with a live countdown and "Keep runtime";
  - settings window with validation and an explicit consent dialog before
    automatic release can be enabled;
  - Windows notifications, start with Windows (HKCU Run key, starts
    minimised), and an exportable diagnostic ZIP (token-free, re-redacted);
  - `run_colab_idle_guard.pyw` windowless launcher and `--minimized`;
  - 12 unit tests plus a scripted GUI smoke test against the real app.
- Phase 4: VS Code extension (`vscode_extension/`, TypeScript):
  - Colab kernel detection through the official Jupyter kernel API (a
    read-only probe of `COLAB_RELEASE_TAG`, once per kernel session), with
    manual marking as a fallback;
  - execution state from the kernel's own busy/idle status, with a 3 s settle
    window for queued cells;
  - activity from Colab notebooks only (selection, scrolling, edits, typing),
    and only while VS Code has focus;
  - automatic pairing via the DPAPI-protected token file, or a manual token in
    SecretStorage; status bar item and commands;
  - 16 Node tests, including tests against the real watchdog; packaged as a
    VSIX and validated live on a CPU Colab server (research §8).
- Phase 3: Chrome extension (`chrome_extension/`, Manifest V3):
  - kernel WebSocket observer (Jupyter protocol, header fields only) plus
    Colab DOM signals, combined fail-safe. Validated live on a CPU runtime and
    end to end with the installed extension (docs/COLAB_RUNTIME_RESEARCH.md §7);
  - activity reporting (event kinds only; trusted events; focused, visible tab);
  - service worker with storage-backed state that survives suspension,
    heartbeats, activity batching and command routing;
  - options page (pairing) and status popup;
  - 26 Node tests, including end-to-end tests against the real watchdog.
- Phase 2: Python watchdog core (`colab_idle_guard/watchdog/`):
  - validated, atomic, upgrade-safe configuration (`configuration.py`);
  - JSON-lines logging with token redaction (`logging_manager.py`);
  - 256-bit API token stored with Windows DPAPI (`security.py`);
  - shared activity timestamp with Win32 foreground corroboration
    (`activity_monitor.py`, `foreground.py`);
  - per-notebook execution and runtime state with fail-closed stale handling
    (`execution_monitor.py`);
  - at-most-once command channel to integrations (`commands.py`);
  - runtime adapter interface and default notify-only adapter
    (`runtime_adapters/`), plus a release coordinator with independent
    confirmation (`runtime_manager.py`);
  - idle and release state machine (`shutdown_controller.py`);
  - authenticated, loopback-only HTTP API (`api_server.py`);
  - service wiring and `python -m colab_idle_guard --headless`;
  - automated tests covering spec TEST 1–12.
- Phase 1: project structure, architecture decisions (docs/ARCHITECTURE.md),
  and Colab runtime-management research (docs/COLAB_RUNTIME_RESEARCH.md).

### Changed
- VS Code: instance ID is stable across window reloads; the goodbye is sent
  first on shutdown; the Colab-probe retry is shortened to 20 s.
- Watchdog: when an integration returns and no longer lists a notebook that
  was idle when contact was lost, the stale entry is cleared (busy ones stay
  unknown).
- Watchdog: only executions of 10 s or more restart the idle timer (kernel
  busy blips from completions no longer do).

### Fixed
- Watchdog: a failed release could be retried automatically within the same
  idle episode if another event (short busy blip, brief "disconnected",
  momentary unknown state) changed the displayed state first. Found by the
  randomised safety-property test; regression-tested.
- API server: a rejected request's unread body no longer corrupts the next
  request on the same keep-alive connection; error responses close the
  connection after draining the body (found by the extension's end-to-end test).
- Chrome extension: options page no longer hangs on "Testing…"; tabs are
  only reported once a notebook is recognised; silent-tab expiry raised to
  150 s to tolerate Chrome's background-tab timer throttling.
