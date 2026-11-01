// End-to-end: the extension's service-worker logic (lib/background.js) with fake
// chrome.* APIs, talking over real HTTP to a real Python watchdog process.
import test from "node:test";
import assert from "node:assert/strict";
import { spawn, execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import net from "node:net";
import { fileURLToPath } from "node:url";
import { createBackground } from "../../chrome_extension/lib/background.js";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const PYTHON = path.join(ROOT, ".venv", "Scripts", "python.exe");
const NB = "nb_" + "c".repeat(32);

function freePort() {
  return new Promise((resolve) => {
    const srv = net.createServer().listen(0, "127.0.0.1", () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

function area() {
  const data = {};
  return {
    data,
    get: async (keys) => Object.fromEntries((Array.isArray(keys) ? keys : [keys]).filter((k) => k in data).map((k) => [k, structuredClone(data[k])])),
    set: async (obj) => Object.assign(data, structuredClone(obj)),
  };
}

function fakeChrome(tabReplies = {}) {
  const sentToTabs = [];
  return {
    sentToTabs,
    storage: { local: area(), session: area() },
    tabs: {
      sendMessage: async (tabId, message) => {
        sentToTabs.push({ tabId, message });
        return tabReplies[tabId] ? tabReplies[tabId](message) : undefined;
      },
    },
  };
}

let watchdog, port, token, home;

test.before(async () => {
  home = fs.mkdtempSync(path.join(os.tmpdir(), "cig-js-"));
  port = await freePort();
  fs.mkdirSync(path.join(home, "config"), { recursive: true });
  fs.writeFileSync(path.join(home, "config", "config.json"),
    JSON.stringify({ api_port: port, require_foreground_corroboration: false }));
  const env = { ...process.env, COLAB_IDLE_GUARD_HOME: home };
  token = execFileSync(PYTHON, ["-m", "colab_idle_guard", "--show-pairing-token"], { cwd: ROOT, env }).toString().trim();
  watchdog = spawn(PYTHON, ["-m", "colab_idle_guard", "--headless"], { cwd: ROOT, env, stdio: "ignore" });
  for (let i = 0; i < 50; i++) {
    try {
      if ((await fetch(`http://127.0.0.1:${port}/v1/health`)).ok) return;
    } catch (_) {}
    await new Promise((r) => setTimeout(r, 200));
  }
  throw new Error("watchdog did not start");
});

test.after(async () => {
  if (watchdog && watchdog.exitCode === null) {
    const exited = new Promise((r) => watchdog.once("exit", r));
    watchdog.kill();
    await exited;
  }
  fs.rmSync(home, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
});

async function watchdogStatus() {
  const r = await fetch(`http://127.0.0.1:${port}/v1/status`, { headers: { Authorization: `Bearer ${token}` } });
  return r.json();
}

async function waitFor(predicate, ms = 4000) {
  const end = Date.now() + ms;
  let last;
  while (Date.now() < end) {
    last = await watchdogStatus();
    if (predicate(last)) return last;
    await new Promise((r) => setTimeout(r, 150));
  }
  assert.fail(`condition not met; last status: ${JSON.stringify(last)}`);
}

function make(chrome, now = () => Date.now()) {
  return createBackground({ chrome, fetch, now, version: "0.1.0", randomId: () => Math.random().toString(16).slice(2) });
}

test("unpaired extension reports not_paired and sends nothing", async () => {
  const chrome = fakeChrome();
  const bg = make(chrome);
  await bg.onMessage({ type: "state", notebook_id: NB, execution: "idle", running_cells: 0, runtime: "connected" }, { tab: { id: 1 } });
  const s = await bg.status();
  assert.equal(s.paired, false);
  assert.equal(s.connection.result, "not_paired");
});

test("wrong token is reported as unauthorized", async () => {
  const chrome = fakeChrome();
  await chrome.storage.local.set({ token: "x".repeat(43), port });
  const bg = make(chrome);
  await bg.heartbeat();
  assert.equal((await bg.status()).connection.result, "unauthorized");
});

test("paired extension: state, activity, busy and tab close reach the watchdog", async () => {
  const chrome = fakeChrome();
  await chrome.storage.local.set({ token, port });
  const bg = make(chrome);
  const test1 = await bg.testConnection(token, port);
  assert.deepEqual([test1.reachable, test1.authenticated], [true, true]);

  await bg.onMessage({ type: "state", notebook_id: NB, execution: "idle", running_cells: 0, runtime: "connected" }, { tab: { id: 7 } });
  let st = await waitFor((s) => s.runtime === "connected" && s.execution === "idle");
  assert.equal(st.integrations.find((i) => i.kind === "chrome").status, "online");
  assert.equal((await bg.status()).connection.result, "ok");

  const before = st.idle_seconds;
  await new Promise((r) => setTimeout(r, 1200));
  await bg.onMessage({ type: "activity", kind: "click", at: Date.now() }, { tab: { id: 7 } });
  await bg.onMessage({ type: "activity", kind: "keystroke-content", at: Date.now() }, { tab: { id: 7 } }); // dropped locally
  await bg.flush();
  st = await waitFor((s) => s.idle_seconds < 1.0);
  assert.ok(before >= 0);

  await bg.onMessage({ type: "state", notebook_id: NB, execution: "busy", running_cells: 1, runtime: "connected" }, { tab: { id: 7 } });
  st = await waitFor((s) => s.execution === "busy" && s.state === "executing");
  assert.equal(st.running_cells, 1);

  // Tab closed while a cell runs: the runtime may still be executing -> stale/unknown, never idle.
  await bg.onTabRemoved(7);
  st = await waitFor((s) => s.execution === "unknown" && s.stale_notebooks === 1);
});

test("release command is routed to the right tab and its result reported", async () => {
  // Uses fake fetch to inject a command; the real round trip is covered by tests/test_api.py.
  const posted = [];
  const fakeFetch = async (url, init) => {
    const body = JSON.parse(init.body);
    posted.push({ path: new URL(url).pathname, body });
    const commands = posted.length === 1 ? [{ id: "c1", nonce: "n1", type: "release_runtime", payload: { notebook_id: NB } }] : [];
    return new Response(JSON.stringify({ monitored: true, commands }), { status: 200 });
  };
  const chrome = fakeChrome({ 5: () => ({ ok: false, detail: "release not supported by this extension version" }) });
  await chrome.storage.local.set({ token: "t".repeat(43), port: 1 });
  const bg = createBackground({ chrome, fetch: fakeFetch, version: "0.1.0", randomId: () => "id" });
  await bg.onMessage({ type: "state", notebook_id: NB, execution: "idle", running_cells: 0, runtime: "connected" }, { tab: { id: 5 } });
  assert.deepEqual(chrome.sentToTabs.map((m) => [m.tabId, m.message.type]), [[5, "release_runtime"]]);
  const result = posted.find((p) => p.path === "/v1/command_result").body;
  assert.deepEqual([result.command_id, result.nonce, result.ok], ["c1", "n1", false]);
  assert.match(result.detail, /not supported/);
});

test("invalid tab messages are ignored", async () => {
  const posted = [];
  const chrome = fakeChrome();
  await chrome.storage.local.set({ token: "t".repeat(43), port: 1 });
  const bg = createBackground({ chrome, fetch: async (u, i) => { posted.push(u); return new Response("{}"); }, randomId: () => "id" });
  await bg.onMessage({ type: "state", notebook_id: "https://colab/raw", execution: "idle", running_cells: 0, runtime: "connected" }, { tab: { id: 1 } });
  await bg.onMessage({ type: "state", notebook_id: NB, execution: "idle", running_cells: 0, runtime: "connected" }, {});
  assert.equal(posted.length, 0);
});
