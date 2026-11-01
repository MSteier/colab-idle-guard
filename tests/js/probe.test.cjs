// Probe combination rules and DOM signals (fixtures mirror the live Colab DOM, see research §7).
const test = require("node:test");
const assert = require("node:assert/strict");
const Probe = require("../../chrome_extension/content/colab_probe.js");

const NONE = { kernel_ws: false, dom: false };
const ALL = { kernel_ws: true, dom: true };

function fakeDoc({ cells = [], connect = null } = {}) {
  // cells: array of class strings, e.g. "running", "pending", "".
  const cellEls = cells.map((c) => ({ c: `cell code notebook-cell ${c}`.trim() }));
  const has = (el, cls) => el.c.split(/\s+/).includes(cls);
  const host = connect && {
    shadowRoot: {
      textContent: connect.text || "",
      querySelector: (sel) => (sel === "#connect" ? { getAttribute: () => connect.tooltip || null } : null),
    },
  };
  return {
    querySelector: (sel) => (sel === "colab-connect-button" ? host : null),
    querySelectorAll: (sel) => {
      if (sel === ".notebook-cell-list") return cells.length ? [{}] : [];
      if (sel === ".notebook-cell-list .cell.code") return cellEls;
      if (sel === ".cell.code.running") return cellEls.filter((e) => has(e, "running"));
      if (sel === ".cell.code.pending") return cellEls.filter((e) => has(e, "pending"));
      return [];
    },
  };
}

const CONNECTED = { tooltip: "Connected to\nPython 3 Google Compute Engine backend\nRAM: 0.89 GB" };
const NOT_CONNECTED = { text: " Connect arrow_drop_down " };
const kernel = (pending, open = 1) => ({ everConnected: true, framesSeen: 10, openSockets: open, pendingExecutions: pending });

test("validated idle page with open kernel socket is idle and connected", () => {
  const r = Probe.probe(fakeDoc({ cells: ["", ""], connect: CONNECTED }), kernel(0));
  assert.deepEqual([r.execution, r.running_cells, r.runtime], ["idle", 0, "connected"]);
});

test("running cell from either provider means busy", () => {
  assert.equal(Probe.probe(fakeDoc({ cells: ["running"], connect: CONNECTED }), kernel(0)).execution, "busy");
  assert.equal(Probe.probe(fakeDoc({ cells: [""], connect: CONNECTED }), kernel(1)).execution, "busy");
});

test("client-side queued cell (invisible to the kernel hook) counts as busy", () => {
  const r = Probe.probe(fakeDoc({ cells: ["running", "pending"], connect: CONNECTED }), kernel(1));
  assert.equal(r.execution, "busy");
  assert.equal(r.running_cells, 2);
});

test("unvalidated providers can prove busy but never idle", () => {
  const providers = [Probe.kernelProvider(kernel(0)), Probe.domProvider(fakeDoc({ cells: [""], connect: CONNECTED }))];
  const r = Probe.combine(providers, NONE);
  assert.equal(r.execution, "unknown");
  assert.equal(r.runtime, "unknown");
  const busy = Probe.combine([Probe.kernelProvider(kernel(2))], NONE);
  assert.equal(busy.execution, "busy");
});

test("unrecognised page (Colab redesign) is unknown, not idle", () => {
  const r = Probe.probe(fakeDoc({ cells: [] }), null);
  assert.equal(r.execution, "unknown");
  assert.equal(r.runtime, "unknown");
});

test("orphaned executions (all sockets closed) block idle", () => {
  const r = Probe.combine([Probe.kernelProvider(kernel(1, 0)), Probe.domProvider(fakeDoc({ cells: [""] }))], ALL);
  assert.equal(r.execution, "busy");
});

test("closed kernel socket alone is not proof of disconnection", () => {
  const r = Probe.probe(fakeDoc({ cells: [""], connect: { text: "Reconnecting" } }), kernel(0, 0));
  assert.equal(r.runtime, "unknown");
});

test("Connect button without a runtime reports disconnected", () => {
  const r = Probe.probe(fakeDoc({ cells: [""], connect: NOT_CONNECTED }), null);
  assert.deepEqual([r.execution, r.runtime], ["idle", "disconnected"]);
});

test("after 'Disconnect and delete runtime' the Reconnect button reports disconnected", () => {
  // Observed live 2026-10-31: text "Reconnect", tooltip "Click to connect".
  const r = Probe.probe(fakeDoc({ cells: [""], connect: { text: " Reconnect arrow_drop_down ", tooltip: "Click to connect" } }), kernel(0, 0));
  assert.deepEqual([r.execution, r.runtime], ["idle", "disconnected"]);
  const t = Probe.probe(fakeDoc({ cells: [""], connect: { text: "", tooltip: "Click to connect" } }), null);
  assert.equal(t.runtime, "disconnected");
});

test("release selectors use stable attributes, not labels", () => {
  assert.match(Probe.RELEASE.menuItem, /command="powerwash-current-vm"/);
  assert.match(Probe.RELEASE.confirmButton, /dialogaction="ok"/);
});

test("connected wins over disconnected between providers", () => {
  const r = Probe.probe(fakeDoc({ cells: [""], connect: NOT_CONNECTED }), kernel(0, 1));
  assert.equal(r.runtime, "connected");
});

test("notebook id is stable, opaque and URL-derived", async () => {
  const subtle = globalThis.crypto.subtle;
  const a = await Probe.notebookId({ pathname: "/drive/1AbC", hash: "#scrollTo=x" }, subtle);
  const b = await Probe.notebookId({ pathname: "/drive/1AbC", hash: "#scrollTo=y" }, subtle);
  const c = await Probe.notebookId({ pathname: "/drive/OTHER", hash: "" }, subtle);
  assert.match(a, /^nb_[0-9a-f]{32}$/);
  assert.equal(a, b);
  assert.notEqual(a, c);
  assert.ok(!a.includes("1AbC"));
});
