# Colab Idle Guard — Architecture

Status: Phase 1 baseline (2026-10-31). Read together with
[COLAB_RUNTIME_RESEARCH.md](COLAB_RUNTIME_RESEARCH.md).

## 1. Development environment (inspected)

| Tool | Version | Notes |
|---|---|---|
| Windows | 11 (10.0.26300) | Target platform |
| Python | 3.12.10 via `py -3.12` | Project venv at `.venv`. The system default `python` is 3.10, so scripts always call the venv interpreter explicitly |
| Node.js / npm | 24.22.2 / 12.0.2 | Builds the VS Code extension |
| VS Code | 1.144.0 | `google.colab` 0.9.7 and `ms-toolsai.jupyter` 2025.9.1 installed |
| Chrome | installed (Program Files) | Manifest V3 |
| Git | 2.56 | |

## 2. Component overview

```
 ┌──────────────── Chrome ────────────────┐     ┌──────────── VS Code ─────────────┐
 │ content.js  (colab.research.google.com)│     │ colab-idle-guard extension (TS)  │
 │  • activity events (no content)        │     │  • notebook UI/edit events       │
 │  • execution/connection probes         │     │  • Jupyter kernel status         │
 │ service_worker.js                      │     │  • Colab association check       │
 │  • batching, heartbeat, command poll   │     │  • heartbeat, command poll       │
 └───────────────┬────────────────────────┘     └───────────────┬──────────────────┘
                 │ HTTP 127.0.0.1:<port>, Bearer token           │
                 ▼                                               ▼
 ┌───────────────────────────── Python watchdog ───────────────────────────────────┐
 │ api_server ──► IntegrationRegistry (heartbeats, liveness)                        │
 │              ├► ActivityMonitor   (single shared last-activity timestamp)        │
 │              └► ExecutionMonitor  (per integration × notebook; IDLE/BUSY/UNKNOWN)│
 │ foreground_check (Win32: is Chrome/Code actually the foreground app?)            │
 │ ShutdownController (state machine) ──► RuntimeManager ──► runtime adapters       │
 │ NotificationManager · Configuration · Security (token/DPAPI) · Logging           │
 └──────────────────────────────────────┬─────────────────────────────────────────┘
                                         │ in-process (thread-safe snapshot API)
                               ┌─────────▼─────────┐
                               │ desktop: tray +   │
                               │ settings + warning│
                               └───────────────────┘
```

The watchdog core and the desktop UI run in **one process** (the tray runs on
the main thread; the HTTP server and controller tick run on worker threads).
`python -m colab_idle_guard --headless` runs the core without the UI, which
the integration tests use.

## 3. Key decisions (ADRs)

**ADR-1 — "Disconnect" means "release runtime"; it is destructive and opt-in.**
Research shows Colab has no nondestructive disconnect that stops compute usage
(research §1–2). The application therefore separates three things:
*notify* (default), *release* (opt-in, VM state lost, notebook file kept), and
*delete files* (never done). The UI says "Release runtime (deletes VM state)"
instead of the ambiguous word "disconnect".

**ADR-2 — Fail closed on uncertainty.** Execution state is three-valued:
`IDLE`, `BUSY`, `UNKNOWN`. Only a fresh, positive `IDLE` from every live
integration that saw the runtime, with no `UNKNOWN` anywhere, allows release.
Missing telemetry *never* becomes `IDLE`. A stale `BUSY` becomes
`UNKNOWN (stale)`, which stays blocking until the integration reconnects and
reports, or until a runtime-side check (e.g. Jupyter kernel status) shows idle.
`stale_verification_max_minutes` only controls when the user is *alerted*
that state is stuck. It never unlocks release.

**ADR-3 — Activity is counted only at the source, then corroborated.**
The content script runs only on `https://colab.research.google.com/*`, and the
VS Code extension counts only events from notebooks proven to be Colab-backed.
The watchdog also uses the Win32 foreground-window owner process
(`GetForegroundWindow` → `GetWindowThreadProcessId` → image name) to reject
input-type activity that arrives while neither Chrome nor VS Code is in the
foreground. No global keyboard or mouse hooks are used (spec §3D).

**ADR-4 — Pull-based command channel.** MV3 service workers and VS Code
extensions can't be reliably pushed to from a local process, so integrations
send heartbeats every 15 s (Chrome uses `chrome.alarms` plus event-driven
wakeups) and collect pending commands (e.g. `release_runtime`) in the
heartbeat response. A command carries a nonce and expiry, and the integration
reports the outcome.

**ADR-5 — Minimal dependencies.** HTTP server: stdlib `ThreadingHTTPServer`.
Win32 and DPAPI: `ctypes`. Settings and warning dialog: `tkinter`. Tray:
`pystray` + `Pillow`, the only runtime dependencies. Tests: `pytest`.
Packaging: `PyInstaller` (build-time only).

**ADR-6 — Authentication.** A 256-bit token from `secrets.token_urlsafe(32)`,
stored DPAPI-encrypted (`CryptProtectData`, current-user scope) in
`%LOCALAPPDATA%\ColabIdleGuard\token.bin`. The user pairs each integration once:
the tray offers "Copy pairing token", and the token is pasted into the Chrome
extension options (kept in `chrome.storage.local`) and VS Code
(`SecretStorage`). The server checks the token with `hmac.compare_digest`,
requires the `Host` header to be `127.0.0.1:<port>`, accepts only the
`chrome-extension://<pinned id>` Origin or no Origin (VS Code), and rejects
any other browser origin, which blocks DNS-rebinding and drive-by requests
from web pages. Tokens are redacted from logs.

**ADR-7 — Python package layout.** The spec's top-level `watchdog/`,
`desktop/` and `runtime_adapters/` become subpackages of one importable
package, `colab_idle_guard`. This avoids a name clash with the popular PyPI
package `watchdog`, gives absolute imports, and gives PyInstaller a single
entry point.

**ADR-8 — Configuration and upgrades.** `config/default.json` ships with the
app. User config lives at `%APPDATA%\ColabIdleGuard\config.json`. On load,
defaults are merged *under* the user values, unknown keys are kept, and
values are checked for type and range; any invalid value falls back to its
default and the problem is logged. Writes are atomic (temp file + replace).
Upgrades never overwrite user config. `automatic_disconnect_enabled` is only
honoured when `release_consent_acknowledged` is also true.

**ADR-9 — Idle time restarts when execution finishes (Phase 2).**
`idle_timer_starts_after_execution` (default on) measures idle time from the
later of the last interaction and the last execution end. Without it, a
2-hour unattended training run would hit the 30-minute limit the moment it
finished, and the in-memory model would be released before the user saw the
result.

**ADR-10 — Running-cell protection cannot be switched off (Phase 2).**
`protect_running_cells` and `require_verified_execution_state` appear in the
config for transparency, but the controller enforces both no matter what they
are set to (`configuration.ALWAYS_ENFORCED`). Spec §5 makes these
non-negotiable, so a stray config edit must not be able to disable them.

**ADR-11 — Foreground check fails open, telemetry fails closed (Phase 2).**
If the foreground check *cannot* decide (no sample), the activity is
accepted. Wrongly rejecting real activity could release a runtime the user
is working in; wrongly accepting it only delays a release. Missing execution
or runtime telemetry is the opposite case: it always blocks release.

**ADR-12 — Release success needs independent confirmation (Phase 2).**
An adapter reporting "done" is not enough. The operation succeeds only once
a fresh integration report, received *after* the adapter finished, shows
the runtime disconnected. Otherwise it fails after 60 s. Failed releases are
never retried automatically; a new idle period is needed first.

## 4. Shutdown state machine

```
MONITORING ──(runtime connected & activity)──► ACTIVE ──(no activity)──► IDLE_COUNTDOWN
     ▲                                            ▲  ▲                         │ elapsed ≥ timeout
     │ paused/resumed                  activity ──┘  └── cell starts / UNKNOWN │ & all checks pass
 PAUSED                                                                        ▼
                                  any activity / BUSY / UNKNOWN / cancel ◄─ WARNING (60 s)
 EXECUTING (BUSY) blocks the countdown; UNKNOWN blocks release        │ recheck all conditions
                                                                      ▼
            notify-only ─► NOTIFIED_IDLE            RELEASING ─► verify ─► RELEASED | RELEASE_FAILED
```

The checks, re-evaluated at the start of WARNING *and* immediately before
release: runtime connected (positively reported); idle ≥ timeout; zero BUSY
anywhere; zero UNKNOWN anywhere; telemetry from the releasing integration
fresher than `telemetry_fresh_seconds`; not paused; and, for release,
`automatic_disconnect_enabled` and consent. The time source is
`time.monotonic()`, so changes to the system clock cannot trigger a release.

## 5. Restart behaviour (spec test 12)

State is **not** persisted across restarts. On startup everything is `UNKNOWN`
and the countdown begins only after integrations report fresh state. The
last-activity time is set to *startup time*, so a restart can never make a
release happen sooner.

## 6. Repository layout

```
colab_idle_guard/            Python package
  __main__.py                entry point (tray, or --headless)
  watchdog/                  core: activity, execution, controller, api, config, security, logging
  runtime_adapters/          base.py, notify_only.py, chrome_ui.py, vscode.py, colab_rest_api.py
  desktop/                   tray.py, settings.py, warning_dialog.py
chrome_extension/            MV3 extension
vscode_extension/            TypeScript extension
tests/                       pytest (unit + local HTTP integration)
scripts/                     install.ps1, uninstall.ps1, build.ps1
config/default.json
docs/                        architecture, research, manuals
```
