# Colab Idle Guard

A Windows 11 tray application that **releases idle Google Colab runtimes**, so
a forgotten GPU session doesn't keep burning compute units, without ever
interrupting a running cell.

It watches Colab in **Google Chrome** and in **VS Code** (Google Colab
extension). After a configurable time with no Colab interaction (default 30
min) and no executing cells, it warns you and — if you enable it — releases
the runtime with Colab's own *Disconnect and delete runtime*.

> **Releasing deletes the runtime's state** (variables, files under
> `/content`). Your notebook is kept. Colab has no gentler way to stop a
> runtime from using compute units. Automatic release is **off by default**.
> See [docs/COLAB_RUNTIME_RESEARCH.md](docs/COLAB_RUNTIME_RESEARCH.md).

## Features

- One idle timer shared by Chrome and VS Code. Only Colab interaction resets
  it; other apps, tabs and files don't. Background windows are excluded by a
  Windows foreground check.
- Running-cell protection from Colab's kernel protocol (Chrome), Colab's page
  (Chrome) and the Jupyter kernel status (VS Code). Missing or uncertain
  information **blocks** release.
- Warning window with countdown and *Keep runtime*; any Colab activity or a
  starting cell cancels it. Every check runs again right before release, and
  success is confirmed independently.
- Tray icon with live status, pause/resume, manual release, settings,
  Windows notifications, logs and a diagnostic report.
- Local only: `127.0.0.1`, token-authenticated (DPAPI-protected), no
  telemetry. Records activity *kinds*, never content.

## Quick start

1. Download and unzip the release, then run
   `powershell -ExecutionPolicy Bypass -File install.ps1`.
2. Chrome: `chrome://extensions` → Developer mode → **Load unpacked** →
   `%LOCALAPPDATA%\Programs\ColabIdleGuard\chrome_extension`. Then tray icon →
   **Copy pairing token** → paste it in the extension's settings → **Save and test**.
3. VS Code: installed by the installer; pairs automatically. Click **Allow**
   when the Jupyter extension asks about kernel access.
4. Optional: tray → **Settings…** → enable automatic release.

Full instructions: [docs/USER_MANUAL.md](docs/USER_MANUAL.md) (also as PDF in the release).

## Documentation

| Document | Contents |
|---|---|
| [docs/USER_MANUAL.md](docs/USER_MANUAL.md) | Using the application (also `Colab-Idle-Guard-User-Manual.pdf`) |
| [docs/INSTALLATION.md](docs/INSTALLATION.md) | Install, upgrade, uninstall, run from source, build |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Problems and fixes |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, data handling, privacy |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components and design decisions |
| [docs/COLAB_RUNTIME_RESEARCH.md](docs/COLAB_RUNTIME_RESEARCH.md) | What Colab supports, and live validation |
| [docs/TEST_REPORT.md](docs/TEST_REPORT.md) | Test coverage and results |
| [CHANGELOG.md](CHANGELOG.md) | Changes |

## Project layout

```
colab_idle_guard/      Python application
  watchdog/            core: activity, execution state, idle/release state machine, local API
  runtime_adapters/    release mechanisms (notify-only, Chrome, VS Code)
  desktop/             tray icon, status/warning/settings windows
chrome_extension/      Manifest V3 extension
vscode_extension/      VS Code extension (TypeScript)
tests/                 pytest suites; tests/js: Chrome extension tests (node --test)
scripts/               install.ps1, uninstall.ps1, build.ps1, manual and icon builders
config/default.json    default settings
docs/                  documentation
```

## Development

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q                 # Python tests
node --test "tests/js/*.test.*"                   # Chrome extension tests
cd vscode_extension; npm install; npm test        # VS Code extension tests
powershell -ExecutionPolicy Bypass -File scripts\build.ps1   # exe, extensions, PDF, release ZIP
```

Run from source: `.venv\Scripts\pythonw.exe run_colab_idle_guard.pyw`
(`python -m colab_idle_guard --headless` runs the watchdog without the tray).
