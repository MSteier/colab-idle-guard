# Google Colab Runtime Management — Research Findings

Research date: 2026-10-31. Phase 1 deliverable. Everything here is either
quoted from official Google documentation or observed directly in locally
installed software. Anything unverified is marked **UNVERIFIED** and must be
tested before code depends on it.

## 1. Summary (read this first)

1. **Colab has no nondestructive "disconnect" that stops compute billing.**
   Compute is used by the *runtime assignment* (the VM assigned to your
   account). The only documented ways to end an assignment are
   *Runtime → Disconnect and delete runtime* (browser), *Colab: Remove Server*
   (VS Code), and `runtimes.delete` (REST API). All three **delete** the VM
   assignment, which destroys in-memory kernel state (variables, loaded
   models) and files written to the VM's local disk (e.g. `/content`).
2. **What survives:** the notebook document itself (in Google Drive for
   browser Colab, or the local `.ipynb` file in VS Code), Google Drive
   contents, and anything written to Drive or other external storage.
3. **Closing the browser tab or VS Code does *not* release the runtime.**
   The VM stays assigned until Colab's own idle timeout or maximum lifetime.
   That gap is the problem this application exists to solve.
4. **The official REST API is allowlist-only beta** and exposes no
   execution/idle/busy state, so it cannot be our default path.
5. **Consequence for the design:** "disconnect" in this application means
   *release the runtime assignment*. It is treated as a destructive action
   (VM state is lost) and is never performed without explicit opt-in
   (`automatic_disconnect_enabled`, off by default) plus all safety checks.
   The default behaviour is **notify only**.

## 2. Terminology — the actions are not equivalent

| Action | Where | Stops compute usage? | Loses kernel memory? | Loses VM disk (`/content`)? | Notebook file kept? |
|---|---|---|---|---|---|
| Close tab / close VS Code | Either | **No** — VM remains assigned until Colab's idle timeout | Not immediately | Not immediately | Yes |
| Interrupt execution | Either | No | No | No | Yes |
| Restart session / restart kernel | Either | No | **Yes** | No | Yes |
| Shut down a Jupyter session/kernel (`delete_session` in API) | API / VS Code kernel shutdown | **No** — VM stays assigned | Yes | No | Yes |
| *Disconnect and delete runtime* | Browser menu | **Yes** | Yes | **Yes** | Yes |
| *Colab: Remove Server* | VS Code | **Yes** (calls `deleteSessions` then `deleteRuntime`) | Yes | **Yes** | Yes |
| `runtimes.delete` | REST API (allowlisted) | **Yes** | Yes | **Yes** | Yes |
| Terminate the underlying cloud instance | Not exposed to users | n/a | n/a | n/a | n/a |

"Stopping" a runtime in a way that keeps it resumable (as Colab Enterprise /
Vertex AI notebook runtimes allow) **does not exist for consumer Colab**. Colab
Enterprise is a separate product with a separate API and is out of scope for
v1; the adapter interface leaves room for it.

## 3. Evidence

### 3.1 Colab FAQ (official)
Source: <https://research.google.com/colaboratory/faq.html>

- "Virtual machines are deleted when idle for a while, and have a maximum
  lifetime enforced by the Colab service." Idle and lifetime limits "vary
  over time" and are not published.
- *Disconnect and delete runtime* is documented as returning "all managed
  virtual machines assigned to you to their original state", and "Colab
  limits how often this can be done". **Implication:** releases can fail when
  rate-limited, so the result must be verified, never assumed.
- The FAQ recommends closing Colab tabs when work is done and avoiding a GPU
  when it is not needed. It documents no API for managing runtimes.

### 3.2 Colab REST API (official, beta)
Source: <https://developers.google.com/colab/api/reference/rest>

- Endpoint `https://colaboratory.googleapis.com`. Methods: `runtimes.create`,
  `runtimes.delete`, `runtimes.get`, `runtimes.list`, `runtimespecs.list`,
  `getSubscription`, and long-running `operations.*`.
- **No stop, disconnect, or release method** other than `delete`.
- `runtimes.delete` ("Deletes a Colab runtime assignment") takes
  `allowMissing`; its documentation says "Avoid deleting runtimes unless
  necessary." Scope: `https://www.googleapis.com/auth/colaboratory`.
  <https://developers.google.com/colab/api/reference/rest/v1beta/runtimes/delete>
- The `Runtime` resource has only `name`, `runtimeSpec` (variant, accelerator,
  shape), `connectionInfo` (proxy URL plus a short-lived token), and
  `version`. **It has no state, last-activity, or busy field.**
  <https://developers.google.com/colab/api/reference/rest/v1beta/runtimes>
- Access is allowlist-only beta (use cases are submitted through a Google form).
  A personal desktop tool cannot count on getting access.
- The MCP tool reference for the same API describes `delete_runtime` as
  permanently removing "the runtime and its associated state", and points to
  `delete_session` for lighter cleanup.
  <https://developers.google.com/colab/api/reference/mcp/tools_list/delete_runtime>

### 3.3 Google Colab VS Code extension (`google.colab` 0.9.7, inspected locally)
Path: `%USERPROFILE%\.vscode\extensions\google.colab-0.9.7`

- Commands: `colab.removeServer`, `colab.mountServer`, `colab.mountDrive`,
  `colab.openTerminal`, `colab.upload`, `colab.signOut`, plus file commands.
  None of them disconnects without deleting.
- `colab.removeServer` → `unassignServer()` → `deleteSessions()` +
  `deleteRuntime()`. It **always opens an interactive QuickPick** to choose the
  server, so it cannot safely be invoked headlessly.
- `activate()` returns `undefined`: **the extension exports no API** that other
  extensions can call.
- It registers a Jupyter server collection with
  `createJupyterServerCollection("colab", "Colab", …)` through
  `ms-toolsai.jupyter`. This is the hook for telling a Colab-backed notebook
  apart from a local Jupyter kernel (see 4.2). **UNVERIFIED:** which kernel
  or controller identifier carries this collection ID at runtime.
- User guide: <https://github.com/googlecolab/colab-vscode/wiki/User-Guide>

### 3.4 Jupyter extension public API (`@vscode/jupyter-extension` 1.1.1 typings)
- `jupyter.kernels.getKernel(uri: Uri): Thenable<Kernel | undefined>`
- `Kernel.status: KernelStatus` (`'idle' | 'busy' | 'starting' | …`) and
  `Kernel.onDidChangeStatus`.
- `Kernel.executeCode(code, token)`.
- **This is the most reliable execution-state source we have:** it reports
  the kernel's own busy/idle state, independent of notebook UI events. It
  serves as the "verify actual runtime state" check in VS Code. Access may
  require the user to approve the calling extension (**UNVERIFIED**, test in
  Phase 4).

## 4. Execution-state sources, ranked by reliability

### 4.1 Browser (colab.research.google.com)
Colab publishes no API for its frontend state. Candidates, to be checked
against a live **CPU-only** runtime in Phase 3:

1. Per-cell running indicators in the DOM (the run button's running/stop
   state, and the "executing" status in the toolbar).
2. The page title and favicon, which Colab updates during execution
   (**UNVERIFIED**).
3. Connection indicator (RAM/Disk widget vs. "Connect" button) for
   *runtime connected* state.

Design rule: several independent signals are combined, and **if the signals
cannot be found (e.g. after a Colab UI redesign) the integration reports
`UNKNOWN`, which blocks shutdown**. Fragile selectors live in one module and
fail safe.

### 4.2 VS Code
1. `jupyter.kernels.getKernel(notebook.uri).status`. This is runtime-side and
   reliable.
2. `NotebookCellExecutionSummary` / `onDidChangeNotebookDocument`
   (`executionSummary.success` / `timing`) as corroboration.
3. Colab association: the notebook's kernel comes from the `colab` server
   collection. If the association cannot be shown, the notebook is **not**
   monitored as Colab. Only proven-Colab notebooks reset the timer.

## 5. Release ("disconnect") mechanisms we can actually implement

| Adapter | Mechanism | Status | Destructive to VM state? |
|---|---|---|---|
| `NotifyOnlyAdapter` (**default**) | Windows notification explaining how to release manually | Safe | No |
| `ChromeColabUiAdapter` | Extension performs the user-facing *Runtime → Disconnect and delete runtime* command in the Colab tab, then checks that the page shows "disconnected" | **Implemented and validated (§9.1)** | **Yes** |
| `VSCodeColabUnassignAdapter` | `google.colab.runtime.unassign()` through the Jupyter `Kernel.executeCode` API (`removeServer` is interactive) | **Implemented and validated (§9.2)** | **Yes** |
| `ColabRestApiAdapter` | `runtimes.list` + `runtimes.delete` | Interface stub only; needs allowlist access plus an OAuth client | **Yes** |

Each destructive adapter requires all of the following:
`automatic_disconnect_enabled = true` (off by default), a one-time
acknowledgement in Settings that VM state will be lost, every safety check
passing at the recheck right before release, and a post-release state check.
A failed or unverifiable release is logged and reported to the user, never
retried blindly.

## 6. Open questions to resolve in later phases

- Q1 (Phase 3): **Answered, see §7.**
- Q2 (Phase 4): **Answered, see §8.** No API exposes the server, so the kernel
  is identified by its `COLAB_RELEASE_TAG` environment variable.
- Q3 (Phase 4): **Answered, see §8.** Yes, the user grants consent once, and
  the status is accurate.
- Q4 (Phase 6): **Answered, see §9.1.**
- Q5 (Phase 6): **Answered, see §9.2.** Yes, it works.
- Q6: **Answered.** Releasing does not touch the notebook document. Browser
  Colab keeps the page and autosaves to Drive; VS Code keeps the editor
  contents, saved or not. Only VM state is lost. No dirty-notebook check is
  needed.

## 7. Live validation of browser signals (Phase 3, 2026-10-31)

Environment: Chrome on Windows 11, `colab.research.google.com/notebooks/empty.ipynb`
(scratchpad, not saved to Drive). The runtime type was checked as **CPU**
before connecting, and the Runtime menu was not used. The observer ran the
exact logic of `content/kernel_hook.js` and `content/colab_probe.js`.

### 7.1 Kernel WebSocket (`kernel_ws` provider): validated
- Colab opens `wss://<host>.colab.dev/api/kernels/<id>/channels` (plus a
  separate `/colab/lsp` socket for the language server, which is ignored).
  No WebSocket subprotocol is used, and frames are **JSON strings** (100%
  decoded, 0 undecodable).
- Standard Jupyter messages are used: `execute_request` out, then
  `status busy/idle` and `execute_reply` with `parent_header.msg_id` in.
- On connect, Colab's frontend runs its own setup `execute_request`, which
  completes normally.

| Scenario | Hook pending count | Correct? |
|---|---|---|
| Idle, connected | 0 | ✔ |
| One 25 s cell running | 1 → 0 when finished | ✔ |
| Cell 2 queued by Ctrl+Enter while cell 1 runs | **1** (cell 2's request is held client-side until cell 1 finishes) | Partial: covered by the DOM provider |
| Run all (2 cells) | 2 → 0 | ✔ |
| Interrupt (Ctrl+M I) | 1 → 0 via `execute_reply` | ✔ |

### 7.2 DOM (`dom` provider): validated
- Cells: `.notebook-cell-list > div.cell.code`.
- Executing cell: class `running`. Queued cell: class `pending`. Both disappear
  when the cell finishes or is interrupted. (The run button's shadow-DOM
  `div.cell-execution` carries the same classes; it isn't used because the
  light-DOM classes are enough.)
- Connected: `colab-connect-button` → shadow `#connect[tooltiptext^="Connected to"]`
  (the tooltip also contains RAM/Disk figures, which are not read).
- Never connected: the shadow text of the same button is `Connect`.
  **Not yet observed:** the state right after a runtime release. This is
  checked in Phase 6.
- The favicon switches to `favicon_busy.ico` while executing. It is a third
  corroborating signal that isn't used yet.

### 7.3 Combined verdict
Over a 36 s sampled "Run all" the verdict was `busy/2/connected` the whole
time, then `idle/0/connected`, with no false idle between cells. Rules:
busy if *any* provider shows work; idle only if a validated provider shows
none and no socket is orphaned with outstanding executions. A closed kernel
socket is never taken as proof of disconnection.

### 7.4 Residual risks
- A Colab redesign that renames `running`/`pending` leaves the DOM provider
  showing 0. The kernel provider still sees executions that have been sent,
  but not cells queued client-side. Mitigation: `PROBE_VERSION` is shown in
  diagnostics, and the validation steps here should be repeated after any
  visible Colab UI change.
- If the extension is installed or reloaded while a Colab tab is open, the
  MAIN-world hook only attaches after a page reload. Until then only the DOM
  provider is active.

### 7.5 End-to-end check with the installed extension (2026-10-31)
The unpacked extension was paired with the watchdog in the user's Chrome,
on a CPU scratchpad runtime.
- Page load: the kernel hook attached at `document_start`, and the watchdog
  showed `connected / idle`.
- 40 s cell: `Notebook executing` for the whole run, then `Active`. The idle
  timer restarted at the end of execution (ADR-9).
- Automated input while the terminal was the foreground window was rejected
  (`app_not_foreground`). Real user scrolling and clicking reset the timer.
- 2.5 min with the tab idle: steady `idle / connected`, no blockers.
- Closing the idle tab removed the notebook with no stale entry.
- Found and fixed during this check: (1) the options page hung on "Testing…"
  because extension pages opened in a tab were mistaken for Colab tabs;
  (2) pages reported before a notebook was recognised created permanent
  "closed while unknown" blocks; (3) the 25 s silent-tab expiry was shorter
  than Chrome's hidden-tab timer throttling (now 150 s; closed tabs are
  still removed immediately via `tabs.onRemoved`).
- Noted for Phase 6: while Colab reconnects, the toolbar briefly shows
  "Connect", so the page reads as `disconnected` for about 2 s. Release
  confirmation must require the disconnected state to persist.

## 8. VS Code validation (Phase 4, 2026-10-31)

Environment: VS Code 1.144, `ms-toolsai.jupyter` 2025.9.1, `google.colab`
0.9.7, and a new **CPU** Colab server assigned through the Colab extension.

### 8.1 Identifying Colab kernels
- `@vscode/jupyter-extension` 1.1.1 `Kernel` exposes `status`,
  `onDidChangeStatus`, `language` and `executeCode` only. It has **no server
  or connection info**. The Colab extension exports no API and writes no
  notebook metadata; its `colab.hasAssignedServer` context key cannot be read
  by other extensions.
- So the extension asks the kernel itself. A read-only one-liner, run once per
  kernel session while the kernel is idle, prints whether `COLAB_RELEASE_TAG`
  is set. On the VS Code-assigned runtime it printed
  `release-colab-external-images_20261029-060149_RC00`, and the extension
  logged "Colab runtime".
- The Jupyter extension asked the user to **Allow** kernel access on first
  use. If the user denies it, nothing is monitored automatically, and
  notebooks can be marked manually.

### 8.2 Execution state
- A 40 s cell was shown as `Notebook executing` for its whole duration (plus
  the 3 s settle window), then `Active`.
- Typing makes the kernel briefly busy (completions and hover help). Those
  blips correctly block release but no longer restart the idle timer: only
  executions of 10 s or more do (`MIN_EXECUTION_FOR_IDLE_RESET`).
- **Untouched for 4 min: no kernel busy events at all.** Nothing polls the
  kernel in the background, and idle time grew steadily into `Idle countdown`.

### 8.3 Activity and pairing
- Typing in a Colab notebook cell reset the idle timer. Using other apps did
  not.
- Automatic pairing: the extension decrypted the watchdog's DPAPI token file
  (same Windows user), with no manual step.

### 8.4 Limitations
- A Colab server can stay assigned after its notebook switches to another
  kernel or is closed (VS Code keeps servers until *Remove Server*). The
  extension then stops reporting that notebook, so the watchdog cannot see
  that runtime. This is documented for users.
- While a cell runs, the stable VS Code API gives no per-cell "running" state;
  the kernel's busy/idle status is used instead, which covers queued cells
  through the settle window.

## 9. Release mechanisms, validated (Phase 6, 2026-10-31)

All tests ran on **CPU** runtimes only. The runtime type was confirmed from
the "Python 3 Google Compute Engine backend" tooltip (no GPU) before each
release.

### 9.1 Chrome: Runtime → Disconnect and delete runtime
- The menu bar uses Google Closure (`#runtime-menu-button`). Script-dispatched
  pointer events (pointerdown/mousedown/pointerup/mouseup/click) open the
  menu and activate items; a bare `mouseup` does not.
- Item: `.goog-menuitem[command="powerwash-current-vm"]`, a stable command ID.
- Confirmation: `mwc-dialog` titled "Disconnect and delete runtime" ("The
  state of this runtime, including all local variables and files, will be
  lost."), with buttons `[dialogaction="ok"]` (Yes) and `[dialogaction="cancel"]`.
- Afterwards the toolbar shows **"Reconnect"** with tooltip "Click to
  connect", steady for 40 s or more. The notebook stays open and unchanged.
  Before any connection the button reads "Connect" ("Connect to a new
  runtime"). The probe treats both as disconnected.
- End-to-end automatic run: idle → 20 s warning → user cancel → idle →
  warning → extension release (16 s, including its own 5 s stability check)
  → watchdog confirmation from an independent later report → `Disconnected`.

### 9.2 VS Code: google.colab.runtime.unassign()
- Source (googlecolab/colabtools `google/colab/runtime.py`): POSTs to the VM's
  runtime service `TBE_RUNTIME_ADDR/unassign`, then calls
  `output.eval_js("google.colab.kernel.disconnect()")` for a browser
  frontend. VS Code has no such frontend, so the adapter swaps `eval_js` for a
  no-op during the call (restored in `finally`) and runs it in a fresh `exec`
  namespace.
- Result: the marker printed about 1.6 s after the request (the POST returned
  200). The watchdog confirmed the release after its 5 s stability window,
  and the Jupyter kernel connection ended about a minute later as the VM went
  away.

### 9.3 Notes
- Colab rate-limits *Disconnect and delete runtime* (FAQ). A refused or failed
  release is reported, never retried automatically.
- After a VS Code window reload the Jupyter extension may report "session has
  been disposed". A Colab server idle for about 45 min had been reclaimed by
  Colab; the extension then correctly classified the replacement local kernel
  as "not Colab".
