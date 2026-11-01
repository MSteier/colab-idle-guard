# Troubleshooting

First steps for any problem:

1. Open the status window (left-click the tray icon). The red lines at the
   bottom say what is currently blocking a release.
2. **tray → Open logs folder**: `colab_idle_guard.log` (one JSON record per
   line). Set **Log level** to `DEBUG` in Settings for more detail.
3. When asking for help, attach **tray → Export diagnostic report…**. It
   contains status, settings and logs, never your token or notebook content.

## Application

| Problem | Cause / fix |
|---|---|
| "Cannot listen on 127.0.0.1:47821 – is Colab Idle Guard already running?" | Another copy is running (check the tray), or another program uses the port. Change **Local port** in Settings, then also in the Chrome extension settings and the VS Code setting `colabIdleGuard.port`. |
| No tray icon | Look under the **^** arrow on the taskbar. Start it from the Start menu. Drag the icon onto the taskbar to keep it visible. |
| No Windows notifications | Settings → **Show Windows notifications**; Windows *Settings → System → Notifications* must allow Python / Colab Idle Guard; Focus mode suppresses them. |
| Settings: "invalid value" | Timeout 1–1440 min, warning 10–600 s, port 1024–65535. |
| `config.invalid.json` appeared | The settings file was unreadable. Defaults were used and the bad file kept under that name. |
| Doesn't start with Windows | Settings → **Start with Windows**. The entry is updated automatically when the program moves (e.g. after installing the .exe). |

## Chrome extension

| Popup shows | Fix |
|---|---|
| *App not running* | Start Colab Idle Guard. Check the port matches. |
| *Not paired - open Settings* / *Token rejected - re-pair* | tray → **Copy pairing token** → extension settings → paste → **Save and test**. Reinstalling or moving the extension clears its stored token. |
| *Saved, but the extension did not respond* | `chrome://extensions` → reload arrow on the card, then try again. |
| "extension not allowed" in the log | A modified copy of the extension (different ID) is trying to pair while another is pinned: Settings → **Forget**, then pair again. |
| *Colab notebooks: None open* while a notebook is open | Reload the Colab tab (F5). The extension only attaches to tabs loaded after it was installed or updated. |
| Execution *unknown* in a Colab tab | Colab's page may have changed. Report it with a diagnostic report. Release stays blocked (safe). |

## VS Code extension

Run **Colab Idle Guard: Show status** from the Command Palette. It lists
each open notebook, whether it was recognised as Colab, and the kernel status.
The **Colab Idle Guard** output channel has the log.

| Problem | Fix |
|---|---|
| Status bar *Colab Guard: not paired* | The app has not created its token yet (start it once), or you are another Windows user. Manual alternative: **Colab Idle Guard: Pair with Colab Idle Guard app**. |
| Notebook not monitored | A kernel must be running: run a cell. Allow kernel access when asked. If you denied it, re-allow it via the Jupyter extension's kernel-access management, or use **Treat this notebook as a Colab notebook**. Detection retries automatically up to 5 times. |
| "Cannot execute code, session has been disposed" | A Jupyter extension message after a window reload or when Colab reclaimed the server. Select the kernel again: kernel picker → Colab → your server (or a new one). |
| A local notebook is monitored | Run **Stop treating this notebook as a Colab notebook**. |

## Release behaviour

| Problem | Cause / fix |
|---|---|
| Nothing happens after the idle time | Automatic release is off (default): you get a notification only. Enable it in Settings. |
| Status *Runtime state unknown* | An extension stopped reporting while a notebook may have been running, or a notebook closed while busy. Reopen the notebook (resolves it automatically) or, if you are sure nothing runs, tray → **Nothing is running - clear unknown state…**. After 10 minutes you get a notification about it. |
| *Release failed* | See the notification for the reason (e.g. "refused: execution is busy", Colab rate limit, the tab/window was closed). Release manually: Colab → *Runtime → Disconnect and delete runtime*; VS Code → *Colab: Remove Server*. No automatic retry until new activity. |
| Warning appears while I'm using Colab | Your activity isn't reaching the app: check the Chrome popup / VS Code status bar shows *Connected*. Activity while Chrome/VS Code is not the foreground window is ignored by design. |
| Runtime still billed after VS Code closed | A Colab server not attached to an open notebook is invisible to the extension. Use *Colab: Remove Server*. |

## Reset everything

Exit the app, then delete `%APPDATA%\ColabIdleGuard` and
`%LOCALAPPDATA%\ColabIdleGuard` (or `uninstall.ps1 -RemoveUserData` and
reinstall). A new token is created, so re-pair the Chrome extension.
