# Patch mutation checks (R6)

A string patch target that still *resolves* after a move (`tests/refactor/test_patch_targets.py`) may no
longer *intercept* the call: the test then passes without testing anything. For every patched target that
lies inside moved code, make the target raise for a moment (for example
`raise AssertionError("mutation")` as its first line), run the tests that patch it, and record here that at
least one of them fails; then restore it.

| Phase.step | Target | Mutation | Tests that patch it | Failed with the mutation | Restored |
|---|---|---|---|---|---|

## Phase −1 (safety net)

No code moved, so there is nothing to check yet. The baseline has 92 distinct string patch targets in
182 uses (`tests/refactor/test_patch_targets.patch_targets()`); one of them is patched with `create=True` and
intercepts nothing at the baseline (`found-bugs.md` #1).

## Phase 0

* 0.2 `ophion/gtkui/i18n.py` → `ophion/i18n.py`: no test patches a name of either module (grep of `tests/` for
  `gtkui.i18n` / `ophion.i18n` patch strings: none), so no mutation check applies. The shim re-exports the same
  objects (`tests/refactor/test_shims.py`).
* 0.3 `ophion/gtkui/validation.py` → `ophion/validation.py`: no test patches a name of either module (none in
  `tests/`; `tests/test_gtkui_components.py` imports from the old path, unchanged), so no mutation check applies.
  The shim re-exports the same objects (`tests/refactor/test_shims.py`).
* 0.3, first attempt (66d7600, reverted in c6c395b, R13): the byte-identical move broke
  `validate_display_name`, whose function-level `from ..inventory.store import validate_name` resolved beyond the
  top-level package from `ophion/validation.py` (2 failures in `tests/test_gtkui_inventory_store.py`). The retry
  changes that import line to `.inventory.store` (R2: import lines only) and adds
  `test_moved_modules_function_level_imports_resolve`. Mutation check: with the old line restored, that test fails.
