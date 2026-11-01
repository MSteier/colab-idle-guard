import test from "node:test";
import assert from "node:assert/strict";
import { isValidTabState, mergeTabs } from "../../chrome_extension/lib/aggregate.js";

const NB = "nb_" + "a".repeat(32);
const NB2 = "nb_" + "b".repeat(32);
const tab = (o) => ({ notebook_id: NB, execution: "idle", running_cells: 0, runtime: "connected", at: 1000, ...o });

test("same notebook in two tabs merges to the safest state", () => {
  const { notebooks } = mergeTabs({ 1: tab({}), 2: tab({ execution: "busy", running_cells: 2 }) }, 1000);
  assert.deepEqual(notebooks, [{ notebook_id: NB, execution: "busy", running_cells: 2, runtime: "connected" }]);
  const u = mergeTabs({ 1: tab({ runtime: "disconnected" }), 2: tab({ execution: "unknown", runtime: "unknown" }) }, 1000).notebooks[0];
  assert.deepEqual([u.execution, u.runtime], ["unknown", "unknown"]);
});

test("silent tabs expire", () => {
  const { notebooks, expiredTabs } = mergeTabs({ 1: tab({ at: 0 }), 2: tab({ notebook_id: NB2, at: 140000 }) }, 160000);
  assert.deepEqual(expiredTabs, ["1"]);
  assert.deepEqual(notebooks.map((n) => n.notebook_id), [NB2]);
});

test("busy always carries at least one running cell", () => {
  assert.equal(mergeTabs({ 1: tab({ execution: "busy", running_cells: 0 }) }, 1000).notebooks[0].running_cells, 1);
});

test("tab state validation", () => {
  assert.ok(isValidTabState(tab({})));
  assert.ok(!isValidTabState(tab({ notebook_id: "/drive/raw-url" })));
  assert.ok(!isValidTabState(tab({ execution: "running" })));
  assert.ok(!isValidTabState(tab({ running_cells: -1 })));
});
