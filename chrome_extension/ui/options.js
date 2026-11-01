"use strict";

const tokenInput = document.getElementById("token");
const portInput = document.getElementById("port");
const result = document.getElementById("result");

function show(text, cls) {
  result.textContent = text;
  result.className = cls;
}

chrome.storage.local.get(["token", "port"]).then(({ token, port }) => {
  if (token) tokenInput.value = token;
  if (port) portInput.value = port;
});

document.getElementById("save").addEventListener("click", async () => {
  const token = tokenInput.value.trim();
  const port = Number(portInput.value);
  if (!Number.isInteger(port) || port < 1024 || port > 65535) return show("Port must be 1024-65535.", "bad");
  if (token && !/^[A-Za-z0-9_-]{40,100}$/.test(token)) return show("That does not look like a pairing token.", "bad");
  await chrome.storage.local.set({ token, port });
  show("Testing…", "muted");
  let test;
  try {
    test = await Promise.race([
      chrome.runtime.sendMessage({ type: "test_connection", token, port }),
      new Promise((_, reject) => setTimeout(() => reject(new Error("timeout")), 8000)),
    ]);
  } catch (_) {
    return show("Saved, but the extension did not respond. Reload it on chrome://extensions and try again.", "bad");
  }
  if (!test || !test.reachable) return show("Saved, but the Colab Idle Guard app is not reachable. Is it running?", "warn");
  if (!test.authenticated) return show("App reachable, but the token was rejected.", "bad");
  await chrome.runtime.sendMessage({ type: "heartbeat_now" });
  show(`Paired with Colab Idle Guard ${test.version}.`, "ok");
});
