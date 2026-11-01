// HTTP client for the local watchdog API. No vscode import: unit-tested against the real watchdog.

import type { ActivityKind, Command, HeartbeatResponse, NotebookReport } from "./protocol";

export type ConnectionResult = "ok" | "not_paired" | "unauthorized" | "unreachable" | `error_${number}`;

export interface TokenSource {
  /** Current token, if any. */
  get(): Promise<string | undefined>;
  /** Called after a 401 so the source can try another way (e.g. re-read the token file). */
  refresh?(): Promise<string | undefined>;
}

export interface ClientOptions {
  port: () => number;
  instanceId: string;
  version: string;
  tokens: TokenSource;
  fetchImpl?: typeof fetch;
  timeoutMs?: number;
}

const MAX_EVENT_AGE_MS = 110_000;

export class WatchdogClient {
  lastResult: ConnectionResult = "not_paired";
  lastContactAt = 0;
  monitored = true;
  private readonly fetchImpl: typeof fetch;
  private pendingActivity: { kind: ActivityKind; at: number }[] = [];

  constructor(private readonly opts: ClientOptions) {
    this.fetchImpl = opts.fetchImpl ?? fetch;
  }

  async heartbeat(notebooks: NotebookReport[], fullSnapshot = true): Promise<Command[]> {
    const res = await this.post<HeartbeatResponse>(fullSnapshot ? "/v1/heartbeat" : "/v1/state",
      { notebooks, full_snapshot: fullSnapshot });
    if (!res) return [];
    this.monitored = res.monitored !== false;
    return Array.isArray(res.commands) ? res.commands : [];
  }

  queueActivity(kind: ActivityKind, at: number = Date.now()): void {
    this.pendingActivity.push({ kind, at });
    if (this.pendingActivity.length > 200) this.pendingActivity.splice(0, this.pendingActivity.length - 200);
  }

  get hasPendingActivity(): boolean {
    return this.pendingActivity.length > 0;
  }

  async flushActivity(now: number = Date.now()): Promise<void> {
    const events = this.pendingActivity
      .filter((e) => now - e.at <= MAX_EVENT_AGE_MS)
      .map((e) => ({ kind: e.kind, age_ms: Math.max(0, Math.round(now - e.at)) }));
    this.pendingActivity = [];
    if (events.length && this.monitored) await this.post("/v1/activity", { events });
  }

  async commandResult(command: Command, ok: boolean, detail: string): Promise<void> {
    await this.post("/v1/command_result", { command_id: command.id, nonce: command.nonce, ok, detail: detail.slice(0, 400) });
  }

  async goodbye(notebooks: NotebookReport[]): Promise<void> {
    await this.post("/v1/goodbye", { notebooks });
  }

  /** Returns reachability/authentication without registering the instance. */
  async test(token: string): Promise<{ reachable: boolean; authenticated: boolean; version?: string }> {
    try {
      const r = await this.fetchImpl(`http://127.0.0.1:${this.opts.port()}/v1/health`, {
        headers: { Authorization: `Bearer ${token}` },
        signal: AbortSignal.timeout(this.opts.timeoutMs ?? 5000),
      });
      const body = (await r.json()) as { authenticated?: boolean; version?: string };
      return { reachable: true, authenticated: Boolean(body.authenticated), version: body.version };
    } catch {
      return { reachable: false, authenticated: false };
    }
  }

  private async post<T>(path: string, body: Record<string, unknown>, retryToken?: string): Promise<T | undefined> {
    const retried = retryToken !== undefined;
    const token = retryToken ?? (await this.opts.tokens.get());
    if (!token) {
      this.lastResult = "not_paired";
      return undefined;
    }
    let response: Response;
    try {
      response = await this.fetchImpl(`http://127.0.0.1:${this.opts.port()}${path}`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ instance_id: this.opts.instanceId, integration: "vscode", version: this.opts.version, ...body }),
        signal: AbortSignal.timeout(this.opts.timeoutMs ?? 5000),
      });
    } catch {
      this.lastResult = "unreachable";
      return undefined;
    }
    if (response.status === 401) {
      // The watchdog may have rotated its token: try the token source once more.
      if (!retried && this.opts.tokens.refresh) {
        const fresh = await this.opts.tokens.refresh();
        if (fresh && fresh !== token) return this.post<T>(path, body, fresh);
      }
      this.lastResult = "unauthorized";
      return undefined;
    }
    if (!response.ok) {
      this.lastResult = `error_${response.status}`;
      return undefined;
    }
    this.lastResult = "ok";
    this.lastContactAt = Date.now();
    return (await response.json()) as T;
  }
}
