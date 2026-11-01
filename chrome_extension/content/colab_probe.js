// Colab Idle Guard - execution / connection state probe (isolated world).
//
// Combines independent signal providers into one fail-safe verdict:
//   * any provider showing running work            -> "busy"   (trusted even if unvalidated)
//   * a *validated* provider showing no work, and
//     nothing showing work                          -> "idle"
//   * anything else (no signals, UI changed, ...)   -> "unknown" (blocks release)
// Runtime connection is only reported from validated providers; otherwise "unknown".
//
// A provider is marked validated only after it has been checked on the live
// Colab UI (see docs/COLAB_RUNTIME_RESEARCH.md, Q1). Selectors are kept here,
// in one place, so a Colab redesign degrades to "unknown" instead of "idle".
(function (root) {
  "use strict";

  const PROBE_VERSION = "2026.10.31";

  // Flip to true only after live validation of the provider.
  // Validated 2026-10-31 on colab.research.google.com (CPU runtime): running,
  // queued, finished and interrupted cells; see docs/COLAB_RUNTIME_RESEARCH.md §7.
  const VALIDATED = Object.freeze({ kernel_ws: true, dom: true });

  // DOM signals observed on the live Colab UI (2026-10-31).
  const DOM = Object.freeze({
    notebookRoot: [".notebook-cell-list"],
    codeCell: [".notebook-cell-list .cell.code"],
    // "running" = executing now; "pending" = queued client-side, its
    // execute_request has not been sent yet (the kernel hook cannot see it).
    runningCell: [".cell.code.running", ".cell.code.pending"],
  });

  function queryAny(doc, selectors) {
    let count = 0;
    let matched = null;
    for (const selector of selectors) {
      try {
        const n = doc.querySelectorAll(selector).length;
        if (n > 0 && matched === null) matched = selector;
        count = Math.max(count, n);
      } catch (_) {
        /* invalid selector in this engine: ignore */
      }
    }
    return { count, matched };
  }

  function countRunning(doc) {
    // Distinct cells, so a cell matching both selectors is not counted twice.
    const cells = new Set();
    for (const selector of DOM.runningCell) {
      try {
        doc.querySelectorAll(selector).forEach((el) => cells.add(el));
      } catch (_) {}
    }
    return cells.size;
  }

  /** Connection state from the toolbar Connect button (lives in its shadow DOM). */
  function domRuntime(doc) {
    const host = doc.querySelector && doc.querySelector("colab-connect-button");
    const shadow = host && host.shadowRoot;
    if (!shadow) return null;
    const button = shadow.querySelector("#connect");
    const tooltip = (button && button.getAttribute("tooltiptext")) || "";
    if (tooltip.startsWith("Connected to")) return "connected";
    // Observed 2026-10-31: "Connect" before the first connection, "Reconnect"
    // after "Disconnect and delete runtime"; both with tooltip "Click to connect".
    const text = (shadow.textContent || "").replace(/arrow_drop_down/g, "").trim();
    if (text === "Connect" || text === "Reconnect" || tooltip === "Click to connect") return "disconnected";
    return null; // connecting, reconnecting, busy, or an unrecognised UI
  }

  function domProvider(doc) {
    const root = queryAny(doc, DOM.notebookRoot);
    const cells = queryAny(doc, DOM.codeCell);
    const available = root.count > 0 && cells.count > 0;
    return {
      provider: "dom",
      available,
      running: available ? countRunning(doc) : null,
      runtime: domRuntime(doc),
    };
  }

  function kernelProvider(snapshot, nowMs) {
    if (!snapshot) return { provider: "kernel_ws", available: false, running: null, runtime: null };
    // A kernel socket seen recently is what makes this provider usable at all.
    const available = Boolean(snapshot.everConnected) && snapshot.framesSeen > 0;
    // A closed socket is not proof of disconnection (Colab reconnects sockets),
    // so this provider only ever asserts "connected".
    const runtime = snapshot.openSockets > 0 ? "connected" : null;
    // With every socket closed, outstanding executions may still run server-side.
    const running = available ? snapshot.pendingExecutions : null;
    return {
      provider: "kernel_ws",
      available: available && (snapshot.openSockets > 0 || snapshot.pendingExecutions > 0),
      running,
      runtime,
      orphaned: snapshot.openSockets === 0 && snapshot.pendingExecutions > 0,
      kernelStatus: snapshot.kernelStatus,
    };
  }

  /**
   * Combine provider results. `validated` lets tests override VALIDATED.
   * Returns {execution, running_cells, runtime, signals}.
   */
  function combine(providers, validated = VALIDATED) {
    let busyCells = 0;
    let validatedIdle = false;
    let orphaned = false;
    const runtimes = [];
    for (const p of providers) {
      if (p.running !== null && p.running > 0) busyCells = Math.max(busyCells, p.running);
      if (p.orphaned) orphaned = true;
      if (validated[p.provider] && p.available && p.running === 0) validatedIdle = true;
      if (validated[p.provider] && p.runtime) runtimes.push(p.runtime);
    }
    let execution = "unknown";
    if (busyCells > 0) execution = "busy";
    else if (validatedIdle && !orphaned) execution = "idle";

    let runtime = "unknown";
    if (runtimes.includes("connected")) runtime = "connected";
    else if (runtimes.length > 0 && runtimes.every((r) => r === "disconnected")) runtime = "disconnected";

    return {
      execution,
      running_cells: execution === "busy" ? busyCells : 0,
      runtime,
      signals: providers.map((p) => ({ provider: p.provider, available: p.available, running: p.running, runtime: p.runtime })),
      probe_version: PROBE_VERSION,
    };
  }

  function probe(doc, kernelSnapshot, nowMs = Date.now(), validated = VALIDATED) {
    return combine([kernelProvider(kernelSnapshot, nowMs), domProvider(doc)], validated);
  }

  /** Stable, non-reversible notebook identifier derived from the URL. */
  async function notebookId(loc, subtle = root.crypto && root.crypto.subtle) {
    let key = loc.pathname;
    if (key === "/" || key === "") {
      const m = /[#&]fileId=([^&]+)/.exec(loc.hash || "");
      key = m ? `/fileId/${m[1]}` : "/untitled";
    }
    const bytes = new TextEncoder().encode(`colab-idle-guard|${key}`);
    const digest = new Uint8Array(await subtle.digest("SHA-256", bytes));
    return "nb_" + Array.from(digest.slice(0, 16), (b) => b.toString(16).padStart(2, "0")).join("");
  }

  // Colab's own release control: Runtime > "Disconnect and delete runtime".
  // Validated 2026-10-31 (research §9): script-dispatched pointer events work,
  // and the item and the dialog button are found by stable attributes, not labels.
  const RELEASE = Object.freeze({
    menuButton: "#runtime-menu-button",
    menuItem: '.goog-menuitem[command="powerwash-current-vm"]',
    dialog: "mwc-dialog",
    dialogTitle: "Disconnect and delete runtime",
    confirmButton: '[dialogaction="ok"]',
  });

  const api = { PROBE_VERSION, VALIDATED, DOM, RELEASE, domProvider, domRuntime, kernelProvider, combine, probe, notebookId };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.ColabIdleGuardProbe = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
