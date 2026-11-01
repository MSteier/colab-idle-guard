# Security and Privacy

## Summary

Colab Idle Guard runs entirely on your computer. It opens no internet
connections of its own, collects no telemetry, and never records what you
type, your code, outputs or file names.

## What is collected and where it goes

| Data | Source | Stored | Sent anywhere? |
|---|---|---|---|
| Activity *kind* (`click`, `key`, `scroll`, `edit`, `focus`, …) and its age in ms | Extensions | Only the latest timestamp, in memory; counts in logs | No |
| Execution state (`idle`/`busy`/`unknown`), number of running cells | Extensions | Memory; state changes in logs | No |
| Runtime connection state | Extensions | Memory; changes in logs | No |
| Notebook ID: `nb_` + first 128 bits of SHA-256 of the notebook URL/URI with a fixed prefix | Extensions | Memory | No. The URL itself is never sent. |
| Foreground process **name** (e.g. `chrome.exe`) | Windows API, sampled once per second | Last 3 minutes, in memory | No |
| Settings, pairing token, logs | App | `%APPDATA%` / `%LOCALAPPDATA%\ColabIdleGuard` | No |

Not collected: keystroke values, typed text, mouse coordinates, page
content, code, outputs, notebook titles or paths, Google account details.
No global keyboard or mouse hooks are installed.

The Chrome extension's kernel observer sees Colab's kernel messages in the
page, but reads only message *headers* (type and ID) and the busy/idle
status; message content is never stored or forwarded. The VS Code extension
runs one read-only line in the kernel to recognise Colab runtimes (see the
user manual §5.3) and, only when releasing, Colab's own
`google.colab.runtime.unassign()`.

## Local API

The app's HTTP API exists only for the two extensions.

- Bound to **127.0.0.1** only, with `SO_EXCLUSIVEADDRUSE`, so it is not
  reachable from the network and another process can't take over the port.
- **Authentication:** every request (except a minimal health check) needs
  `Authorization: Bearer <token>`, compared in constant time.
- **DNS-rebinding protection:** the `Host` header must be
  `127.0.0.1:<port>` or `localhost:<port>`.
- **No web access:** requests with a web `Origin` are refused, CORS preflight
  is always denied, and no CORS headers are sent. Only `chrome-extension://`
  origins are accepted: the official extension ID
  (`glchpgialnkmebkjemimjgfijpnkdcdi`, fixed by the manifest key) or one
  pinned custom ID (first authenticated use; reset in Settings).
- **Input validation:** JSON only, 64 KiB maximum, strict field types and
  ranges, identifier patterns, at most 200 events / 50 notebooks per request.
  Rejected requests close the connection. Fuzz-tested with 600 random
  requests (docs/TEST_REPORT.md).
- **Commands** (release requests to extensions) carry a random nonce, expire,
  are delivered at most once, and results are accepted only from the
  instance they were sent to.

## Pairing token

- 256 bits from Python's `secrets` module.
- Stored in `%LOCALAPPDATA%\ColabIdleGuard\token.bin`, encrypted with
  **Windows DPAPI** (current-user scope, application-specific entropy). Other
  Windows accounts and copies of the file on other machines cannot decrypt
  it. Any program running as *your* user could, which is inherent to
  user-level secrets, and is also how the VS Code extension pairs automatically.
- Chrome keeps its copy in `chrome.storage.local` (this profile only).
  VS Code keeps a manually entered token in its SecretStorage.
- **Never logged:** a redaction filter removes the token and any `Bearer …`
  value from every log record; diagnostic reports are re-redacted and checked.
- Shown only on explicit request (**Copy pairing token**, or
  `--show-pairing-token` on the command line).

## Destructive actions

Releasing a runtime deletes its state (see the user manual §1.1).
Safeguards:

- Automatic release is **off** by default. Enabling it requires an explicit
  consent dialog, and revoking consent turns it off.
- Never while a cell is running or while state is unknown. These protections
  cannot be disabled.
- Always preceded by a cancellable warning. All conditions are rechecked
  before acting, and the extension rechecks again on its own.
- Success is confirmed independently. Failures are reported and never retried
  automatically within the same idle period.
- No Colab deletion API is called directly. Only Colab's own user-facing
  controls are used (*Disconnect and delete runtime*, `runtime.unassign()`).

## Permissions

| Component | Permissions |
|---|---|
| Chrome extension | `storage`, `alarms`; host access `http://127.0.0.1/*`; content scripts only on `https://colab.research.google.com/*` |
| VS Code extension | Jupyter kernel access (granted by you), SecretStorage, workspace state |
| App | Per-user files and the per-user `Run` registry key (only if *Start with Windows* is on); no administrator rights |

## Reporting a problem

If you find a security issue, export a diagnostic report (it contains no
secrets) and describe the issue to the maintainer privately. Don't post
tokens or diagnostic contents publicly.
