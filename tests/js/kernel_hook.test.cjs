// Runs content/kernel_hook.js in a sandbox with a fake WebSocket and replays
// Jupyter protocol traffic shaped like what was observed on live Colab.
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(path.join(__dirname, "../../chrome_extension/content/kernel_hook.js"), "utf8");

function sandbox() {
  const sockets = [];
  const events = [];
  class FakeWebSocket {
    constructor(url) {
      this.url = url;
      this.sent = [];
      this.listeners = {};
      sockets.push(this);
    }
    addEventListener(type, fn) {
      (this.listeners[type] ||= []).push(fn);
    }
    send(data) {
      this.sent.push(data);
    }
    emit(type, data) {
      for (const fn of this.listeners[type] || []) fn({ data });
    }
  }
  const document = {
    dispatchEvent: (e) => events.push(JSON.parse(e.detail)),
    addEventListener: () => {},
  };
  const window = { WebSocket: FakeWebSocket };
  const ctx = vm.createContext({
    window, document, Proxy, Reflect, Object, JSON, Map, Math, Date, Number, String, ArrayBuffer, DataView, Uint8Array,
    TextDecoder, setInterval: () => 0,
    CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init.detail; } },
  });
  vm.runInContext(SOURCE, ctx);
  const last = () => events[events.length - 1];
  return { window, sockets, events, last };
}

const msg = (type, id, parent, content = {}) =>
  JSON.stringify({ header: { msg_type: type, msg_id: id }, parent_header: parent ? { msg_id: parent } : {}, content, metadata: {} });

function v1Frame(type, id, parent, content) {
  // Jupyter v1 websocket binary framing.
  const enc = new TextEncoder();
  const parts = ["iopub", { msg_type: type, msg_id: id }, parent ? { msg_id: parent } : {}, {}, content].map((p) =>
    enc.encode(typeof p === "string" ? p : JSON.stringify(p)));
  const headerLen = 8 * (parts.length + 1);
  const total = headerLen + parts.reduce((n, p) => n + p.length, 0);
  const buf = new ArrayBuffer(total);
  const view = new DataView(buf);
  view.setBigUint64(0, BigInt(parts.length), true);
  let offset = headerLen;
  parts.forEach((p, i) => {
    view.setBigUint64(8 * (i + 1), BigInt(offset), true);
    new Uint8Array(buf, offset, p.length).set(p);
    offset += p.length;
  });
  return buf;
}

test("only kernel channel sockets are instrumented", () => {
  const s = sandbox();
  const lsp = new s.window.WebSocket("wss://x.colab.dev/colab/lsp");
  lsp.send(msg("execute_request", "a"));
  assert.equal(s.events.length, 0);
  const k = new s.window.WebSocket("wss://x.colab.dev/api/kernels/123/channels");
  k.emit("open");
  assert.equal(s.last().openSockets, 1);
  assert.ok(k instanceof Object && k.sent !== undefined, "real socket object returned");
});

test("execute_request -> busy until execute_reply / status idle with matching parent", () => {
  const s = sandbox();
  const k = new s.window.WebSocket("wss://x.colab.dev/api/kernels/123/channels");
  k.emit("open");
  k.send(msg("execute_request", "r1"));
  k.send(msg("execute_request", "r2"));
  assert.equal(s.last().pendingExecutions, 2);
  assert.deepEqual(k.sent.length, 2, "messages are still forwarded");
  k.emit("message", msg("status", "s1", "r1", { execution_state: "busy" }));
  k.emit("message", msg("execute_reply", "x1", "r1", { status: "ok" }));
  assert.equal(s.last().pendingExecutions, 1);
  k.emit("message", msg("status", "s2", "r2", { execution_state: "idle" }));
  assert.equal(s.last().pendingExecutions, 0);
  assert.equal(s.last().kernelStatus, "idle");
});

test("unrelated replies (e.g. completions) do not clear executions", () => {
  const s = sandbox();
  const k = new s.window.WebSocket("wss://x.colab.dev/api/kernels/1/channels");
  k.emit("open");
  k.send(msg("execute_request", "r1"));
  k.send(msg("complete_request", "c1"));
  k.emit("message", msg("complete_reply", "x", "c1"));
  k.emit("message", msg("status", "s", "c1", { execution_state: "idle" }));
  assert.equal(s.last().pendingExecutions, 1);
});

test("binary v1 framing is decoded", () => {
  const s = sandbox();
  const k = new s.window.WebSocket("wss://x/api/kernels/1/channels");
  k.emit("open");
  k.send(msg("execute_request", "r9"));
  k.emit("message", v1Frame("status", "s", "r9", { execution_state: "idle" }));
  assert.equal(s.last().pendingExecutions, 0);
  assert.equal(s.last().framesUndecoded, 0);
});

test("garbage frames are counted, never thrown", () => {
  const s = sandbox();
  const k = new s.window.WebSocket("wss://x/api/kernels/1/channels");
  k.emit("open");
  k.emit("message", "not json");
  k.emit("message", new ArrayBuffer(3));
  k.send("{broken");
  assert.equal(s.last().openSockets, 1);
  k.emit("message", msg("status", "s", null, { execution_state: "busy" }));
  assert.equal(s.last().framesUndecoded, 2);
});

test("socket close keeps outstanding executions (orphaned, not idle)", () => {
  const s = sandbox();
  const k = new s.window.WebSocket("wss://x/api/kernels/1/channels");
  k.emit("open");
  k.send(msg("execute_request", "r1"));
  k.emit("close");
  assert.deepEqual([s.last().openSockets, s.last().pendingExecutions], [0, 1]);
});

test("no message content is published", () => {
  const s = sandbox();
  const k = new s.window.WebSocket("wss://x/api/kernels/1/channels");
  k.emit("open");
  k.send(JSON.stringify({ header: { msg_type: "execute_request", msg_id: "r1" }, content: { code: "SECRET_CODE" } }));
  k.emit("message", msg("stream", "o", "r1", { text: "SECRET_OUTPUT" }));
  const published = JSON.stringify(s.events);
  assert.ok(!published.includes("SECRET"));
});
