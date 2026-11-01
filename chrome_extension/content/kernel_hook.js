// Colab Idle Guard - kernel protocol observer (runs in the page's MAIN world).
//
// Colab's frontend talks to its Jupyter kernel over WebSockets. The Jupyter
// messaging protocol (https://jupyter-client.readthedocs.io/en/latest/messaging.html)
// marks the start of a cell run with an `execute_request` and its end with an
// `execute_reply` / `status: idle` whose parent_header.msg_id points back to it.
// Counting outstanding execute_requests gives the number of running or queued
// cells, independently of Colab's DOM.
//
// Privacy: only header fields (msg_type, msg_id, parent msg_id) and the
// `execution_state` of status messages are read. Code, outputs and any other
// message content are never stored or forwarded.
//
// Whether Colab really uses this channel is checked on a live page
// (COLAB_RUNTIME_RESEARCH.md Q1). Until then the isolated-world probe treats
// this provider as unvalidated: it can prove "busy" but not "idle".
(() => {
  "use strict";
  if (window.__colabIdleGuardHook) return;
  Object.defineProperty(window, "__colabIdleGuardHook", { value: true });

  const EVENT = "colab-idle-guard:kernel";
  const KERNEL_URL = /\/api\/kernels\/[^/?#]+\/channels/;
  const state = {
    openSockets: 0,
    everConnected: false,
    pending: new Map(), // execute_request msg_id -> start time (ms)
    kernelStatus: "unknown", // last iopub execution_state
    lastMessageAt: 0,
    framesSeen: 0,
    framesUndecoded: 0,
  };

  // --- Jupyter message decoding ------------------------------------------------

  const decoder = new TextDecoder();

  // v1.kernel.websocket.jupyter.org binary framing: uint64 count, uint64 offsets,
  // then channel, header, parent_header, metadata, content, buffers...
  function decodeV1(buffer) {
    const view = new DataView(buffer);
    if (buffer.byteLength < 16) return null;
    const count = Number(view.getBigUint64(0, true));
    if (count < 5 || count > 64 || 8 * (count + 1) > buffer.byteLength) return null;
    const offsets = [];
    for (let i = 1; i <= count; i++) offsets.push(Number(view.getBigUint64(8 * i, true)));
    const part = (i) => decoder.decode(new Uint8Array(buffer, offsets[i], (offsets[i + 1] ?? buffer.byteLength) - offsets[i]));
    return {
      channel: part(0),
      header: JSON.parse(part(1)),
      parent_header: JSON.parse(part(2)),
      content: JSON.parse(part(4)),
    };
  }

  function decode(data) {
    try {
      if (typeof data === "string") return JSON.parse(data);
      if (data instanceof ArrayBuffer) return decodeV1(data);
      if (ArrayBuffer.isView(data)) return decodeV1(data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength));
    } catch (_) {
      /* not a Jupyter message */
    }
    return null;
  }

  function summary(msg) {
    if (!msg || typeof msg !== "object" || !msg.header) return null;
    const type = msg.header.msg_type || msg.msg_type;
    return {
      type,
      id: msg.header.msg_id,
      parent: msg.parent_header && msg.parent_header.msg_id,
      executionState: type === "status" && msg.content ? msg.content.execution_state : undefined,
    };
  }

  // --- state tracking -----------------------------------------------------------

  function onOutgoing(data) {
    const s = summary(decode(data));
    if (s && s.type === "execute_request" && s.id) {
      state.pending.set(s.id, Date.now());
      publish();
    }
  }

  function onIncoming(data) {
    state.framesSeen++;
    const s = summary(decode(data));
    if (!s) {
      state.framesUndecoded++;
      return;
    }
    state.lastMessageAt = Date.now();
    if (s.type === "status" && s.executionState) state.kernelStatus = s.executionState;
    const finished =
      (s.type === "execute_reply" && s.parent) ||
      (s.type === "status" && s.executionState === "idle" && s.parent);
    if (finished && state.pending.delete(s.parent)) publish();
    else if (s.type === "status") publish();
  }

  function snapshot() {
    return {
      provider: "kernel_ws",
      openSockets: state.openSockets,
      everConnected: state.everConnected,
      pendingExecutions: state.pending.size,
      oldestExecutionMs: state.pending.size ? Date.now() - Math.min(...state.pending.values()) : 0,
      kernelStatus: state.kernelStatus,
      msSinceMessage: state.lastMessageAt ? Date.now() - state.lastMessageAt : null,
      framesSeen: state.framesSeen,
      framesUndecoded: state.framesUndecoded,
    };
  }

  function publish() {
    document.dispatchEvent(new CustomEvent(EVENT, { detail: JSON.stringify(snapshot()) }));
  }

  // --- WebSocket instrumentation ------------------------------------------------

  const NativeWebSocket = window.WebSocket;

  function instrument(ws) {
    const nativeSend = ws.send;
    ws.send = function (data) {
      try {
        onOutgoing(data);
      } catch (_) {}
      return nativeSend.call(this, data);
    };
    ws.addEventListener("open", () => {
      state.openSockets++;
      state.everConnected = true;
      publish();
    });
    ws.addEventListener("close", () => {
      state.openSockets = Math.max(0, state.openSockets - 1);
      publish();
    });
    ws.addEventListener("message", (event) => {
      try {
        onIncoming(event.data);
      } catch (_) {}
    });
  }

  window.WebSocket = new Proxy(NativeWebSocket, {
    construct(target, args, newTarget) {
      const ws = Reflect.construct(target, args, newTarget);
      try {
        if (KERNEL_URL.test(String(args[0]))) instrument(ws);
      } catch (_) {}
      return ws;
    },
  });

  // Periodic publish so the isolated world always has a fresh snapshot.
  setInterval(publish, 2000);
  document.addEventListener("colab-idle-guard:request", publish);
})();
