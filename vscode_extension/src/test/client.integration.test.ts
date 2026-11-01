// The VS Code extension's client and token reader against the real Python watchdog.
import test from "node:test";
import assert from "node:assert/strict";
import { spawn, execFileSync, type ChildProcess } from "node:child_process";
import * as fs from "node:fs";
import * as net from "node:net";
import * as os from "node:os";
import * as path from "node:path";
import { readWatchdogToken, tokenFilePath } from "../token_reader";
import { WatchdogClient } from "../watchdog_client";
import type { NotebookReport } from "../protocol";

const ROOT = path.resolve(__dirname, "../../..");
const PYTHON = path.join(ROOT, ".venv", "Scripts", "python.exe");
const NB = "nb_" + "d".repeat(32);

let watchdog: ChildProcess;
let port: number;
let home: string;
let realToken: string;

function freePort(): Promise<number> {
  return new Promise((resolve) => {
    const srv = net.createServer().listen(0, "127.0.0.1", () => {
      const p = (srv.address() as net.AddressInfo).port;
      srv.close(() => resolve(p));
    });
  });
}

async function status(): Promise<any> {
  const r = await fetch(`http://127.0.0.1:${port}/v1/status`, { headers: { Authorization: `Bearer ${realToken}` } });
  return r.json();
}

async function waitFor(pred: (s: any) => boolean, ms = 4000): Promise<any> {
  const end = Date.now() + ms;
  let last: any;
  while (Date.now() < end) {
    last = await status();
    if (pred(last)) return last;
    await new Promise((r) => setTimeout(r, 150));
  }
  assert.fail("condition not met: " + JSON.stringify(last));
}

const nb = (o: Partial<NotebookReport> = {}): NotebookReport =>
  ({ notebook_id: NB, execution: "idle", running_cells: 0, runtime: "connected", ...o });

test.before(async () => {
  home = fs.mkdtempSync(path.join(os.tmpdir(), "cig-vsc-"));
  port = await freePort();
  fs.mkdirSync(path.join(home, "config"), { recursive: true });
  fs.writeFileSync(path.join(home, "config", "config.json"), JSON.stringify({ api_port: port, require_foreground_corroboration: false }));
  const env = { ...process.env, COLAB_IDLE_GUARD_HOME: home };
  realToken = execFileSync(PYTHON, ["-m", "colab_idle_guard", "--show-pairing-token"], { cwd: ROOT, env }).toString().trim();
  watchdog = spawn(PYTHON, ["-m", "colab_idle_guard", "--headless"], { cwd: ROOT, env, stdio: "ignore" });
  for (let i = 0; i < 50; i++) {
    try {
      if ((await fetch(`http://127.0.0.1:${port}/v1/health`)).ok) return;
    } catch {}
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error("watchdog did not start");
});

test.after(async () => {
  if (watchdog.exitCode === null) {
    const exited = new Promise((r) => watchdog.once("exit", r));
    watchdog.kill();
    await exited;
  }
  fs.rmSync(home, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
});

test("token file path honours COLAB_IDLE_GUARD_HOME", () => {
  assert.equal(tokenFilePath({ COLAB_IDLE_GUARD_HOME: "X:\\h" }), path.join("X:\\h", "data", "token.bin"));
  assert.ok(tokenFilePath({ LOCALAPPDATA: "C:\\L" }).endsWith(path.join("ColabIdleGuard", "token.bin")));
});

test("automatic pairing: DPAPI token file written by Python is read by the extension", { skip: process.platform !== "win32" }, async () => {
  const token = await readWatchdogToken(path.join(home, "data", "token.bin"));
  assert.equal(token, realToken);
});

test("token reader rejects missing and corrupt files", async () => {
  assert.equal(await readWatchdogToken(path.join(home, "missing.bin")), undefined);
  const bad = path.join(home, "bad.bin");
  fs.writeFileSync(bad, "CIGDPAPI1not-a-dpapi-blob");
  assert.equal(await readWatchdogToken(bad), undefined);
  const plain = path.join(home, "plain.bin");
  fs.writeFileSync(plain, "CIGPLAIN1" + "p".repeat(43));
  assert.equal(await readWatchdogToken(plain), "p".repeat(43));
});

test("unpaired client sends nothing", async () => {
  const c = new WatchdogClient({ port: () => port, instanceId: "vscode-unpaired", version: "0.1.0", tokens: { get: async () => undefined } });
  assert.deepEqual(await c.heartbeat([nb()]), []);
  assert.equal(c.lastResult, "not_paired");
});

test("a rejected token is refreshed once from the token source", async () => {
  let calls = 0;
  const c = new WatchdogClient({
    port: () => port, instanceId: "vscode-refresh", version: "0.1.0",
    tokens: { get: async () => "w".repeat(43), refresh: async () => { calls++; return realToken; } },
  });
  await c.heartbeat([]);
  assert.equal(calls, 1);
  assert.equal(c.lastResult, "ok");
});

test("state, activity, busy and goodbye reach the watchdog", async () => {
  const c = new WatchdogClient({ port: () => port, instanceId: "vscode-e2e", version: "0.1.0", tokens: { get: async () => realToken } });
  await c.heartbeat([nb()]);
  let s = await waitFor((x) => x.runtime === "connected" && x.execution === "idle");
  assert.equal(s.integrations.find((i: any) => i.instance === "vscode-e2e").kind, "vscode");

  await new Promise((r) => setTimeout(r, 1200));
  c.queueActivity("edit");
  c.queueActivity("key");
  await c.flushActivity();
  assert.equal(c.hasPendingActivity, false);
  await waitFor((x) => x.idle_seconds < 1.0);

  await c.heartbeat([nb({ execution: "busy", running_cells: 1 })]);
  s = await waitFor((x) => x.state === "executing");
  assert.equal(s.running_cells, 1);

  // VS Code closes while the Colab kernel is busy: never assumed idle.
  await c.goodbye([nb({ execution: "busy", running_cells: 1 })]);
  s = await waitFor((x) => x.execution === "unknown" && x.stale_notebooks === 1);
  assert.equal(s.integrations.find((i: any) => i.instance === "vscode-e2e").status, "closed");
});

test("commands are acknowledged", async () => {
  const posted: any[] = [];
  const fakeFetch = (async (url: string, init: any) => {
    posted.push({ path: new URL(url).pathname, body: JSON.parse(init.body) });
    const commands = posted.length === 1 ? [{ id: "c1", nonce: "n1", type: "release_runtime", payload: {} }] : [];
    return new Response(JSON.stringify({ monitored: true, commands, heartbeat_interval_seconds: 15 }), { status: 200 });
  }) as typeof fetch;
  const c = new WatchdogClient({ port: () => 1, instanceId: "vscode-cmd", version: "0.1.0", tokens: { get: async () => "t".repeat(43) }, fetchImpl: fakeFetch });
  const cmds = await c.heartbeat([nb()]);
  assert.equal(cmds[0].type, "release_runtime");
  await c.commandResult(cmds[0], false, "not supported");
  assert.deepEqual(posted[1], {
    path: "/v1/command_result",
    body: { instance_id: "vscode-cmd", integration: "vscode", version: "0.1.0", command_id: "c1", nonce: "n1", ok: false, detail: "not supported" },
  });
});
