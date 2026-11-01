"use strict";

const CONNECTION = {
  ok: ["Connected", "ok"],
  not_paired: ["Not paired - open Settings", "warn"],
  unauthorized: ["Token rejected - re-pair", "bad"],
  unreachable: ["App not running", "bad"],
  starting: ["Starting…", "muted"],
};

function cell(id, text, cls = "") {
  const el = document.getElementById(id);
  el.textContent = "";
  if (cls) {
    const dot = document.createElement("span");
    dot.className = `dot ${cls}`;
    el.append(dot);
  }
  el.append(text);
}

async function refresh() {
  const s = await chrome.runtime.sendMessage({ type: "heartbeat_now" });
  const [text, cls] = (s.paired ? CONNECTION[s.connection?.result] : CONNECTION.not_paired) || [s.connection?.result || "Unknown", "warn"];
  cell("connection", text, cls);
  cell("monitored", s.monitored ? "On" : "Off in app settings", s.monitored ? "ok" : "warn");
  if (!s.notebooks.length) {
    cell("notebooks", "None open", "muted");
  } else {
    const n = s.notebooks;
    const busy = n.filter((x) => x.execution === "busy").length;
    const unknown = n.filter((x) => x.execution === "unknown").length;
    const parts = [`${n.length} open`];
    if (busy) parts.push(`${busy} executing`);
    if (unknown) parts.push(`${unknown} state unknown`);
    cell("notebooks", parts.join(", "), busy ? "warn" : unknown ? "bad" : "ok");
  }
}

document.getElementById("options").addEventListener("click", () => chrome.runtime.openOptionsPage());
refresh();
