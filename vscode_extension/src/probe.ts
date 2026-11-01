// The Colab detection probe and its parser (pure; see colab_detector.ts for how it is run).

/** One read-only line: prints whether Colab's runtime marker is present. Defines no variables. */
export const PROBE_CODE =
  'print("CIG_COLAB=" + ("1" if __import__("os").environ.get("COLAB_RELEASE_TAG") else "0"))';

export type ProbeResult = "yes" | "no" | "error";

export function parseProbeOutput(text: string): ProbeResult {
  const m = /CIG_COLAB=([01])/.exec(text);
  if (!m) return "error";
  return m[1] === "1" ? "yes" : "no";
}

/**
 * Releases the Colab runtime from inside its kernel using Colab's own helper,
 * google.colab.runtime.unassign(). That helper POSTs to the VM's runtime
 * service (TBE_RUNTIME_ADDR/unassign) and then asks a *browser* frontend to
 * disconnect via output.eval_js. VS Code has no such frontend, so eval_js would
 * block forever; it is replaced by a no-op for the duration of the call and
 * restored afterwards. Runs in a fresh namespace: no user variables are touched.
 */
export const UNASSIGN_CODE =
  'exec("import google.colab.output as o, google.colab.runtime as r\\n' +
  "f = o.eval_js\\n" +
  "o.eval_js = lambda *a, **k: None\\n" +
  "try:\\n    r.unassign()\\n    print('CIG_UNASSIGNED')\\n" +
  'finally:\\n    o.eval_js = f", {})';

export type UnassignResult = { state: "released" } | { state: "error"; detail: string } | { state: "pending" };

export function parseUnassignOutput(text: string): UnassignResult {
  if (text.includes("CIG_UNASSIGNED")) return { state: "released" };
  const err = /(\w*(?:Error|Exception)\b[^\n]*)/.exec(text);
  if (err || text.includes("Traceback")) return { state: "error", detail: (err ? err[1] : "Python error").slice(0, 300) };
  return { state: "pending" };
}
