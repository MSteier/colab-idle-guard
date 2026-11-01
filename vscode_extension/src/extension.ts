// Colab Idle Guard - VS Code extension entry point.
//
// Monitors only notebooks whose kernel is proven to run on Colab, and reports
// activity kinds plus kernel execution state to the local watchdog
// (127.0.0.1, token-authenticated). See docs/ARCHITECTURE.md.

import * as vscode from "vscode";
import { randomUUID } from "node:crypto";
import type { Jupyter } from "@vscode/jupyter-extension";
import { ActivityMonitor } from "./activity_monitor";
import { ExecutionMonitor } from "./execution_monitor";
import type { Command, NotebookReport } from "./protocol";
import { readWatchdogToken } from "./token_reader";
import { WatchdogClient, type TokenSource } from "./watchdog_client";

const SECRET_KEY = "colabIdleGuard.token";
const STATE_DEBOUNCE_MS = 500;
const ACTIVITY_FLUSH_MS = 2000;

let shutdown: (() => Promise<void>) | undefined;

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const log = vscode.window.createOutputChannel("Colab Idle Guard", { log: true });
  context.subscriptions.push(log);
  const config = () => vscode.workspace.getConfiguration("colabIdleGuard");

  const jupyterExt = vscode.extensions.getExtension<Jupyter>("ms-toolsai.jupyter");
  if (!jupyterExt) {
    log.warn("The Jupyter extension (ms-toolsai.jupyter) is not installed; nothing to monitor.");
    return;
  }
  const jupyter = jupyterExt.isActive ? jupyterExt.exports : await jupyterExt.activate();

  // Token: a manually paired token (SecretStorage) wins; otherwise read the
  // watchdog's DPAPI-protected token file, which works without any pairing step.
  let cachedToken: string | undefined;
  const tokens: TokenSource = {
    async get() {
      cachedToken ??= (await context.secrets.get(SECRET_KEY)) ?? (await readWatchdogToken());
      return cachedToken;
    },
    async refresh() {
      cachedToken = (await context.secrets.get(SECRET_KEY)) ?? (await readWatchdogToken());
      return cachedToken;
    },
  };

  // Stable per window/workspace across reloads, so a reloaded window is
  // recognised as the same integration returning rather than a new one.
  let instanceId = context.workspaceState.get<string>("colabIdleGuard.instanceId");
  if (!instanceId || !/^vscode-[0-9a-f]{24}$/.test(instanceId)) {
    instanceId = "vscode-" + randomUUID().replace(/-/g, "").slice(0, 24);
    await context.workspaceState.update("colabIdleGuard.instanceId", instanceId);
  }

  const client = new WatchdogClient({
    port: () => config().get<number>("port", 47821),
    instanceId,
    version: String(context.extension.packageJSON.version),
    tokens,
  });

  const execution = new ExecutionMonitor(jupyter, context.globalState, log);
  const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 50);
  statusBar.command = "colabIdleGuard.showStatus";
  context.subscriptions.push(execution, statusBar);

  const reports = (): NotebookReport[] => execution.trackers().filter((t) => t.monitored).map((t) => t.report());
  const enabled = () => config().get<boolean>("enabled", true);

  // ---------------------------------------------------------------- reporting

  let lastResult = "";
  async function handleCommands(commands: Command[]): Promise<void> {
    for (const command of commands) {
      if (command.type === "release_runtime" && typeof command.payload.notebook_id === "string") {
        const result = await execution.releaseRuntime(command.payload.notebook_id);
        await client.commandResult(command, result.ok, result.detail);
        if (result.ok) {
          void vscode.window.showInformationMessage(
            "Colab Idle Guard released an idle Colab runtime. Your notebook is unchanged; reconnect a kernel to continue.");
        }
      } else {
        await client.commandResult(command, false, `unsupported command: ${command.type}`);
      }
    }
  }

  async function heartbeat(): Promise<void> {
    if (!enabled()) return;
    const commands = await client.heartbeat(reports(), true);
    await handleCommands(commands);
    updateStatusBar();
    if (client.lastResult !== lastResult) {
      lastResult = client.lastResult;
      log.info(`Watchdog connection: ${lastResult}`);
    }
  }

  let stateTimer: NodeJS.Timeout | undefined;
  const scheduleHeartbeat = () => {
    if (stateTimer) clearTimeout(stateTimer);
    stateTimer = setTimeout(() => void heartbeat(), STATE_DEBOUNCE_MS);
  };
  context.subscriptions.push(execution.onDidChange(() => {
    updateStatusBar();
    scheduleHeartbeat();
  }));

  let flushTimer: NodeJS.Timeout | undefined;
  const activity = new ActivityMonitor(execution, (kind) => {
    if (!enabled()) return;
    client.queueActivity(kind);
    flushTimer ??= setTimeout(() => {
      flushTimer = undefined;
      void client.flushActivity();
    }, ACTIVITY_FLUSH_MS);
  });
  context.subscriptions.push(activity);

  const interval = setInterval(() => void heartbeat(), 15_000);
  context.subscriptions.push({ dispose: () => clearInterval(interval) });
  void heartbeat();

  // ------------------------------------------------------------------- status

  function updateStatusBar(): void {
    const monitored = execution.trackers().filter((t) => t.monitored);
    const busy = monitored.filter((t) => t.isBusy()).length;
    if (!enabled()) {
      statusBar.text = "$(shield) Colab Guard: off";
    } else if (client.lastResult === "not_paired" || client.lastResult === "unauthorized") {
      statusBar.text = "$(shield) Colab Guard: not paired";
    } else if (client.lastResult !== "ok") {
      statusBar.text = "$(shield) Colab Guard: app not running";
    } else if (monitored.length === 0) {
      statusBar.text = "$(shield) Colab Guard";
    } else {
      statusBar.text = busy ? `$(sync~spin) Colab Guard: ${busy} executing` : `$(shield) Colab Guard: ${monitored.length} watched`;
    }
    statusBar.tooltip = "Colab Idle Guard - click for status";
    statusBar.show();
  }
  updateStatusBar();

  // ----------------------------------------------------------------- commands

  context.subscriptions.push(
    vscode.commands.registerCommand("colabIdleGuard.pair", async () => {
      const token = await vscode.window.showInputBox({
        title: "Colab Idle Guard pairing token",
        prompt: "Paste the token from the Colab Idle Guard tray menu (Copy pairing token). Leave empty to use automatic pairing.",
        password: true,
        ignoreFocusOut: true,
        validateInput: (v) => (v === "" || /^[A-Za-z0-9_-]{40,100}$/.test(v.trim()) ? undefined : "That does not look like a pairing token."),
      });
      if (token === undefined) return;
      if (token.trim() === "") await context.secrets.delete(SECRET_KEY);
      else await context.secrets.store(SECRET_KEY, token.trim());
      const effective = await tokens.refresh!();
      const test = effective ? await client.test(effective) : { reachable: false, authenticated: false };
      if (!test.reachable) vscode.window.showWarningMessage("Colab Idle Guard app is not reachable. Is it running?");
      else if (!test.authenticated) vscode.window.showErrorMessage("The Colab Idle Guard app rejected the token.");
      else vscode.window.showInformationMessage("Paired with Colab Idle Guard.");
      await heartbeat();
    }),
    vscode.commands.registerCommand("colabIdleGuard.showStatus", async () => {
      await heartbeat();
      const lines = execution.describe().map(
        (n) => `${n.monitored ? "●" : "○"} ${n.name} — Colab: ${n.colab}, kernel: ${n.kernel}`,
      );
      const header = `Watchdog: ${client.lastResult}. Kernel access: ${execution.kernelAccess}.`;
      await vscode.window.showQuickPick([header, ...(lines.length ? lines : ["No Jupyter notebooks open."])], {
        title: "Colab Idle Guard status",
        canPickMany: false,
      });
    }),
    vscode.commands.registerCommand("colabIdleGuard.markColab", async () => {
      const nb = vscode.window.activeNotebookEditor?.notebook;
      if (!nb) {
        void vscode.window.showWarningMessage("Open a notebook first.");
        return;
      }
      await execution.setManualMark(nb, true);
      vscode.window.showInformationMessage("This notebook is now monitored as a Colab notebook.");
    }),
    vscode.commands.registerCommand("colabIdleGuard.unmarkColab", async () => {
      const nb = vscode.window.activeNotebookEditor?.notebook;
      if (!nb) {
        void vscode.window.showWarningMessage("Open a notebook first.");
        return;
      }
      await execution.setManualMark(nb, false);
      vscode.window.showInformationMessage("This notebook is no longer monitored.");
    }),
    vscode.commands.registerCommand("colabIdleGuard.redetect", async () => {
      const nb = vscode.window.activeNotebookEditor?.notebook;
      if (!nb) {
        void vscode.window.showWarningMessage("Open a notebook first.");
        return;
      }
      await execution.setManualMark(nb, undefined);
      execution.redetect(nb);
    }),
  );

  // Graceful close: report final state so the watchdog can drop idle notebooks
  // instead of treating this window as lost.
  // VS Code allows only a few seconds here, so the goodbye goes first. If it
  // never arrives, the stable instance ID lets the watchdog recover anyway.
  shutdown = async () => {
    try {
      await client.goodbye(reports());
    } catch {
      /* best effort */
    }
  };
}

export async function deactivate(): Promise<void> {
  await shutdown?.();
}
