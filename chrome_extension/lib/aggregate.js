// Merge per-tab Colab state into per-notebook reports for the watchdog.

const EXEC_RANK = { idle: 0, unknown: 1, busy: 2 };
const RUNTIME_RANK = { disconnected: 0, unknown: 1, connected: 2 };

/**
 * A tab that stops reporting (navigated away, frozen, crashed) is dropped after this.
 * Closed tabs are removed immediately via chrome.tabs.onRemoved; this only covers
 * silent tabs. It must exceed Chrome's intensive timer throttling of hidden tabs
 * (timers run about once a minute), or background notebooks would flap.
 */
export const TAB_STALE_MS = 150000;

/**
 * @param {Record<string, {notebook_id:string, execution:string, running_cells:number, runtime:string, at:number}>} tabs
 * @returns {{notebooks: Array<object>, expiredTabs: string[]}}
 */
export function mergeTabs(tabs, now, staleMs = TAB_STALE_MS) {
  const byNotebook = new Map();
  const expiredTabs = [];
  for (const [tabId, t] of Object.entries(tabs)) {
    if (now - t.at > staleMs) {
      expiredTabs.push(tabId);
      continue;
    }
    const prev = byNotebook.get(t.notebook_id);
    if (!prev) {
      byNotebook.set(t.notebook_id, {
        notebook_id: t.notebook_id,
        execution: t.execution,
        running_cells: t.running_cells,
        runtime: t.runtime,
      });
      continue;
    }
    // Same notebook open in several tabs: take the safest combination.
    if (EXEC_RANK[t.execution] > EXEC_RANK[prev.execution]) prev.execution = t.execution;
    prev.running_cells = Math.max(prev.running_cells, t.running_cells);
    if (RUNTIME_RANK[t.runtime] > RUNTIME_RANK[prev.runtime]) prev.runtime = t.runtime;
  }
  const notebooks = [...byNotebook.values()].map((n) => ({
    ...n,
    running_cells: n.execution === "busy" ? Math.max(1, n.running_cells) : 0,
  }));
  return { notebooks, expiredTabs };
}

export function isValidTabState(m) {
  return (
    m &&
    typeof m.notebook_id === "string" &&
    /^nb_[0-9a-f]{32}$/.test(m.notebook_id) &&
    m.execution in EXEC_RANK &&
    m.runtime in RUNTIME_RANK &&
    Number.isInteger(m.running_cells) &&
    m.running_cells >= 0
  );
}
