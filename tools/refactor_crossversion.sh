#!/usr/bin/env bash
# Cross-version harness (R9): data written by the pre-refactor build loads in this one, and the other way round.
#
#   tools/refactor_crossversion.sh [TAG]        (default TAG: pre-refactor)
#
# The tag is exported (git archive) into a temporary folder, never into the working tree. With each build,
# tools/crossversion_data.py creates a full data folder (inventory, settings, UI state, smart groups, keybox,
# passwords, macros, exports); each folder is then read by both builds. All four reads must be equal.
# Exit status 0 = compatible.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="${1:-pre-refactor}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ophion-crossversion.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

mkdir -p "$WORK/old"
git -C "$ROOT" archive "$TAG" | tar -x -C "$WORK/old"
HELPER="$ROOT/tools/crossversion_data.py"

run() {  # run <tree> <command> <folder>
  PYTHONPATH="$1" python3 -P "$HELPER" "$2" "$3" 2>"$WORK/stderr" || { cat "$WORK/stderr" >&2; return 1; }
}

run "$WORK/old" create "$WORK/data-old"
run "$ROOT" create "$WORK/data-new"
run "$WORK/old" read "$WORK/data-old" > "$WORK/old-reads-old.json"
run "$ROOT" read "$WORK/data-old" > "$WORK/new-reads-old.json"
run "$ROOT" read "$WORK/data-new" > "$WORK/new-reads-new.json"
run "$WORK/old" read "$WORK/data-new" > "$WORK/old-reads-new.json"

status=0
for pair in "old-reads-old new-reads-old" "old-reads-old new-reads-new" "old-reads-old old-reads-new"; do
  set -- $pair
  if ! diff -u "$WORK/$1.json" "$WORK/$2.json" > "$WORK/diff"; then
    echo "crossversion: $2 differs from $1:" >&2
    head -40 "$WORK/diff" >&2
    status=1
  fi
done
[ "$status" -eq 0 ] && echo "crossversion: ok ($TAG <-> working tree, both directions)"
exit "$status"
