// Colab Idle Guard - MV3 service worker entry point. Logic lives in lib/background.js.
import { createBackground } from "./lib/background.js";

const background = createBackground({
  chrome,
  fetch: (...args) => fetch(...args),
  version: chrome.runtime.getManifest().version,
  randomId: () => crypto.randomUUID().replace(/-/g, ""),
});

const ALARM = "colab-idle-guard-heartbeat";

function ensureAlarm() {
  // 30 s is the minimum alarm period; content scripts wake the worker more often.
  chrome.alarms.get(ALARM).then((alarm) => {
    if (!alarm) chrome.alarms.create(ALARM, { periodInMinutes: 0.5 });
  });
}

chrome.runtime.onInstalled.addListener(() => {
  ensureAlarm();
  chrome.storage.local.get("token").then(({ token }) => {
    if (!token) chrome.runtime.openOptionsPage();
  });
});
chrome.runtime.onStartup.addListener(ensureAlarm);
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM) background.onAlarm();
});
chrome.tabs.onRemoved.addListener((tabId) => background.onTabRemoved(tabId));

const EXTENSION_ORIGIN = chrome.runtime.getURL("");

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (sender.id !== chrome.runtime.id) return false;
  // Extension pages (popup, options) are identified by URL: the options page
  // opens in a tab, so sender.tab alone does not distinguish it from Colab.
  if ((sender.url || "").startsWith(EXTENSION_ORIGIN)) {
    if (message && message.type === "get_status") {
      background.status().then(sendResponse);
      return true;
    }
    if (message && message.type === "test_connection") {
      background.testConnection(message.token, message.port).then(sendResponse);
      return true;
    }
    if (message && message.type === "heartbeat_now") {
      background.heartbeat().then(() => background.status()).then(sendResponse);
      return true;
    }
    return false;
  }
  background.onMessage(message, sender);
  return false;
});

ensureAlarm();
