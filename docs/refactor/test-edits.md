# Test edits (R4)

Allowed edits of existing tests: (a) deleting tests of code deleted in step 0.1 (the legacy Textual UI),
(b) mechanical import-path / patch-target updates when a symbol moved and no shim can keep the old path,
(c) new tests. Every (a) and (b) edit is listed here with its justification and commit.

## Phase −1 (safety net)

No existing test was edited, removed, skipped or loosened. Only new tests were added, all under
`tests/refactor/` (see `baseline.md`).

## Phase 0 (cleanup and layer boundaries)

| Commit | Edit | Kind | Justification |
|---|---|---|---|
| `refactor(0.1)` | deleted `tests/test_ui_helpers.py` (4 tests: `parse_extra_options` / `format_extra_options` of `ophion/ui.py`) | R4 (a) | The tests exercised only the legacy Textual UI, deleted in the same commit. `baseline_counts.json` `removed_legacy_tests` 0 → 4. |

Follow-up (not done in 0.1): `ophion/launcher.py` (`ConnectionLauncher`) was used only by the deleted
`ophion/main.py`; it stays because it is still exported from `ophion/__init__.py` (`_EXPORTS`), so removing it
would shrink the API surface. `docs/rename-audit.md` still mentions the Textual UI: it is a historical audit and
is left as written.
