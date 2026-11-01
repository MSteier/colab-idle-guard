// Decides whether a kernel runs on a Colab runtime.
//
// Neither the Jupyter extension API nor the Colab extension exposes which server
// a kernel belongs to (docs/COLAB_RUNTIME_RESEARCH.md §3.3-3.4). The only
// official way to find out is to ask the kernel: one read-only line that
// checks for Colab's runtime marker and prints 0/1. It defines no variables
// and touches no files. It runs at most once per kernel session, only when the
// kernel is idle. The Jupyter extension asks the user to allow kernel access
// the first time.

import * as vscode from "vscode";
import type { Kernel } from "@vscode/jupyter-extension";
import { PROBE_CODE, parseProbeOutput, type ProbeResult } from "./probe";

export async function probeKernel(kernel: Kernel, timeoutMs = 15000): Promise<ProbeResult> {
  if (kernel.language && kernel.language.toLowerCase() !== "python") return "no";
  const cts = new vscode.CancellationTokenSource();
  const timer = setTimeout(() => cts.cancel(), timeoutMs);
  const decoder = new TextDecoder();
  let text = "";
  try {
    for await (const output of kernel.executeCode(PROBE_CODE, cts.token)) {
      for (const item of output.items) text += decoder.decode(item.data);
      if (text.length > 4096) break;
    }
    return parseProbeOutput(text);
  } catch {
    return "error";
  } finally {
    clearTimeout(timer);
    cts.dispose();
  }
}
