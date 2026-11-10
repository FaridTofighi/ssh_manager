# Phase 0 report — cleanup and layer boundaries (R15)

Branch `refactor/architecture`, from `refactor-phase-m1-done` (26c0c6a) to `refactor-phase-0-done`.
Prerequisite gate on 26c0c6a: ok (suite 1775 passed, 1 skipped).

## Commits

| Commit | Subject | Gate | Suite (passed / skipped / failed) |
|---|---|---|---|
| ac7d80e | refactor(0.1): remove the legacy Textual UI | ok (snapshots ok) | 1771 / 1 / 0 (−4 removed, R4 (a)) |
| dcdab99 | refactor(0.2): neutral i18n module | ok (snapshots ok) | 1772 / 1 / 0 (+1 new) |
| 66d7600 | refactor(0.3): neutral validation module | **FAILED** (step 5) | 1771 / 1 / **2** |
| c6c395b | Revert "refactor(0.3): neutral validation module" | (revert, R13) | — |
| a32b2d5 | refactor(0.3): neutral validation module (retry) | ok (snapshots ok) | 1774 / 1 / 0 (+2 new) |
| 0edbfb2 | refactor(0.4): layer-boundary ratchet test | ok | 1778 / 1 / 0 (+4 new) |
| 6abdfd3 | refactor(0.5): pyproject.toml (metadata and tool settings only) | ok | 1778 / 1 / 0 |
| 9ff777d | refactor(0.6): optional CI workflow | ok, `--phase-end` (snapshots, benchmarks) | 1778 / 1 / 0 |

Floor: 1747 − 4 removed legacy tests = 1743 (`tests/refactor/baseline_counts.json`). The suite only grew apart
from the four tests removed under R4 (a).

The revert commit carries git's default message (no `refactor(…)` prefix and no attribution lines). It was left
as is because amending is not allowed (R13).

## Steps

**0.1 Legacy Textual UI removed.** Evidence from before the deletion:

* `ophion/ui.py` was imported only by `ophion/main.py` and `tests/test_ui_helpers.py`, and `ophion/terminal.py`
  only by `ui.py`.
* `textual` / `rich` were imported only in `ui.py` and `terminal.py`.
* `parse_extra_options` / `format_extra_options` were used only in `ui.py` and that test.
* No entry point referenced them: root `main.py` → `ophion.gtk_main`, `ophion/__main__.py`,
  `ssh_manager/__main__.py`, AppRun (`usr/share/ophion/main.py`), `install.sh` (`python -m ophion`) and the
  `.desktop` file (`Exec=ophion %U`).

The deleted files are `ui.py` (1912 lines), `terminal.py`, `ophion/main.py` and `tests/test_ui_helpers.py`
(4 tests). `ophion/__init__.py` `_EXPORTS` listed `parse_args`, `run_app` and `main` from `ophion.main`; these
were removed. Stale references were fixed in the README (3 places), the `ophion/gtkui/__init__.py` docstring,
`packaging/appimage/README.md` and the `packaging/appimage/tests/smoke.sh` comment. The three modules are in
`tests/refactor/api_allowlist.txt`.

**0.2 `ophion/i18n.py`.** The module was moved with `git mv` and is byte-identical (checked with `cmp`).
`ophion/gtkui/i18n.py` re-exports `DOMAIN`, `_`, `_translation`, `gettext` and `ngettext`. The identity test is
`tests/refactor/test_shims.py`. The six core importers now use `ophion.i18n`; the 89 `gtkui` importers are
unchanged.

**0.3 `ophion/validation.py`.** This step needed a revert and a retry (see Deviations, item 2). The shim
re-exports every name, and the identity test was extended to cover them. `tunnels/model.py` now imports
`ophion.validation`.

**0.4 `tests/test_architecture_boundaries.py`** with `tests/architecture_allowlist.txt`. It checks four things:

* (a) module-level `gi` / `gtkui` imports outside `gtkui` must be listed in the allowlist;
* (b) a stale entry fails;
* (c) runtime SCCs must be subsets of the baseline's four;
* (d) classes must not grow, and a new class must stay ≤ 600 lines.

A non-vacuity test feeds each check a violation. A mutation check also passed: an extra `ophion.gtk_main`
entry fails the test. The test runs in the suite and also in gate step 4, so `--quick` runs it too.

**0.5 `pyproject.toml`.**

* Version: `version = "0.0.0"`. There is no static version to reuse: `native_transfer.app_version()` reads
  `git describe` or the `VERSION` file that `install.sh` writes, so the file comments that the packaging scripts
  are authoritative.
* Python and dependencies: `requires-python >=3.10` (`install.sh` `MIN_PYTHON`; the AppImage bundles CPython
  3.10). The dependencies are paramiko, cryptography, pexpect, keyring and certifi, the top-level libraries of
  `requirements.lock` (see `packaging/appimage/README.md`); `dev` extras are listed.
* ruff: `E9` + `F` with per-file ignores for exactly today's 5 findings, so `ruff check` passes.
* mypy: strict, opt-in for `ophion/i18n.py` and `ophion/validation.py`; it passes.
* Gate step 11 runs both tools report-only, never with `--fix`. When a tool is missing, the step skips it with
  a notice; this was checked with ruff and mypy removed from `PATH`.
* `install.sh`, `build.sh` and `AppRun` are byte-identical (sha256 before and after). Both packaging flows copy
  only `main.py`, `ophion/` and `ssh_manager/`, and pip installs only the pinned locks, so no flow reads
  `pyproject.toml`.
* pytest now reports `configfile: pyproject.toml`. The file has no `[tool.pytest]` table, the rootdir is
  unchanged, and the suite count is identical.

**0.6 `.github/workflows/tests.yml`.** It runs the suite through `tools/run_gtk_tests.py` in an `ubuntu:24.04`
container with the apt packages `install.sh` lists, plus Xvfb, D-Bus and the Python libraries. The file was
only added. It has **not** been run: there is no CI here, and only the YAML parse was checked.

## Metrics (baseline `tests/refactor/metrics_baseline.json` vs `refactor-phase-0-done`)

| Metric | Baseline | Now |
|---|---|---|
| modules (`ophion/`) | 238 | 237 |
| LOC (`ophion/`) | 68 904 | 66 737 |
| core → GTK imports, module level | 11 | **5** |
| core → GTK imports, any level | 15 | 9 |
| runtime SCCs / module-level SCCs | 4 / 0 | 4 / 0 |
| `except Exception` / followed by `pass` | 204 / 55 | 196 / 52 (in the deleted UI) |
| thread modules / Thread+idle_add+timeout_add uses | 24 / 158 | 24 / 158 |
| `win._…` reads in controllers / `getattr(manager, "store")` | 361 / 10 | 361 / 10 |
| `TerminalTab` / `TerminalNotebook` / `SSHManagerWindow` lines | 2123 / 1187 / 727 | unchanged |

Six modules no longer import GTK at module level: `host_key_preflight`, `multirun.engine`, `multirun.report`,
`ssh_keys`, `tunnels.engine` and `tunnels.model`. Five remain, all of them GUI modules: `connector` (GLib),
`gtk_app`, `gtk_terminal`, `password_dialog` and `vte_bridge`.

**Benchmarks (R10).** These compare `pre-refactor` with the working tree, as the median of 3 alternating
rounds. The phase-end gate passed, and a second run on 9ff777d recorded the numbers below. Every metric is
within +10 % or +5 ms. The largest increase is `tree.rebuild(refresh=True)` at 183.2 → 201.1 ms (+9.8 %).
Phase 0 did not touch the tree code, and in the same run `tree.rebuild()` went 142.5 → 141.3 ms. The increase
is close to the limit, so it is noted here.

| Operation | pre-refactor ms | now ms |
|---|---|---|
| startup (main window, 1,200 hosts, best of 5) | 107.2 | 104.7 |
| startup (new window) | 315.1 | 312.9 |
| search typing (5 keystrokes) | 729.6 | 710.4 |
| move host (there and back) | 507.3 | 503.9 |
| add host → rebuild → select | 315.0 | 319.6 |
| tree.rebuild() / (refresh=True) | 142.5 / 183.2 | 141.3 / 201.1 |
| Preferences dialog | 146.3 | 151.2 |
| manager.refresh | 44.4 | 45.2 |

**Snapshots (R11).** All 118 images are within tolerance in every gate that rendered them (0.1, 0.2, 0.3 retry,
phase end).

## Test edits (R4)

See `docs/refactor/test-edits.md`:

* (a) `tests/test_ui_helpers.py` deleted (4 tests);
* (b) `tests/rename_allowlist.txt` row 19 now names `ophion/i18n.py`, where the `ssh-manager` gettext domain
  now lives.

New tests:

* `tests/refactor/test_shims.py` (3);
* `tests/test_architecture_boundaries.py` (4).

## Found bugs (R3)

None new. The five ruff findings (four unused imports and one f-string without placeholders) are lint, not
behaviour. They are listed as ignores in `pyproject.toml` and left unfixed.

## Deviations

1. **Initial allowlist: 5 entries, not the 8 the spec names.** `gtk_main`, `config_viewer` and `rdp/engines`
   import `gi` / `gtkui` only inside functions. Rule (a) covers module level, so those entries would be stale,
   and rule (b) makes a stale entry fail. They are still visible as "core → GTK any level" in `arch_metrics`.
2. **0.3 was not a pure move.** The spec says `validation.py` "imports only `.i18n`". That holds at module level
   only: `validate_display_name` has a function-level `from ..inventory.store import validate_name`. The
   byte-identical move made it resolve beyond the package, and the gate failed
   (`tests/test_gtkui_inventory_store.py`, 2 tests). The commit was reverted (R13). The retry changes only that
   import line to `.inventory.store` (R2: import lines) and adds a test that resolves every relative import of
   the moved modules, including function-level ones. A mutation check confirmed that this test fails with the
   old line.
3. **API allowlist for re-exported functions.** `tools/api_surface.py` reads the source statically, so a shim's
   `from ..x import f` shows as `import`, not `function(…)`. The 2 + 10 re-exported functions are allowlisted
   with a justification. The identity tests prove they are the same objects, which is stronger than comparing
   signatures.
4. **Commit size.** 0.1 deletes 2263 lines (dead files deleted whole). Every other commit is under 300 changed
   lines.
5. **`tools/refactor_gate.sh` changed in 0.4 and 0.5.** Step 4 now runs the boundary test, and a report-only
   step 11 was added. The spec requires both to be in the gate.

## Follow-ups (decisions for the owner)

* `ophion/launcher.py` (`ConnectionLauncher`, tmux/terminal launcher) was used only by the deleted
  `ophion/main.py`. It is kept because it is still exported from `ophion/__init__.py`.
* `ophion/inventory/manager.py:75` has a comment that names `gtkui.validation.validators_for`, which is now
  `ophion.validation`. It is only a comment; the file was not named in the step.
* `docs/rename-audit.md` still mentions the Textual UI. It is a historical audit and is left as written.
* `ophion/legacy_paths.py` imports `gi.repository` (Gio, GLib) inside a function ("any level" list).
* The CI workflow is untested until it runs on GitHub.
