# Test edits (R4)

Allowed edits of existing tests: (a) deleting tests of code deleted in step 0.1 (the legacy Textual UI),
(b) mechanical import-path / patch-target updates when a symbol moved and no shim can keep the old path,
(c) new tests. Every (a) and (b) edit is listed here with its justification and commit.

## Phase −1 (safety net)

No existing test was edited, removed, skipped or loosened. Only new tests were added, all under
`tests/refactor/` (see `baseline.md`).
