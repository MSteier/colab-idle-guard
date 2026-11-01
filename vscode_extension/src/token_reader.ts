// Reads the watchdog's pairing token directly from its DPAPI-protected file, so
// VS Code pairs automatically. DPAPI (CurrentUser scope) only decrypts for the
// same Windows account the watchdog runs under; this grants nothing that any
// other process of that user could not already do. Format: see
// colab_idle_guard/watchdog/security.py (magic prefix + DPAPI blob, fixed entropy).

import { execFile } from "node:child_process";
import * as fs from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";

const DPAPI_MAGIC = "CIGDPAPI1";
const PLAIN_MAGIC = "CIGPLAIN1";
const ENTROPY = "colab-idle-guard/api-token/v1";

export function tokenFilePath(env: NodeJS.ProcessEnv = process.env): string {
  const home = env.COLAB_IDLE_GUARD_HOME;
  if (home) return path.join(home, "data", "token.bin");
  const local = env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
  return path.join(local, "ColabIdleGuard", "token.bin");
}

function dpapiUnprotect(blob: Buffer): Promise<string> {
  // The blob is passed via stdin (base64), never on the command line.
  const script =
    "Add-Type -AssemblyName System.Security;" +
    "$b=[Convert]::FromBase64String([Console]::In.ReadToEnd().Trim());" +
    `$e=[Text.Encoding]::UTF8.GetBytes('${ENTROPY}');` +
    "$p=[Security.Cryptography.ProtectedData]::Unprotect($b,$e,[Security.Cryptography.DataProtectionScope]::CurrentUser);" +
    "[Console]::Out.Write([Text.Encoding]::UTF8.GetString($p))";
  return new Promise((resolve, reject) => {
    const child = execFile(
      "powershell.exe",
      ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
      { windowsHide: true, timeout: 15000 },
      (err, stdout) => (err ? reject(new Error("DPAPI decryption failed")) : resolve(stdout.trim())),
    );
    child.stdin?.end(blob.toString("base64"));
  });
}

/** Returns the token, or undefined if the watchdog has not created one / it cannot be read. */
export async function readWatchdogToken(file: string = tokenFilePath()): Promise<string | undefined> {
  let raw: Buffer;
  try {
    raw = await fs.readFile(file);
  } catch {
    return undefined;
  }
  try {
    const head = raw.subarray(0, DPAPI_MAGIC.length).toString("latin1");
    let token: string;
    if (head === DPAPI_MAGIC) {
      if (process.platform !== "win32") return undefined;
      token = await dpapiUnprotect(raw.subarray(DPAPI_MAGIC.length));
    } else if (head === PLAIN_MAGIC) {
      token = raw.subarray(PLAIN_MAGIC.length).toString("utf8");
    } else {
      return undefined;
    }
    return /^[A-Za-z0-9_-]{32,128}$/.test(token) ? token : undefined;
  } catch {
    return undefined;
  }
}
