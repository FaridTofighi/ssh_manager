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
