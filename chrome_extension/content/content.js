// Colab Idle Guard - content script (isolated world, colab.research.google.com only).
//
// Reports two things to the service worker:
//  1. Activity *kinds* (pointer, click, key, scroll, edit, focus). Never key
//     values, text, coordinates or any notebook content.
//  2. Execution / connection state from ColabIdleGuardProbe.
// Activity counts only for trusted (real user) events while this tab is
// visible and focused, so a mouse passing over a background window does not count.
(() => {
  "use strict";
  const Probe = globalThis.ColabIdleGuardProbe;
  const THROTTLE_MS = 5000;
  const STATE_INTERVAL_MS = 10000;
  const PROBE_INTERVAL_MS = 2000;

  let kernelSnapshot = null;
  let notebookId = null;
  let lastState = null;
  let lastStateSentAt = 0;
  // Nothing is reported until this page is recognised as a notebook (cells or a
  // kernel socket seen). A loading page or a non-notebook Colab page has no
  // runtime to protect, and reporting it as "unknown" would leave a permanent
  // uncertain entry in the watchdog when the tab closes.
  let recognised = false;
  const lastSent = new Map();
  let contextAlive = true;

  function send(message) {
    if (!contextAlive) return;
    try {
      chrome.runtime.sendMessage(message).catch(() => {});
    } catch (_) {
      // Extension was reloaded/updated: this script is orphaned and must stop.
      contextAlive = false;
    }
  }

  // --- activity ------------------------------------------------------------------

  function interactive() {
    return document.visibilityState === "visible" && document.hasFocus();
  }

  function activity(kind, event) {
    if (event && event.isTrusted === false) return; // synthetic events never count
    if (!interactive()) return;
    const now = Date.now();
    if (now - (lastSent.get(kind) || 0) < THROTTLE_MS) return;
    lastSent.set(kind, now);
    send({ type: "activity", kind, at: now });
  }

  const opts = { capture: true, passive: true };
  addEventListener("pointermove", (e) => activity("pointer", e), opts);
  addEventListener("pointerdown", (e) => activity("click", e), opts);
  addEventListener("keydown", (e) => activity("key", e), opts);
  addEventListener("wheel", (e) => activity("scroll", e), opts);
  addEventListener("input", (e) => activity("edit", e), opts);
  addEventListener("focus", (e) => activity("focus", e), opts);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") activity("focus", null);
  });

  // --- execution state -----------------------------------------------------------

  document.addEventListener("colab-idle-guard:kernel", (event) => {
    try {
      kernelSnapshot = JSON.parse(event.detail);
    } catch (_) {}
    report(false);
  });

  async function ensureNotebookId() {
    const id = await Probe.notebookId(location);
    if (id !== notebookId) {
      notebookId = id;
      lastState = null;
    }
    return id;
  }

  async function report(force) {
    if (!contextAlive || !document.documentElement) return;
    const id = await ensureNotebookId();
    const result = Probe.probe(document, kernelSnapshot);
    if (result.signals.some((s) => s.available) || result.execution === "busy") recognised = true;
    if (!recognised) return;
    const key =`${result.execution}|${result.running_cells}|${result.runtime}`;
    const now = Date.now();
    if (!force && key === lastState && now - lastStateSentAt < STATE_INTERVAL_MS) return;
    lastState = key;
    lastStateSentAt = now;
    send({ type: "state", notebook_id: id, execution: result.execution, running_cells: result.running_cells,
           runtime: result.runtime, probe_version: result.probe_version, signals: result.signals });
  }

  // The service worker asks for state when it wakes up.
  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (message && message.type === "request_state") {
      report(true).then(() => sendResponse({ ok: true }));
      return true;
    }
    if (message && message.type === "release_runtime") {
      releaseRuntime(message.payload || {})
        .catch((e) => ({ ok: false, detail: `release error: ${e && e.message}` }))
        .then(sendResponse);
      return true;
    }
    return false;
  });

  // --- runtime release (Runtime > Disconnect and delete runtime) -------------------

  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  async function waitFor(fn, timeoutMs, stepMs = 100) {
    const end = Date.now() + timeoutMs;
    while (Date.now() < end) {
      const v = fn();
      if (v) return v;
      await sleep(stepMs);
    }
    return null;
  }

  function pointerClick(el) {
    const r = el.getBoundingClientRect();
    const base = { bubbles: true, cancelable: true, view: window, button: 0, clientX: r.x + Math.min(20, r.width / 2), clientY: r.y + r.height / 2 };
    for (const [type, extra] of [["pointerover", {}], ["mouseover", {}], ["pointerdown", { buttons: 1 }], ["mousedown", { buttons: 1 }], ["pointerup", {}], ["mouseup", {}], ["click", {}]]) {
      const Ctor = type.startsWith("pointer") ? PointerEvent : MouseEvent;
      el.dispatchEvent(new Ctor(type, { ...base, ...extra }));
    }
  }

  function closeMenus() {
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", keyCode: 27, bubbles: true }));
  }

  function openReleaseDialog() {
    return [...document.querySelectorAll(Probe.RELEASE.dialog)].find((d) => {
      const open = d.open || d.hasAttribute("open");
      const text = `${d.textContent} ${d.shadowRoot ? d.shadowRoot.textContent : ""}`;
      return open && text.includes(Probe.RELEASE.dialogTitle);
    });
  }

  /** Never release unless this page itself shows a connected runtime with no work. */
  function safeToRelease() {
    const r = Probe.probe(document, kernelSnapshot);
    if (r.execution !== "idle") return `execution is ${r.execution}`;
    if (r.runtime !== "connected") return `runtime is ${r.runtime}`;
    return null;
  }

  async function releaseRuntime(payload) {
    if (payload.notebook_id && payload.notebook_id !== (await ensureNotebookId())) {
      return { ok: false, detail: "refused: different notebook" };
    }
    let unsafe = safeToRelease();
    if (unsafe) return { ok: false, detail: `refused: ${unsafe}` };

    const menuButton = document.querySelector(Probe.RELEASE.menuButton);
    if (!menuButton) return { ok: false, detail: "Runtime menu not found (Colab UI changed?)" };
    pointerClick(menuButton);
    const item = await waitFor(() => {
      const el = document.querySelector(Probe.RELEASE.menuItem);
      return el && el.offsetParent !== null ? el : null;
    }, 3000);
    if (!item) {
      closeMenus();
      return { ok: false, detail: "'Disconnect and delete runtime' menu item not found (Colab UI changed?)" };
    }
    if (item.classList.contains("goog-menuitem-disabled") || item.getAttribute("aria-disabled") === "true") {
      closeMenus();
      return { ok: false, detail: "release menu item is disabled" };
    }
    // Final check immediately before the destructive step.
    unsafe = safeToRelease();
    if (unsafe) {
      closeMenus();
      return { ok: false, detail: `refused at final check: ${unsafe}` };
    }
    pointerClick(item);
    const dialog = await waitFor(openReleaseDialog, 5000);
    if (!dialog) return { ok: false, detail: "confirmation dialog did not appear" };
    const confirm = dialog.querySelector(Probe.RELEASE.confirmButton);
    if (!confirm) return { ok: false, detail: "confirmation button not found" };
    confirm.click();

    // Success only once the page shows the runtime disconnected for 5 s in a row.
    let since = null;
    const end = Date.now() + 45000;
    while (Date.now() < end) {
      const runtime = Probe.probe(document, kernelSnapshot).runtime;
      if (runtime === "disconnected") {
        since ??= Date.now();
        if (Date.now() - since >= 5000) {
          await report(true);
          return { ok: true, detail: "runtime released (Disconnect and delete runtime)" };
        }
      } else {
        since = null;
      }
      await sleep(500);
    }
    await report(true);
    return { ok: false, detail: "runtime was not shown as disconnected after confirming" };
  }

  addEventListener("hashchange", () => report(true));
  setInterval(() => report(false), PROBE_INTERVAL_MS);
  report(true);
})();
