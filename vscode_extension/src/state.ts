// Per-notebook execution / runtime state machine. Pure logic (no vscode import) so it is unit-tested.
//
// Source of truth for execution is the Jupyter kernel's own status
// (busy/idle from the kernel's iopub channel, via the Jupyter extension API).
// The stable VS Code API does not expose "cell is running" until it finishes,
// and the Jupyter extension sends queued cells one at a time, so the kernel
// goes idle for a moment between queued cells. An idle status therefore only
// counts after it has held for IDLE_SETTLE_MS.
//
// The Colab verdict belongs to the current *kernel*: switching the notebook to
// another kernel resets it, so a notebook moved to a local kernel stops being
// treated as Colab. A notebook without a live kernel is not reported at all;
// the watchdog then retires it (dropped if it was idle, kept as stale/unknown
// if it was busy, because the remote kernel may still be running).

import type { Execution, NotebookReport, Runtime } from "./protocol";

export type KernelStatus =
  | "unknown" | "starting" | "idle" | "busy" | "terminating" | "restarting" | "autorestarting" | "dead";

/** yes = proven Colab (probe or user mark); no = proven not Colab; pending = not decided yet. */
export type ColabVerdict = "yes" | "no" | "pending";

export const IDLE_SETTLE_MS = 3000;

const ACTIVE: ReadonlySet<string> = new Set(["busy", "starting", "restarting", "autorestarting"]);
const CONNECTED: ReadonlySet<string> = new Set(["idle", "busy", "starting", "restarting", "autorestarting"]);

export class NotebookTracker {
  /** Verdict for the current kernel. */
  colab: ColabVerdict = "pending";
  /** User override for this notebook: true = always Colab, false = never. */
  manual: boolean | undefined;
  private status: KernelStatus | "none" = "none";
  private kernelGeneration = 0;
  private lastActiveAt = Number.NEGATIVE_INFINITY;

  constructor(readonly notebookId: string, private readonly now: () => number = Date.now) {}

  /**
   * Set after this extension released the notebook's Colab runtime. The notebook
   * then keeps being reported as idle/disconnected (so the watchdog can confirm
   * the release) until a new live kernel is attached.
   */
  released = false;

  markReleased(): void {
    this.released = true;
  }

  /** A different kernel object is now attached (or none). Resets the Colab verdict. */
  kernelChanged(status: KernelStatus | "none"): number {
    if (this.released && CONNECTED.has(status)) this.released = false;
    this.kernelGeneration++;
    this.colab = "pending";
    this.lastActiveAt = Number.NEGATIVE_INFINITY;
    this.status = "none";
    this.setKernelStatus(status);
    return this.kernelGeneration;
  }

  get generation(): number {
    return this.kernelGeneration;
  }

  setKernelStatus(status: KernelStatus | "none"): void {
    if (ACTIVE.has(status) || ACTIVE.has(this.status)) this.lastActiveAt = this.now();
    this.status = status;
  }

  get kernelStatus(): KernelStatus | "none" {
    return this.status;
  }

  /** True while the kernel is doing work, or was within the settle window. */
  isBusy(): boolean {
    if (ACTIVE.has(this.status)) return true;
    return this.status === "idle" && this.now() - this.lastActiveAt < IDLE_SETTLE_MS;
  }

  get isColab(): boolean {
    return this.manual ?? this.colab === "yes";
  }

  /** Reported to the watchdog only while proven Colab and attached to a live kernel. */
  get monitored(): boolean {
    return this.released || (this.isColab && this.status !== "none" && this.status !== "dead");
  }

  report(): NotebookReport {
    if (this.released) return { notebook_id: this.notebookId, execution: "idle", running_cells: 0, runtime: "disconnected" };
    const execution = this.execution();
    return { notebook_id: this.notebookId, execution, running_cells: execution === "busy" ? 1 : 0, runtime: this.runtime() };
  }

  private execution(): Execution {
    if (this.isBusy()) return "busy";
    return this.status === "idle" ? "idle" : "unknown"; // unknown / terminating
  }

  private runtime(): Runtime {
    return CONNECTED.has(this.status) ? "connected" : "unknown";
  }
}
