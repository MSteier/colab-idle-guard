# Colab Idle Guard — User Manual

Version 0.1.0 · Windows 11

## 1. What Colab Idle Guard does

Google Colab keeps a runtime (the virtual machine, often with a GPU, that
runs your notebook) assigned to you until you explicitly release it or until
Colab's own idle timeout, which can be much later. **Closing the browser tab
or VS Code does not release it**, and a paid runtime keeps consuming compute
units while it sits idle.

Colab Idle Guard watches how you use Colab in **Google Chrome** and in **VS
Code** (with the Google Colab extension). When neither has been used for a
configurable time (default **30 minutes**) and **no notebook cell is
running**, it warns you and, if you have switched automatic release on,
releases the idle runtime for you.

What makes it different from a simple timer:

- **Only Colab activity counts.** Using other programs, other websites or
  other VS Code files does not keep a runtime alive.
- **Running cells are always protected.** A runtime is never released while
  a cell is executing, or while Colab Idle Guard cannot tell for sure.
- **You stay in control.** Automatic release is **off** until you turn it
  on, and every release is preceded by a warning (default 60 seconds) that
  you can cancel.

### 1.1 What "release" means — please read

Colab offers no way to stop a runtime from using compute units without
**deleting** it. Releasing a runtime is the same as choosing *Runtime →
Disconnect and delete runtime* in Colab (or *Colab: Remove Server* in VS
Code):

| Lost | Kept |
|---|---|
| Variables and objects in memory (e.g. loaded models, data frames) | Your notebook (in Google Drive, or your local `.ipynb` file in VS Code) |
| Files written to the runtime's disk, e.g. under `/content` | Everything saved to Google Drive or other external storage |
| Installed packages (re-run your setup cells next time) | Cell outputs already shown in the notebook |

**Save anything you need (models, results) to Drive or download it before
leaving a runtime idle.**

## 2. System requirements

| Component | Requirement |
|---|---|
| Operating system | Windows 11 (Windows 10 expected to work; not tested) |
| Google Chrome | Version 116 or later (for Colab in the browser) |
| VS Code | 1.95 or later, with the **Jupyter** extension and Google's **Google Colab** extension (for Colab in VS Code) |
| Python | Only if you run from source: Python 3.12 |
| Network | None beyond what Colab needs; Colab Idle Guard talks only to `127.0.0.1` on your computer |

You can use Chrome only, VS Code only, or both.

## 3. Installation

### 3.1 Install the application

1. Unzip `colab-idle-guard-0.1.0.zip` anywhere (for example your Downloads folder).
2. Right-click `install.ps1` → **Run with PowerShell**, or open PowerShell in
   that folder and run:

    ```powershell
    powershell -ExecutionPolicy Bypass -File install.ps1
    ```

The installer works for your Windows account only and needs no
administrator rights. It installs to
`%LOCALAPPDATA%\Programs\ColabIdleGuard`, adds **Colab Idle Guard** to the
Start menu and to *Settings → Apps*, installs the VS Code extension if VS Code
is installed, and starts the application. Its icon appears in the system
tray (bottom right of the taskbar; it may be hidden under the **^** arrow).

### 3.2 Python setup (only when running from source)

The installed application includes everything it needs. To run from the
source code instead:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\pythonw.exe run_colab_idle_guard.pyw
```

### 3.3 Chrome extension

Chrome only installs extensions from outside the Chrome Web Store through
**Developer mode**:

1. In Chrome, open `chrome://extensions`.
2. Turn on **Developer mode** (switch at the top right).
3. Click **Load unpacked** and choose the folder
   `%LOCALAPPDATA%\Programs\ColabIdleGuard\chrome_extension`
   (in the folder dialog, paste that path into its address bar and press Enter,
   then click **Select Folder**).
4. The **Colab Idle Guard** card appears, and its settings page opens.
5. Pair it (section 4), then reload any open Colab tabs.

Optional: click Chrome's puzzle-piece icon and pin Colab Idle Guard to see
its status popup.

### 3.4 VS Code extension

The installer installs it automatically when the `code` command is
available. Otherwise: in VS Code open **Extensions**, click **…** →
**Install from VSIX…**, and choose `colab-idle-guard-0.1.0.vsix` from the
release folder. Reload VS Code windows afterwards.

The first time a Colab notebook runs, the Jupyter extension asks whether
**Colab Idle Guard may access Jupyter kernels** — click **Allow** (see 5.3).

## 4. Pairing (token configuration)

The application and the extensions share a secret **pairing token** so that
no other program or website can send it fake activity or commands.

- **VS Code** pairs automatically. It reads the token from the
  application's encrypted token file, which only your Windows account can
  decrypt. No action needed.
- **Chrome** must be paired once:
    1. Right-click the tray icon → **Copy pairing token**.
    2. On the extension's settings page (`chrome://extensions` → Colab Idle
       Guard → **Details** → **Extension options**), click the **Pairing token**
       box and press **Ctrl+V**.
    3. Click **Save and test**. You should see *"Paired with Colab Idle Guard
       0.1.0."*

Only the official Colab Idle Guard extension is accepted. It has a fixed ID
(`glchpgialnkmebkjemimjgfijpnkdcdi`) whichever folder you load it from. If you
load a modified copy, which gets a different ID, the first one to pair is
remembered and others are refused; **tray → Settings… → Forget** lets another
one pair. Reinstalling or moving the extension clears its stored token, so
paste the token again (steps 1–3).

## 5. Runtime monitoring

### 5.1 What counts as Colab activity

| Where | Counts | Does not count |
|---|---|---|
| Chrome | Mouse movement, clicks, keys, scrolling and edits **inside a Colab notebook tab**, while that tab is visible and Chrome is the active window | Other tabs and websites; a mouse passing over a Chrome window that is not in front; anything outside Chrome |
| VS Code | Clicking, scrolling, typing and editing **in a notebook verified to run on Colab**, while VS Code is the active window | Local Jupyter notebooks; other files; output streaming in by itself |

As an extra safeguard, Colab Idle Guard checks which program is in the
foreground (it does not record keystrokes or mouse positions) and ignores
activity reported while neither Chrome nor VS Code is in front.

Only the **kind** of activity is recorded (for example "click"), never what
you type, your code or your outputs.

### 5.2 How running cells are detected

- **Chrome:** from Colab's own communication with its kernel (each cell run
  is a Jupyter "execute request" that ends with a reply) and from the
  notebook page, which marks running and queued cells. Either source showing
  work means *busy*.
- **VS Code:** from the kernel's own busy/idle status, reported by the
  Jupyter extension. Idle only counts once it has lasted 3 seconds, which
  covers the short gaps between queued cells.

If the state cannot be determined — an extension stops reporting, a tab or
VS Code window closes while a cell was running, or Colab's page changes so
that the signals are not found — the state is **unknown**, and unknown
**blocks** any release until it is resolved.

### 5.3 How VS Code notebooks are recognised as Colab

VS Code does not tell other extensions which server a kernel runs on. Once
per kernel session, while the kernel is idle, the extension runs this
read-only line in the kernel:

```python
print("CIG_COLAB=" + ("1" if __import__("os").environ.get("COLAB_RELEASE_TAG") else "0"))
```

It only reads an environment variable that Colab runtimes set; it creates no
variables or files and does not appear in your notebook. Notebooks on local
or other remote kernels are then ignored. If you prefer no probe, set
**Colab Idle Guard › Detection** to `manual` in VS Code's settings and use the
command **Colab Idle Guard: Treat this notebook as a Colab notebook**.

### 5.4 The tray icon and status window

Left-click the tray icon to open the status window; right-click for the menu.

| Icon / status | Meaning |
|---|---|
| Grey — *Monitoring* | Waiting; no Colab notebook is reporting a runtime |
| Green — *Active Colab session* | Colab used within the last minute |
| Blue ▶ — *Notebook executing* | A cell is running; release is impossible |
| Amber — *Idle - release check in mm:ss* | No Colab activity; counting down |
| Red ! — *Releasing runtime in mm:ss* | Warning period; you can cancel |
| Purple — *Disconnecting* | Release in progress |
| Dark — *Disconnected* | No runtime connected (nothing to do) |
| Orange ? — *Runtime state unknown* | Cannot verify execution state; release blocked |
| Amber ! — *Idle - release the runtime manually* | Idle limit reached; automatic release is off |
| Red ✕ — *Release failed* | A release did not succeed; see the notification |
| Grey ❚❚ — *Monitoring paused* | You paused monitoring |

**Tray menu:**

- **Open status window** — runtime, cells, which integrations are connected,
  idle time, mode, and anything currently blocking a release.
- **Pause monitoring / Resume monitoring** — while paused nothing is
  released; resuming starts a full timeout again.
- **Cancel pending release** — during a warning or a queued release.
- **Release runtime now…** — releases immediately after confirmation (still
  refused while a cell runs or state is unknown).
- **Nothing is running - clear unknown state…** — appears only when state is
  unknown; confirm only if you are sure no cell is still running.
- **Copy pairing token**, **Settings…**, **Open logs folder**,
  **Export diagnostic report…**, **Exit**.

Closing the status window hides it; Colab Idle Guard keeps running in the
tray. Use **Exit** to stop it.

## 6. Inactivity settings

Open **tray → Settings…**:

| Setting | Default | Notes |
|---|---|---|
| Idle timeout (minutes) | 30 | 1–1440. Time without Colab activity before the warning |
| Warning countdown (seconds) | 60 | 10–600. Time you get to cancel |
| Monitor Colab in Chrome / in VS Code | On | Turn off to ignore one of them entirely |
| Release idle runtimes automatically | **Off** | Requires confirming a consent dialog |
| Show Windows notifications | On | |
| Start with Windows | Off | Starts minimised to the tray when you sign in |
| Log level | INFO | DEBUG for troubleshooting |
| Local port | 47821 | Change only if another program uses it; restart required |

Always on and not configurable: never release while a cell runs, and never
release while execution state cannot be verified.

The idle timer also restarts when a long execution (10 seconds or more)
finishes, so a long unattended training run gets a full timeout afterwards to
let you save its results.

Settings are stored in `%APPDATA%\ColabIdleGuard\config.json` and are kept
when you upgrade or reinstall.

## 7. Shutdown behaviour

When Colab has been idle for the timeout:

1. Colab Idle Guard checks that a runtime is connected, that no cell is
   running, that every integration's information is current, and that
   nothing is in an unknown state.
2. **Automatic release off (default):** you get one Windows notification
   per idle period telling you the runtime is idle and how to release it.
   Nothing else happens.
3. **Automatic release on:** a warning window appears on top of other
   windows with a countdown. **Keep runtime (cancel)**, closing the window,
   pressing Esc, or any Colab interaction cancels it and restarts the timer.
   A cell starting also cancels it.
4. When the countdown ends, every check is repeated. The release request goes
   to the extension that shows the runtime, which checks once more on its
   own (Chrome: the page; VS Code: the kernel) immediately before acting:
    - **Chrome:** uses Colab's *Runtime → Disconnect and delete runtime* in the tab.
    - **VS Code:** calls Colab's `google.colab.runtime.unassign()` in the kernel.
5. Success is only reported once the runtime is seen disconnected for 5
   seconds in a row. You get a notification either way.
6. A failed release is **not retried automatically** until there has been
   new Colab activity. Release it by hand (see 1.1).

Releases cannot happen when the extension showing the runtime is not
connected (for example Chrome is closed). Colab's own idle timeout still
applies in that case.

## 8. How to disable automatic shutdown

Any of these stops automatic releases:

- **tray → Settings… →** untick **Release idle runtimes automatically** →
  **Save** (you then only get notifications), or
- **tray → Pause monitoring** (temporary; until you resume), or
- **tray → Exit** (nothing is monitored until you start it again).

To stop monitoring only one side, untick **Monitor Colab in Chrome** or
**Monitor Colab notebooks in VS Code**.

## 9. Troubleshooting (summary)

| Symptom | What to do |
|---|---|
| Chrome popup: *App not running* | Start Colab Idle Guard from the Start menu |
| Chrome popup: *Not paired* / *Token rejected* | Pair again (section 4) |
| Colab tab not detected | Reload the tab (F5) after installing or updating the extension |
| VS Code notebook not monitored | Run a cell so the kernel starts; allow kernel access; check **Colab Idle Guard: Show status** |
| Status *Runtime state unknown* for a long time | Reopen the notebook that was closed while running, or use *Nothing is running - clear unknown state…* if you are sure |
| *Release failed* | Release manually (Colab: *Runtime → Disconnect and delete runtime*; VS Code: *Colab: Remove Server*) |

Full guide: TROUBLESHOOTING.md. To ask for help, use **tray → Export
diagnostic report…** and include the ZIP (it contains no token or notebook
content).

## 10. Security and privacy

- Works entirely on your computer. Nothing is sent to any server other than
  what Chrome, VS Code and Colab already do.
- The application listens only on `127.0.0.1` (not reachable from the
  network), requires the pairing token on every request, refuses requests
  from web pages, and accepts only your paired Chrome extension.
- The token is encrypted with Windows DPAPI for your user account and never
  written to logs or diagnostic reports.
- Recorded: activity *kinds*, execution states, counts and anonymised
  notebook IDs (a one-way hash of the notebook address). Never recorded:
  keystrokes, text, code, outputs, file names or notebook titles.

Details: SECURITY.md.

## 11. Uninstalling

Run **Settings → Apps → Colab Idle Guard → Uninstall** (or
`uninstall.ps1` in the installation folder). Your settings are kept unless
you run `uninstall.ps1 -RemoveUserData`. Remove the Chrome extension yourself
in `chrome://extensions`.

## 12. Known limitations

- A Colab server in VS Code that is no longer attached to any open notebook
  (e.g. you switched the notebook to another kernel) cannot be seen; use
  *Colab: Remove Server* to release it.
- Releasing requires the Chrome tab or VS Code window showing the runtime to
  be open and connected.
- Colab limits how often *Disconnect and delete runtime* may be used; a
  refusal shows up as *Release failed*.
- Activity inside interactive output widgets (which Colab runs in separate
  frames) is not counted; normal notebook use is.
- Colab may change its web page. If its signals are no longer found, Colab
  Idle Guard reports *unknown* and does not release (safe), until updated.
