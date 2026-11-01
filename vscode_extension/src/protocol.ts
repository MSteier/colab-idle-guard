// Wire types shared with the Python watchdog (colab_idle_guard/watchdog/api_server.py).
import { createHash } from "node:crypto";

export type Execution = "idle" | "busy" | "unknown";
export type Runtime = "connected" | "disconnected" | "unknown";
export type ActivityKind = "pointer" | "click" | "key" | "scroll" | "edit" | "execute" | "focus" | "notebook_ui";

export interface NotebookReport {
  notebook_id: string;
  execution: Execution;
  running_cells: number;
  runtime: Runtime;
}

export interface Command {
  id: string;
  nonce: string;
  type: string;
  payload: Record<string, unknown>;
}

export interface HeartbeatResponse {
  heartbeat_interval_seconds: number;
  monitored: boolean;
  commands: Command[];
}

/** Opaque, stable notebook identifier: the notebook URI is never sent. */
export function notebookId(uri: string): string {
  return "nb_" + createHash("sha256").update(`colab-idle-guard|vscode|${uri}`).digest("hex").slice(0, 32);
}
