// Tracks open Jupyter notebooks, their kernels (via the official Jupyter
// extension API) and whether each kernel is a Colab kernel.

import * as vscode from "vscode";
import type { Jupyter, Kernel } from "@vscode/jupyter-extension";
import { probeKernel } from "./colab_detector";
import { parseUnassignOutput, UNASSIGN_CODE } from "./probe";
import { notebookId } from "./protocol";
import { NotebookTracker, type KernelStatus } from "./state";

const KERNEL_POLL_MS = 5000;
const PROBE_RETRY_MS = 20_000; // the first probe can fail while a new kernel is still starting
const MAX_PROBE_ATTEMPTS = 5;
const MANUAL_KEY = "colabIdleGuard.manualMarks";

interface Entry {
  doc: vscode.NotebookDocument;
  tracker: NotebookTracker;
  kernel: Kernel | undefined;
  statusSub: vscode.Disposable | undefined;
  probeAttempts: number;
  nextProbeAt: number;
  probing: boolean;
}

export class ExecutionMonitor implements vscode.Disposable {
  private readonly entries = new Map<string, Entry>();
  private readonly disposables: vscode.Disposable[] = [];
  private timer: NodeJS.Timeout | undefined;
  private readonly changed = new vscode.EventEmitter<void>();
  /** Fires when any reported state may have changed. */
  readonly onDidChange = this.changed.event;
  kernelAccess: "unknown" | "granted" | "denied" = "unknown";

  constructor(
    private readonly jupyter: Jupyter,
    private readonly state: vscode.Memento,
    private readonly log: vscode.LogOutputChannel,
  ) {
    for (const doc of vscode.workspace.notebookDocuments) this.open(doc);
    this.disposables.push(
      vscode.workspace.onDidOpenNotebookDocument((d) => this.open(d)),
      vscode.workspace.onDidCloseNotebookDocument((d) => this.close(d)),
    );
    this.timer = setInterval(() => void this.pollKernels(), KERNEL_POLL_MS);
    void this.pollKernels();
  }

  /** Tracker for a document, if it is a tracked Jupyter notebook. */
  trackerFor(doc: vscode.NotebookDocument): NotebookTracker | undefined {
    return this.entries.get(doc.uri.toString())?.tracker;
  }

  trackers(): NotebookTracker[] {
    return [...this.entries.values()].map((e) => e.tracker);
  }

  describe(): { name: string; colab: string; kernel: string; monitored: boolean }[] {
    return [...this.entries.values()].map((e) => ({
      name: vscode.workspace.asRelativePath(e.doc.uri),
      colab: e.tracker.manual === undefined ? e.tracker.colab : e.tracker.manual ? "yes (marked)" : "no (marked)",
      kernel: e.tracker.kernelStatus,
      monitored: e.tracker.monitored,
    }));
  }

  async setManualMark(doc: vscode.NotebookDocument, mark: boolean | undefined): Promise<void> {
    const marks = { ...this.state.get<Record<string, boolean>>(MANUAL_KEY, {}) };
    const key = doc.uri.toString();
    if (mark === undefined) delete marks[key];
    else marks[key] = mark;
    await this.state.update(MANUAL_KEY, marks);
    const entry = this.entries.get(key);
    if (entry) entry.tracker.manual = mark;
    this.changed.fire();
  }

  redetect(doc: vscode.NotebookDocument): void {
    const entry = this.entries.get(doc.uri.toString());
    if (!entry) return;
    entry.tracker.colab = "pending";
    entry.probeAttempts = 0;
    entry.nextProbeAt = 0;
    void this.maybeProbe(entry);
  }

  /**
   * Release the Colab runtime behind a notebook (watchdog command). Refuses
   * unless the kernel is proven Colab (probe, not just a manual mark) and is idle
   * right now.
   */
  async releaseRuntime(notebookId: string): Promise<{ ok: boolean; detail: string }> {
    const entry = [...this.entries.values()].find((e) => e.tracker.notebookId === notebookId);
    if (!entry) return { ok: false, detail: "refused: notebook is not open in this VS Code window" };
    const t = entry.tracker;
    const kernel = entry.kernel;
    if (!kernel || !t.monitored || t.released) return { ok: false, detail: "refused: notebook is not attached to a Colab kernel" };
    if (t.colab !== "yes") return { ok: false, detail: "refused: kernel is not verified as a Colab runtime" };
    if (kernel.status !== "idle" || t.isBusy()) return { ok: false, detail: `refused: kernel is ${kernel.status}` };

    const name = vscode.workspace.asRelativePath(entry.doc.uri);
    this.log.warn(`Releasing the Colab runtime of ${name} (watchdog request).`);
    const generation = t.generation;
    const cts = new vscode.CancellationTokenSource();
    const decoder = new TextDecoder();
    let text = "";
    let streamEnded = false;
    void (async () => {
      try {
        for await (const output of kernel.executeCode(UNASSIGN_CODE, cts.token)) {
          for (const item of output.items) text += decoder.decode(item.data);
        }
      } catch (err) {
        text += `\n[stream error: ${String(err)}]`;
      } finally {
        streamEnded = true;
      }
    })();

    const deadline = Date.now() + 60_000;
    let outcome: { ok: boolean; detail: string } | undefined;
    while (!outcome && Date.now() < deadline) {
      const parsed = parseUnassignOutput(text);
      if (parsed.state === "released") {
        outcome = { ok: true, detail: "runtime released (google.colab.runtime.unassign)" };
      } else if (parsed.state === "error") {
        outcome = { ok: false, detail: `unassign failed: ${parsed.detail}` };
      } else if (t.generation !== generation || ["dead", "unknown", "terminating"].includes(kernel.status)) {
        // The VM went away before the confirmation line could be delivered.
        outcome = { ok: true, detail: `runtime released (kernel connection ended: ${kernel.status})` };
      } else if (streamEnded) {
        outcome = { ok: false, detail: "unassign finished without confirmation" };
      } else {
        await new Promise((r) => setTimeout(r, 250));
      }
    }
    cts.cancel();
    cts.dispose();
    outcome ??= { ok: false, detail: "no confirmation from the runtime within 60 s" };
    if (outcome.ok) t.markReleased();
    this.log.info(`Release of ${name}: ${outcome.detail}`);
    this.changed.fire();
    return outcome;
  }

  dispose(): void {
    if (this.timer) clearInterval(this.timer);
    for (const e of this.entries.values()) e.statusSub?.dispose();
    for (const d of this.disposables) d.dispose();
    this.changed.dispose();
  }

  // ------------------------------------------------------------------ internals

  private open(doc: vscode.NotebookDocument): void {
    if (doc.notebookType !== "jupyter-notebook") return;
    const key = doc.uri.toString();
    if (this.entries.has(key)) return;
    const tracker = new NotebookTracker(notebookId(key));
    tracker.manual = this.state.get<Record<string, boolean>>(MANUAL_KEY, {})[key];
    this.entries.set(key, { doc, tracker, kernel: undefined, statusSub: undefined, probeAttempts: 0, nextProbeAt: 0, probing: false });
    void this.refreshKernel(this.entries.get(key)!);
  }

  private close(doc: vscode.NotebookDocument): void {
    const entry = this.entries.get(doc.uri.toString());
    if (!entry) return;
    entry.statusSub?.dispose();
    this.entries.delete(doc.uri.toString());
    this.changed.fire();
  }

  private async pollKernels(): Promise<void> {
    for (const entry of [...this.entries.values()]) await this.refreshKernel(entry);
  }

  private async refreshKernel(entry: Entry): Promise<void> {
    let kernel: Kernel | undefined;
    try {
      kernel = await this.jupyter.kernels.getKernel(entry.doc.uri);
      if (this.kernelAccess === "unknown" && kernel) this.kernelAccess = "granted";
    } catch (err) {
      // The Jupyter extension refuses access until the user allows it.
      if (this.kernelAccess !== "denied") this.log.warn(`Kernel access unavailable: ${String(err)}`);
      this.kernelAccess = "denied";
      kernel = undefined;
    }
    if (kernel !== entry.kernel) {
      entry.statusSub?.dispose();
      entry.kernel = kernel;
      entry.probeAttempts = 0;
      entry.nextProbeAt = 0;
      entry.tracker.kernelChanged(kernel ? (kernel.status as KernelStatus) : "none");
      entry.statusSub = kernel?.onDidChangeStatus((status) => {
        entry.tracker.setKernelStatus(status as KernelStatus);
        this.changed.fire();
        if (status === "idle") void this.maybeProbe(entry);
      });
      this.log.info(`Kernel ${kernel ? "attached" : "detached"} for ${vscode.workspace.asRelativePath(entry.doc.uri)}`);
      this.changed.fire();
    } else if (kernel && kernel.status !== entry.tracker.kernelStatus) {
      entry.tracker.setKernelStatus(kernel.status as KernelStatus);
      this.changed.fire();
    }
    await this.maybeProbe(entry);
  }

  private async maybeProbe(entry: Entry): Promise<void> {
    const t = entry.tracker;
    const kernel = entry.kernel;
    if (!kernel || t.colab !== "pending" || entry.probing) return;
    if (t.manual !== undefined) return; // user decided
    if (vscode.workspace.getConfiguration("colabIdleGuard").get<string>("detection", "auto") !== "auto") return;
    if (kernel.status !== "idle" || entry.probeAttempts >= MAX_PROBE_ATTEMPTS || Date.now() < entry.nextProbeAt) return;
    entry.probing = true;
    entry.probeAttempts++;
    const generation = t.generation;
    try {
      const result = await probeKernel(kernel);
      if (t.generation !== generation) return; // kernel changed meanwhile
      if (result === "error") {
        entry.nextProbeAt = Date.now() + PROBE_RETRY_MS;
        this.log.warn(`Colab detection failed (attempt ${entry.probeAttempts}) for ${vscode.workspace.asRelativePath(entry.doc.uri)}`);
      } else {
        t.colab = result;
        this.log.info(`${vscode.workspace.asRelativePath(entry.doc.uri)}: ${result === "yes" ? "Colab runtime" : "not a Colab runtime"}`);
        this.changed.fire();
      }
    } finally {
      entry.probing = false;
    }
  }
}
