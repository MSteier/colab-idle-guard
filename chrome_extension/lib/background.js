// Service-worker logic, written against injected dependencies so it can be
// tested in Node (tests/js) against the real Python watchdog.
//
// MV3 service workers are suspended after ~30 s without events. All state that
// must survive suspension lives in chrome.storage.session (tab states, queued
// activity); pairing settings live in chrome.storage.local. Wake-ups come from
// content-script messages (every few seconds while a Colab tab is open) and a
// 30-second chrome.alarms heartbeat.

import { isValidTabState, mergeTabs } from "./aggregate.js";

export const DEFAULT_PORT = 47821;
const HEARTBEAT_MIN_MS = 12000;
const ACTIVITY_FLUSH_DELAY_MS = 2000;
const MAX_QUEUED_EVENTS = 200;
const MAX_EVENT_AGE_MS = 110000;
const ACTIVITY_KINDS = new Set(["pointer", "click", "key", "scroll", "edit", "focus"]);

export function createBackground({ chrome, fetch, now = () => Date.now(), version = "0.0.0", randomId }) {
  let state = null; // {tabs, activity, lastBeatAt, lastStateKey, status}
  let chain = Promise.resolve();
  let flushTimer = null;

  // Serialise every state mutation: messages can arrive concurrently.
  function locked(fn) {
    const run = chain.then(fn, fn);
    chain = run.catch(() => {});
    return run;
  }

  async function load() {
    if (state) return state;
    const s = await chrome.storage.session.get(["cig_state"]);
    state = s.cig_state || { tabs: {}, activity: [], lastBeatAt: 0, lastStateKey: "", status: { result: "starting" } };
    return state;
  }

  async function save() {
    await chrome.storage.session.set({ cig_state: state });
  }

  async function settings() {
    const s = await chrome.storage.local.get(["token", "port", "instanceId"]);
    if (!s.instanceId) {
      s.instanceId = "chrome-" + randomId();
      await chrome.storage.local.set({ instanceId: s.instanceId });
    }
    return { token: s.token || "", port: Number(s.port) || DEFAULT_PORT, instanceId: s.instanceId };
  }

  async function call(path, body) {
    const cfg = await settings();
    if (!cfg.token) {
      state.status = { result: "not_paired", at: now() };
      return null;
    }
    let response;
    try {
      response = await fetch(`http://127.0.0.1:${cfg.port}${path}`, {
        method: "POST",
        headers: { Authorization: `Bearer ${cfg.token}`, "Content-Type": "application/json" },
        body: JSON.stringify({ instance_id: cfg.instanceId, integration: "chrome", version, ...body }),
        signal: AbortSignal.timeout(5000),
      });
    } catch (_) {
      state.status = { result: "unreachable", at: now() };
      return null;
    }
    if (response.status === 401) {
      state.status = { result: "unauthorized", at: now() };
      return null;
    }
    if (!response.ok) {
      state.status = { result: `error_${response.status}`, at: now() };
      return null;
    }
    state.status = { result: "ok", at: now() };
    return response.json();
  }

  // --- heartbeat & commands -------------------------------------------------------

  async function heartbeatLocked(force) {
    const t = now();
    const { notebooks, expiredTabs } = mergeTabs(state.tabs, t);
    for (const id of expiredTabs) delete state.tabs[id];
    const key = JSON.stringify(notebooks);
    if (!force && key === state.lastStateKey && t - state.lastBeatAt < HEARTBEAT_MIN_MS) return null;
    const result = await call("/v1/heartbeat", { full_snapshot: true, notebooks });
    if (result) {
      state.lastBeatAt = t;
      state.lastStateKey = key;
      state.monitored = result.monitored !== false;
    }
    await save();
    if (result && Array.isArray(result.commands)) {
      for (const command of result.commands) await runCommand(command);
    }
    return result;
  }

  async function runCommand(command) {
    let ok = false;
    let detail = "unsupported command";
    if (command.type === "release_runtime") {
      const tabId = Object.entries(state.tabs).find(([, t]) => t.notebook_id === command.payload?.notebook_id)?.[0];
      if (!tabId) {
        detail = "notebook tab not found";
      } else {
        try {
          const reply = await chrome.tabs.sendMessage(Number(tabId), { type: "release_runtime", payload: command.payload });
          ok = Boolean(reply && reply.ok);
          detail = (reply && reply.detail) || "no reply";
        } catch (e) {
          detail = `tab unreachable: ${e && e.message}`;
        }
      }
    }
    await call("/v1/command_result", { command_id: command.id, nonce: command.nonce, ok, detail: String(detail).slice(0, 400) });
  }

  // --- activity ---------------------------------------------------------------------

  async function flushActivityLocked() {
    const t = now();
    const events = state.activity
      .filter((e) => t - e.at <= MAX_EVENT_AGE_MS)
      .map((e) => ({ kind: e.kind, age_ms: Math.max(0, Math.round(t - e.at)) }));
    state.activity = [];
    if (events.length && state.monitored !== false) await call("/v1/activity", { events });
    await save();
  }

  function scheduleFlush() {
    if (flushTimer) return;
    flushTimer = setTimeout(() => {
      flushTimer = null;
      locked(async () => {
        await load();
        await flushActivityLocked();
      });
    }, ACTIVITY_FLUSH_DELAY_MS);
  }

  // --- event handlers ------------------------------------------------------------------

  function onMessage(message, sender) {
    const tabId = sender && sender.tab && sender.tab.id;
    if (!message || tabId === undefined) return Promise.resolve();
    return locked(async () => {
      await load();
      if (message.type === "state" && isValidTabState(message)) {
        state.tabs[String(tabId)] = {
          notebook_id: message.notebook_id,
          execution: message.execution,
          running_cells: message.running_cells,
          runtime: message.runtime,
          at: now(),
        };
        await heartbeatLocked(false);
      } else if (message.type === "activity" && ACTIVITY_KINDS.has(message.kind)) {
        const at = Number.isFinite(message.at) ? Math.min(message.at, now()) : now();
        state.activity.push({ kind: message.kind, at });
        if (state.activity.length > MAX_QUEUED_EVENTS) state.activity.splice(0, state.activity.length - MAX_QUEUED_EVENTS);
        await save();
        scheduleFlush();
      }
    });
  }

  function onTabRemoved(tabId) {
    return locked(async () => {
      await load();
      if (state.tabs[String(tabId)]) {
        delete state.tabs[String(tabId)];
        await heartbeatLocked(true);
      }
    });
  }

  function onAlarm() {
    return locked(async () => {
      await load();
      await flushActivityLocked();
      await heartbeatLocked(true);
    });
  }

  async function status() {
    return locked(async () => {
      await load();
      const cfg = await settings();
      return {
        paired: Boolean(cfg.token),
        port: cfg.port,
        connection: state.status,
        monitored: state.monitored !== false,
        notebooks: mergeTabs(state.tabs, now()).notebooks,
      };
    });
  }

  async function testConnection(token, port) {
    try {
      const r = await fetch(`http://127.0.0.1:${port}/v1/health`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        signal: AbortSignal.timeout(4000),
      });
      const body = await r.json();
      return { reachable: true, authenticated: Boolean(body.authenticated), version: body.version };
    } catch (_) {
      return { reachable: false, authenticated: false };
    }
  }

  return { onMessage, onTabRemoved, onAlarm, status, testConnection, heartbeat: () => locked(async () => { await load(); return heartbeatLocked(true); }), flush: () => locked(async () => { await load(); await flushActivityLocked(); }) };
}
