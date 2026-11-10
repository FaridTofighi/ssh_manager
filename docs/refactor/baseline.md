# Refactor baseline (Phase −1: the safety net)

The state the refactor starts from, and the net that must notice any behaviour change. Branch
`refactor/architecture`, starting commit tag `pre-refactor` (`83b820e`). No production code changes in this
phase: only `tools/`, `tests/refactor/` and `docs/refactor/`.

## Environment

| | |
|---|---|
| Python | 3.13.5 |
| GTK | 3.24.49 (`libgtk-3-0t64` 3.24.49-3) |
| VTE | 0.80.1 (`gir1.2-vte-2.91` 0.80.1-1) |
| PyGObject | 3.50.0 |
| paramiko / cryptography / pytest | 3.5.1 / 43.0.0 / 8.4.2 |
| X server for tests | Xvfb 21.1.16 through `tools/run_gtk_tests.py` (1920 × 1200, 96 dpi, Adwaita, Sans 10) |
| Not installed | `coverage` (see deviations), `shellcheck` |

## Test suite at the baseline (§2.1)

Three runs of the full suite with `tools/run_gtk_tests.py` in a checkout of `pre-refactor`:

| Run | Collected | Passed | Skipped | Failed | xfail | Wall time |
|---|---|---|---|---|---|---|
| 1 | 1748 | 1747 | 1 | 0 | 0 | 455 s |
| 2 | 1748 | 1747 | 1 | 0 | 0 | 455 s |
| 3 | 1748 | 1747 | 1 | 0 | 0 | 455 s |

- The skip: `tests/test_installer.py:56` (shellcheck is not installed; `bash -n` passed).
- **Flaky tests: none.** The per-test results of the three runs are identical (and of three more runs
  of a `git archive` export of the same commit, where three app-icon tests skip as "not a git checkout":
  1744 passed, 4 skipped, identical again).
- Machine-readable: `tests/refactor/baseline_counts.json` (the gate's floor: passed ≥ 1747).
- With the safety net in place the suite has **1775 passed, 1 skipped** (28 new tests under
  `tests/refactor/`).

## Architecture metrics (§2.2): `tools/arch_metrics.py` → `tests/refactor/metrics_baseline.json`

AST only, nothing imported. Compared with Appendix A:

| Item | Appendix A | Measured | Note |
|---|---|---|---|
| Modules / lines of `ophion/` | 238 / ≈ 68 900 | 238 / 68 904 | |
| `TerminalTab` | 2123 lines, 174 methods | 2123, 174 | |
| `TerminalNotebook` | 1187, 120 | 1187, 120 | |
| `RdpSessionTab` / `FileBrowserTab` / `SSHManagerWindow` | 1286 / 942 / 727 | 1286 / 942 / 727 | |
| `ui.py` / `SSHManagerApp` | 1912 / 1477 | 1912 / 1477 | |
| Runtime import cycles | 4 SCCs, no module-level cycle | the same 4, none at module level | `theme.accent↔theme.tokens`, `config_viewer↔gtkui.dialogs.config_viewer`, `gtk_terminal↔gtkui.local_terminal`, `controllers.tree↔views.favorites↔views.sidebar` |
| Core modules importing `gi` / `gtkui` | 14 listed | 11 at module level, 15 at any level | the appendix's list includes function-level imports; the 15th is `legacy_paths` (inside a function) |
| `except Exception` | 204 | 204 | |
| … whose body is only `pass` | 49 | **55** | the appendix used a text search (`except Exception:` newline `pass`), which misses six handlers with a comment on the `except` line (`connector.py:461`, `:472`, `credentials.py:222`, `gtk_terminal.py:720`, `gtkui/theme/loader.py:149`, `secure_store.py:55`). The AST count is the baseline. |
| Modules using `threading.Thread` | 24 | 24 | |
| `Thread` / `idle_add` / `timeout_add` uses | 164 | 158 (code), 165 (all text) | the AST counts code only; the other 7 are mentions in docstrings and comments; no definition gives exactly 164 |
| `win._<name>` in `gtkui/controllers` | `_notify` 93, `_settings` 49, `_credential_store` 34, `_log_event` 29, `_ui` 23, `_notify_error` 21, `_secure_store` 19, `_save_ui_state` 17 | the same; 361 in all (23 names) | |
| `getattr(…manager, "store", None)` | — | 10 | |
| Classes over 600 lines | — | 12 | |
| Functions over 100 lines | — | 16 | |

## API surface (§2.3): `tools/api_surface.py` → `tests/refactor/api_surface.json`

240 modules (`ophion/` and the `ssh_manager` shim), 6 277 top-level names (including names bound by
imports), 479 classes with 7 298 members (methods with their signatures, class attributes, `self.<name>`
attributes; underscore names included). `tests/refactor/test_api_surface.py`: the current surface contains the
snapshot; the allowlist `tests/refactor/api_allowlist.txt` is empty. String patch targets
(`tests/refactor/test_patch_targets.py`): 92 distinct targets in 182 uses, all resolve; one is patched with
`create=True` and intercepts nothing (`found-bugs.md` #1).

## Characterization tests (§2.4): `tests/refactor/`, goldens in `tests/refactor/golden/`

Goldens are written once on the baseline (`OPHION_UPDATE_GOLDENS=1`) and compared afterwards; each was run
at least twice more without a difference, alone and in the full suite.

| Area | Test | What is pinned |
|---|---|---|
| Spawn plan (1) | `test_spawn_golden.py` | 15 host cases (credential / prompted / legacy password, without sshpass, key file, agent, ProxyJump, ProxyCommand, forwards, extra options, `PermitLocalCommand no`, own `LocalCommand`, alias only, profile env), the split pane, the local shell, protocols: path (pre-flight / spawn / prompt), argv, env **in order**, spawn and pty flags, log lines; R8 asserted for each |
| Auth / host keys (2) | `test_auth_golden.py` | the 5 `ssh_output` captures and 6 typical messages through `classify_exit` (exit 0, 5, 6, 255, killed), `detect_changed` / `detect_unknown`, prompts; a real tab ending with each code after that output: status, banner, log, retry, host-key bar, `session-closed` |
| Connection order (3, R7) | `test_connect_order.py` | the ordered `connect` / `connect_after` calls on the window (6), the notebook (1), a tab (6) and its terminals (14, then 12 for a split) |
| Broadcast (4) | `test_broadcast_characterization.py` | typing mirrored to the included, connected sessions (off / on / excluded / disconnected), `close_current`, the Paramiko remote close; `q` / `r` / Close / chips were already covered (`test_broadcast_ended.py` and others) |
| Tiles (5) | `test_tile_geometry_golden.py` | 1, 2, 4, 7 sessions in both densities: tile frames, header buttons, the window a press reaches at the zoom button's centre and 3 px inside each edge (all 75 on the button's own input window), and a zoomed tile |
| Persistence (6) | `test_persistence_golden.py` | `settings.json`, `ui_state.json`, `smart_groups.json` round trips; the populated database (schema, counts, `user_version` 2, content); plain and encrypted `.ophion` import and re-export; keybox, `passwords.json`, `macros.enc` with the known master password; frozen identifiers (keybox and export formats, schema version, keyring service, HKDF info) |
| Security (R8) | `test_security_invariants.py` | `aead.py`, `keybox.py`, `secure_store.py`, `unlock.py` byte-identical to the tag; no passphrase in error texts |

Fixtures (inputs written once by `tests/refactor/make_fixtures.py`) are in `tests/refactor/fixtures/`.

### Coverage (7): `tools/line_coverage.py` → `tests/refactor/coverage_baseline.json`

| Area | Lines run / executable |
|---|---|
| `gtk_terminal.py` | 2367 / 2828 (83.7 %) |
| `gtk_app.py` | 650 / 797 (81.6 %) |
| `gtkui/controllers/*` | 5546 / 7189 (77.1 %) |
| `inventory/*` | 1138 / 1280 (88.9 %) |
| `gtkui/controllers/native_transfer.py` | 164 / 222 (73.9 %) |

Per function in the JSON; per method of `TerminalTab` / `TerminalNotebook` in `terminal-inventory.md`
(147 of 174 and 97 of 120 methods run by a test at the baseline, before the broadcast tests were added). Of the
broadcast-area methods without a test, `feed_input`, `close_current` and `_on_paramiko_remote_closed` are now
covered; `_on_reconnect_clicked` and `_on_close_button_clicked` are **connected nowhere** (dead handlers at the
baseline; Phase 0 decides).

## The gate (§2.7): `tools/refactor_gate.sh`

| Step | Tool | Time |
|---|---|---|
| 1 compileall | `python3 -m compileall` | 1 s |
| 2 patch targets | `tests/refactor/test_patch_targets.py` | 2 s |
| 3 API surface | `tools/api_surface.py --check` | 1 s |
| 4 ratchets | `tools/arch_metrics.py --check` | 2 s |
| 5 full suite | `tools/run_gtk_tests.py` (floor 1747 passed) | 485 s |
| 6 security | `test_security_invariants`, the spawn and persistence goldens, 10 existing security test files | 10 s |
| 7 cross-version | `tools/refactor_crossversion.sh` | 3 s |
| 8 smoke | `tools/refactor_smoke.sh` | 8 s |
| 9 snapshots | `tools/refactor_snapshots.py` (with `--snapshots`, `--phase-end`, or a GTK change in the last commit) | 71 s |
| 10 benchmarks | `tools/refactor_bench.py check` (with `--phase-end`) | 97 s |

On the safety-net commit `8f881ef` with `--phase-end`: **all ten steps ok, 11 min 20 s**.

## Deviations from the phase document

1. **Coverage without `coverage`.** It is not installed and the refactor adds no dependency (nothing is
   downloaded): `tools/line_coverage.py` is a pytest plugin on `sys.monitoring` (Python 3.12+, standard
   library) that records executed lines of the watched files, threads included.
2. **AppImage smoke skipped** ("skipped: tooling"): `packaging/appimage/tests/smoke.sh` runs in fresh Docker /
   Podman containers it pulls, on an AppImage built from the tree; it runs only with `OPHION_SMOKE_APPIMAGE=1`.
3. **Smoke start through the tests' `WindowSandbox`**, not `python3 -m ophion`: the real start opens the unlock
   or first-run dialog and waits. The window is built as the app builds it, in a temporary HOME, shown ~5 s.
4. **Snapshots and benchmarks are compared with the tag, live.** Both export `pre-refactor` (`git archive`)
   and measure it next to the working tree, instead of committed images or numbers: two renders of unchanged
   code differ by up to 0.45 % of an image's bytes in about 17 of 118 images (times, paths, animation state
   rendered by `tools/ui_snapshots.py`, which this phase may not change), so the tolerance is 1 % per image;
   stored benchmark numbers drifted by 11–14 % on unchanged code, so the two builds are measured alternately
   (3 rounds, medians). A metric regresses at +10 % **and** +5 ms (operations of a few milliseconds vary more
   than 10 %). `tests/refactor/bench_baseline.json` records the baseline numbers.
5. **Stable names instead of line numbers in the connection-order golden:** a handler is named by
   `__name__` (`<lambda>` for lambdas), so a method moved to a helper class keeps its record while a reordered
   or dropped connection shows.
6. **The review document** `ophion-architecture-review-v2.md` is not in the repository; Appendix A of the
   phase document was used and re-verified (table above).

## What could not be made deterministic

Nothing in the characterization tests: two fixes were needed and are in place. The tile geometry depended
on whether an earlier test had loaded the app's stylesheet (the test now loads it at a fixed font size); the
auth captures must be read as bytes (raw terminal output). The UI snapshot noise above is handled by the
tolerance, not removed (the renderer is out of this phase's scope).

## The net detects damage (deliberate mutations)

Each mutation was applied to the working tree of `8f881ef` (the gate green on it), the full gate run, and the
file restored with `git checkout -- <file>` (the tree clean afterwards; a gate run on it is green again):

| Mutation | Change | Gate | Caught by |
|---|---|---|---|
| An argv flag | `ophion/ssh_command.py`: `port_flag="-p"` → `"-P"` in `build_ssh_command` | **red** (exit 1): steps 5 and 6 | suite: 18 failed (1757 passed), among them `test_spawn_golden` (`spawn_credential_password.json`, `spawn_secondary_pane.json`); security step: 2 failed |
| Two `connect` calls reordered | `ophion/gtk_terminal.py` `_connect_mouse`: `button-press-event` and `button-release-event` swapped | **red** (exit 1): step 5 | suite: 1 failed (1774 passed): `test_connect_order` (`connect_order.json`) |
| A colour key dropped | `ophion/gtkui/controllers/ui_state.py` `UiState.to_dict`: the `host_colors` entry removed | **red** (exit 1): steps 5, 6 and 7 | suite: 9 failed (1766 passed), among them `test_persistence_golden` (`persist_ui_state.json`); security step: 1 failed; cross-version: all three reads differ from the old build's |

Gate run times on the mutations: 644 s, 518 s and 517 s (steps 9 and 10 skipped: no GTK commit, not a phase end).
