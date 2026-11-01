import test from "node:test";
import assert from "node:assert/strict";
import { IDLE_SETTLE_MS, NotebookTracker } from "../state";
import { notebookId } from "../protocol";
import { parseProbeOutput, parseUnassignOutput, PROBE_CODE, UNASSIGN_CODE } from "../probe";

function tracker() {
  let t = 1_000_000;
  const clock = { now: () => t, advance: (ms: number) => (t += ms) };
  return { nb: new NotebookTracker("nb_x", clock.now), clock };
}

test("only proven Colab notebooks with a live kernel are monitored", () => {
  const { nb } = tracker();
  nb.kernelChanged("idle");
  assert.equal(nb.monitored, false); // verdict pending
  nb.colab = "no";
  assert.equal(nb.monitored, false);
  nb.colab = "yes";
  assert.equal(nb.monitored, true);
  nb.setKernelStatus("dead");
  assert.equal(nb.monitored, false);
});

test("idle kernel reports idle and connected", () => {
  const { nb } = tracker();
  nb.kernelChanged("idle");
  nb.colab = "yes";
  assert.deepEqual(nb.report(), { notebook_id: "nb_x", execution: "idle", running_cells: 0, runtime: "connected" });
});

test("busy, then idle only after the settle window (bridges queued cells)", () => {
  const { nb, clock } = tracker();
  nb.kernelChanged("idle");
  nb.colab = "yes";
  nb.setKernelStatus("busy");
  assert.equal(nb.report().execution, "busy");
  assert.equal(nb.report().running_cells, 1);
  clock.advance(60_000);
  nb.setKernelStatus("idle"); // first queued cell finished
  clock.advance(IDLE_SETTLE_MS - 1);
  assert.equal(nb.report().execution, "busy");
  nb.setKernelStatus("busy"); // next queued cell started
  clock.advance(10);
  nb.setKernelStatus("idle");
  clock.advance(IDLE_SETTLE_MS);
  assert.equal(nb.report().execution, "idle");
});

test("starting/restarting count as busy and connected", () => {
  const { nb } = tracker();
  nb.kernelChanged("starting");
  nb.colab = "yes";
  assert.deepEqual([nb.report().execution, nb.report().runtime], ["busy", "connected"]);
  nb.setKernelStatus("restarting");
  assert.equal(nb.report().execution, "busy");
});

test("unknown and terminating are reported as unknown", () => {
  const { nb } = tracker();
  nb.kernelChanged("unknown");
  nb.colab = "yes";
  assert.deepEqual([nb.report().execution, nb.report().runtime], ["unknown", "unknown"]);
  nb.setKernelStatus("terminating");
  assert.equal(nb.report().execution, "unknown");
});

test("switching kernels resets the Colab verdict", () => {
  const { nb } = tracker();
  nb.kernelChanged("idle");
  nb.colab = "yes";
  const gen = nb.generation;
  nb.kernelChanged("idle"); // e.g. user picked a local kernel
  assert.equal(nb.colab, "pending");
  assert.equal(nb.monitored, false);
  assert.equal(nb.generation, gen + 1);
});

test("manual marks override detection", () => {
  const { nb } = tracker();
  nb.kernelChanged("idle");
  nb.manual = true;
  assert.equal(nb.monitored, true);
  nb.colab = "yes";
  nb.manual = false;
  assert.equal(nb.monitored, false);
});

test("released notebook keeps reporting disconnected until a new live kernel attaches", () => {
  const { nb } = tracker();
  nb.kernelChanged("idle");
  nb.colab = "yes";
  nb.markReleased();
  assert.deepEqual(nb.report(), { notebook_id: "nb_x", execution: "idle", running_cells: 0, runtime: "disconnected" });
  nb.kernelChanged("none"); // kernel went away with the VM
  assert.equal(nb.monitored, true);
  assert.equal(nb.report().runtime, "disconnected");
  nb.kernelChanged("idle"); // user reconnected
  assert.equal(nb.released, false);
  assert.equal(nb.monitored, false); // new kernel must be verified again
});

test("unassign output parsing", () => {
  assert.deepEqual(parseUnassignOutput("CIG_UNASSIGNED\n"), { state: "released" });
  assert.equal(parseUnassignOutput("Traceback (most recent call last):\nRuntimeManagementError: unassign failed").state, "error");
  assert.equal(parseUnassignOutput("ModuleNotFoundError: No module named 'google.colab'").state, "error");
  assert.equal(parseUnassignOutput("").state, "pending");
  assert.ok(UNASSIGN_CODE.startsWith('exec("') && UNASSIGN_CODE.endsWith('", {})'), "runs in a fresh namespace");
  assert.ok(UNASSIGN_CODE.includes("finally:") && UNASSIGN_CODE.includes("o.eval_js = f"), "eval_js is restored");
});

test("notebook id is opaque and stable", () => {
  const a = notebookId("file:///c%3A/work/train.ipynb");
  assert.match(a, /^nb_[0-9a-f]{32}$/);
  assert.equal(a, notebookId("file:///c%3A/work/train.ipynb"));
  assert.notEqual(a, notebookId("file:///c%3A/work/other.ipynb"));
  assert.ok(!a.includes("train"));
});

test("probe output parsing", () => {
  assert.equal(parseProbeOutput("CIG_COLAB=1\n"), "yes");
  assert.equal(parseProbeOutput("CIG_COLAB=0\n"), "no");
  assert.equal(parseProbeOutput("Traceback ..."), "error");
  assert.equal(parseProbeOutput(""), "error");
  assert.ok(!/=\s*[a-zA-Z_]\w*\s*=/.test(PROBE_CODE), "probe must not assign variables");
});
