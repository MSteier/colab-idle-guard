# Installation

## Requirements

- Windows 11 (Windows 10 expected to work, not tested).
- Google Chrome 116+ and/or VS Code 1.95+ with the **Jupyter** and **Google
  Colab** extensions.
- No administrator rights are needed. Everything installs for the current
  Windows user.

## 1. Install from the release ZIP (recommended)

1. Unzip `colab-idle-guard-<version>.zip`. Optionally verify it against
   `colab-idle-guard-<version>.zip.sha256`:
   `Get-FileHash colab-idle-guard-<version>.zip -Algorithm SHA256`.
2. In the unzipped folder run:

   ```powershell
   powershell -ExecutionPolicy Bypass -File install.ps1
   ```

   | Option | Effect |
   |---|---|
   | `-InstallDir <path>` | Install elsewhere (default `%LOCALAPPDATA%\Programs\ColabIdleGuard`) |
   | `-SkipVSCode` | Don't install the VS Code extension |
   | `-NoShortcuts` | No Start-menu shortcuts |
   | `-NoRegister` | No entry in *Settings → Apps* |
   | `-NoLaunch` | Don't start the app afterwards |

   The installer copies the app and the Chrome extension, creates the
   **Colab Idle Guard** and **Colab Idle Guard User Manual** Start-menu
   entries, registers an uninstaller, installs the VS Code extension (when the
   `code` command exists), and starts the app in the tray.

3. **Chrome extension** (cannot be installed by a script):
   1. `chrome://extensions` → turn on **Developer mode**.
   2. **Load unpacked** → choose `%LOCALAPPDATA%\Programs\ColabIdleGuard\chrome_extension`.
      If an older copy loaded from another folder is listed, **Remove** it first.
   3. Tray icon → **Copy pairing token** → in the extension's settings page,
      paste into **Pairing token** → **Save and test** → *"Paired…"*.
   4. Reload open Colab tabs.

4. **VS Code**: reload open windows. On the first Colab notebook, allow
   *Colab Idle Guard* to access Jupyter kernels.

5. **Settings**: tray → **Settings…**. Automatic release is off until you
   enable it there.

## 2. Upgrade

Run the new release's `install.ps1`. It stops the running app, replaces the
program files and restarts it. Your settings
(`%APPDATA%\ColabIdleGuard\config.json`), pairing token and logs
(`%LOCALAPPDATA%\ColabIdleGuard\`) are not touched, so the extensions stay
paired. In Chrome, click the reload arrow on the extension card.

## 3. Uninstall

*Settings → Apps → Colab Idle Guard → Uninstall*, or:

```powershell
powershell -ExecutionPolicy Bypass -File "$env:LOCALAPPDATA\Programs\ColabIdleGuard\uninstall.ps1"
```

| Option | Effect |
|---|---|
| `-RemoveUserData` | Also delete settings, token and logs |
| `-KeepVSCodeExtension` | Leave the VS Code extension installed |

Remove the Chrome extension in `chrome://extensions`.

## 4. Run from source

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pythonw.exe run_colab_idle_guard.pyw          # tray app
.venv\Scripts\python.exe -m colab_idle_guard --headless     # watchdog only, console
```

Load the Chrome extension from the `chrome_extension` folder of the source
tree. Build and install the VS Code extension:

```powershell
cd vscode_extension; npm install; npm run package
code --install-extension colab-idle-guard-<version>.vsix
```

## 5. Build a release

```powershell
.venv\Scripts\pip install -r requirements-dev.txt
cd vscode_extension; npm install; cd ..
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

`build.ps1` runs all tests, then produces in `dist\`: the PyInstaller
one-folder app `ColabIdleGuard\ColabIdleGuard.exe`, the `.vsix`, a Chrome
extension ZIP and the PDF manual, and in `release\` the release ZIP (including
the full source) with its SHA-256. The PDF is rendered with Google Chrome in
headless mode. Switches: `-SkipTests`, `-SkipExe`, `-SkipExtensions`,
`-SkipManual`, `-SkipZip`.

## File locations

| What | Where |
|---|---|
| Program | `%LOCALAPPDATA%\Programs\ColabIdleGuard\` |
| Settings | `%APPDATA%\ColabIdleGuard\config.json` |
| Pairing token (DPAPI-encrypted) | `%LOCALAPPDATA%\ColabIdleGuard\token.bin` |
| Logs (JSON lines, 5 × 1 MB rotation) | `%LOCALAPPDATA%\ColabIdleGuard\logs\` |
| Start with Windows | `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\ColabIdleGuard` |
