# Colab Idle Guard — Test Report

Phase 7, 2026-10-31. Windows 11, Python 3.12.10, Node 24.22, Chrome (current
stable), VS Code 1.144, `google.colab` 0.9.7, `ms-toolsai.jupyter` 2025.9.1.

## 1. Summary

| Suite | Tests | Result |
|---|---|---|
| Python (`pytest`) — unit, component, HTTP integration, full-stack system, fuzz, concurrency, randomised safety properties | 214 | ✅ all pass |
| Chrome extension (`node --test tests/js`) — probe, kernel hook, aggregation, service worker vs. real watchdog | 28 | ✅ all pass |
| VS Code extension (`npm test`) — state machine, probes, client and DPAPI token reader vs. real watchdog | 18 | ✅ all pass |
| Static analysis — `mypy --check-untyped-defs` (28 files), `tsc --strict` | — | ✅ no issues |
| Fresh environment — new venv from `requirements.txt` only | ✅ all pass (runtime deps: pystray, Pillow) |
| Live validation on real Colab **CPU** runtimes (Phases 3–6) | see §4 | ✅ |

Branch coverage (Python): **94%** across the three safety-critical modules
(`shutdown_controller`, `runtime_manager`, `execution_monitor`) and 78%
overall. The GUI modules (`desktop/tray.py`, `desktop/windows.py`) and the CLI
entry point are not exercised by pytest. They are covered by a scripted GUI
smoke test against the real app (15 steps) and by live use.

Re-run everything:

```powershell
.venv\Scripts\python.exe -m pytest -q
node --test "tests/js/*.test.*"
cd vscode_extension; npm test
```

## 2. Spec §12 traceability

| Spec test | Automated evidence | Live evidence |
|---|---|---|
| **1** Chrome Colab activity resets timer | `test_activity::test_01_*`, `test_api::test_heartbeat_activity_and_status`, JS `background.integration` | Phase 3: user scrolling/clicking reset the timer |
| **2** VS Code Colab activity resets timer | `test_activity::test_02_*`, VS Code `client.integration` | Phase 4: typing in a Colab cell reset the timer |
| **3** Other Windows apps do not reset | `test_activity::test_03_*` (foreground check rejects background-window events) | Phase 3: automation with the terminal in front rejected (`app_not_foreground`); Phase 4: 4 min untouched, idle rose steadily |
| **4** Chrome closes, VS Code continues | `test_shutdown::test_04_*`, `test_system::test_both_integrations_released_together` | Phase 3: closing the idle tab removed the notebook cleanly |
| **5** VS Code closes, Chrome continues | `test_shutdown::test_05_*`, `test_system::test_vscode_closes_while_busy_then_returns` | Phase 6: VS Code window reloads with Chrome connected |
| **6** Inactive for timeout → warning | `test_shutdown::test_06_*`, `test_system::test_automatic_release_end_to_end_over_http` | Phase 6 Test B: warning window after 1 min idle |
| **7** Executing cell prohibits shutdown | `test_shutdown::test_07_*`, `test_system::test_executing_cell_blocks_over_http` | Phases 3/4: 40 s cells shown as "Notebook executing" |
| **8** Cell starts during warning → cancel | `test_shutdown::test_08_*`, `test_system::test_cell_starts_after_command_delivered_extension_refuses` | — (automated only) |
| **9** Interaction during warning → cancel & reset | `test_shutdown::test_09_*`, `test_user_cancel_resets_timer` | Phase 6 Test B: "Keep runtime" cancelled and reset |
| **10** Lost integration blocks automatic release | `test_shutdown::test_10_*`, `test_lost_while_busy_never_assumed_idle`, `test_integration_lost_during_warning_cancels` | Phase 6: lost VS Code window blocked release until resolved |
| **11** Already disconnected → no operation | `test_shutdown::test_11_*`, `test_system::test_automatic_release_end_to_end_over_http` (no 2nd op) | Phase 6: after release, state stayed Disconnected |
| **12** Restart preserves config, re-evaluates | `test_api::test_12_*`, `test_system::test_watchdog_restart_mid_warning` | Several watchdog/tray restarts in Phases 3–6 |

## 3. Failure scenarios tested

| Scenario | Test | Outcome |
|---|---|---|
| Integration refuses release (cell started at the last moment) | `test_system::test_integration_refusal_*`, `test_cell_starts_after_*` | Reported as failed; never retried automatically |
| Integration dies after receiving the release command | `test_system::test_integration_dies_after_receiving_command` | Times out → failed; command never re-sent |
| One of two releases fails | `test_system::test_one_of_two_releases_fails` | Operation reported failed (partial success logged) |
| Watchdog restarts during warning | `test_system::test_watchdog_restart_mid_warning` | No release; idle clock restarts |
| Stale command result after restart | `test_system::test_stale_command_result_after_restart_is_rejected` | 409, ignored |
| Integration lost during warning | `test_shutdown::test_integration_lost_during_warning_cancels` | Warning cancelled; state unknown |
| Automatic release disabled during warning | `test_shutdown::test_final_check_fails_*` | Final pre-release check refuses |
| Adapter says done, runtime never shows disconnected | `test_shutdown::test_unconfirmed_release_*` | Failed after confirmation timeout |
| Colab reconnect flicker after release | `test_runtime_manager::test_flapping_*` | Not taken as confirmation |
| Notebook closed / tab closed while busy | `test_execution::test_notebook_closed_while_busy_*`, JS `background.integration` | Stays unknown (blocks) |
| Window reload without goodbye | `test_execution::test_returning_instance_*` | Idle entries cleared on return; busy stay unknown |
| Malformed / hostile API input (600 randomised requests) | `test_robustness::test_fuzzed_requests_*` | Only expected 2xx/4xx; no handler exception |
| 16 concurrent clients × 80 requests | `test_robustness::test_concurrent_clients_*` | No errors; consistent final state |
| Keep-alive after a rejected request | `test_api::test_keep_alive_*` | Fixed in Phase 3; regression-tested |
| Corrupt config / token file, port in use | `test_configuration`, `test_security`, manual smoke | Defaults + backup / new token / exit code 3 |
| Token leakage | `test_security::test_logs_never_contain_token`, `test_api::test_token_never_written_to_log`, `test_desktop::test_diagnostic_report_*` | Never in logs or diagnostic ZIP |

## 4. Randomised safety properties

`tests/test_invariants.py` runs 60 seeded simulations of 250 random events
each (activity, cell runs, crashes, reconnects, goodbyes, runtime changes,
pause/resume, cancel, settings changes, adapter outcomes) over many simulated
hours, with timeouts of 1, 5 and 30 min. At every release attempt it checks,
independently of the controller: P1 nothing busy · P2 nothing unknown · P3
target recently reported connected · P4 automatic only when enabled,
unpaused and idle ≥ timeout · P5 one operation at a time · P6 no retry after
a failure within the same idle episode. 299 releases were exercised.

**Mutation check:** three bugs were injected into the controller (ignore
running cells; ignore unknown state; halve the timeout). All three were
detected (P1, P2, P4) on seed 0 or 1.

**Bug found:** P6 exposed a real defect. After a failed release, events that
changed the displayed state (a short kernel busy blip, the page briefly
showing "disconnected", a moment of unknown state) erased the "do not retry"
memory, so the release was retried within the same idle episode. Fixed by
recording the failed episode separately
(`ShutdownController._failed_episode_ref`). Regression test
`test_shutdown::test_failed_release_not_retried_after_runtime_flicker`
fails without the fix and passes with it.

## 5. Live validation (real Colab, CPU runtimes only)

| What | Phase | Result |
|---|---|---|
| Kernel WebSocket protocol, DOM signals (running/pending/finished/interrupted) | 3 | Validated (research §7) |
| Installed Chrome extension end to end | 3 | Validated (research §7.5) |
| VS Code Colab detection (`COLAB_RELEASE_TAG`), kernel status, activity, auto-pairing | 4 | Validated (research §8) |
| VS Code release via `google.colab.runtime.unassign()` | 6 | Released in 7 s, confirmed (research §9.2) |
| Chrome release via *Disconnect and delete runtime*, full automatic pipeline incl. cancel | 6 | Released in 16 s, confirmed (research §9.1) |

## 6. Not covered / known gaps

- **TEST 8 live** (cell starting during a real warning) was not exercised on a
  live runtime. It is covered by unit and system tests and by the extension's
  own re-check immediately before acting.
- **GPU runtimes** were deliberately never used. Release mechanics are
  identical (same menu item / same `unassign()`), but GPU-specific UI states
  (e.g. quota dialogs) were not observed.
- **Colab rate limiting** of *Disconnect and delete runtime* was not triggered.
  A refusal would surface as a failed release (reported, not retried).
- **Colab UI changes**: the Chrome probe and release selectors match the UI as
  of 2026-10-31. A redesign degrades to "unknown" (blocks release) or to a
  reported release failure, never to a false idle.
