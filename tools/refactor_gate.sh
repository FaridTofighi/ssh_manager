#!/usr/bin/env bash
# The refactor gate (§2.7): run after every commit of the refactor. Exit status 0 = the commit may stay.
#
#   tools/refactor_gate.sh [--snapshots] [--phase-end] [--quick]
#
#   1 compileall                       6 security invariants (R8)
#   2 string patch targets resolve      7 cross-version harness (R9)
#   3 API surface ⊇ baseline (R5)       8 smoke (app start, entry points, installer dry run)
#   4 architecture ratchets             9 UI snapshots vs baseline (R11): with --snapshots, or when the
#   5 full suite on the private X         last commit touches GTK code (ophion/gtkui, gtk_app, gtk_terminal)
#     server: passed ≥ baseline        10 benchmarks vs baseline (R10): with --phase-end
#
# --quick skips step 5 (for trying a step locally; a commit needs the full gate).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REF="$ROOT/tests/refactor"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ophion-gate.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
cd "$ROOT"

SNAPSHOTS=0; PHASE_END=0; QUICK=0
for arg in "$@"; do
  case "$arg" in
    --snapshots) SNAPSHOTS=1 ;;
    --phase-end) PHASE_END=1; SNAPSHOTS=1 ;;
    --quick) QUICK=1 ;;
    *) echo "unknown option $arg" >&2; exit 2 ;;
  esac
done
if [ "$SNAPSHOTS" -eq 0 ] && git diff --name-only HEAD~1 HEAD 2>/dev/null \
    | grep -qE '^ophion/(gtkui/|gtk_app\.py|gtk_terminal\.py)'; then
  SNAPSHOTS=1
fi

declare -a RESULTS=()
FAILED=0
START=$(date +%s)
gate() {  # gate <number> <name> <command…>
  local number="$1" name="$2"; shift 2
  local began=$(date +%s)
  if "$@" >"$WORK/step$number.log" 2>&1; then
    RESULTS+=("  ok      $number  $name ($(( $(date +%s) - began ))s)")
  else
    RESULTS+=("  FAILED  $number  $name ($(( $(date +%s) - began ))s): see below")
    FAILED=1
    { echo "----- step $number: $name"; tail -40 "$WORK/step$number.log"; } >>"$WORK/failures"
  fi
}
skip() { RESULTS+=("  skipped $1  $2"); }

suite() {  # the full suite; passed must reach the baseline (minus legacy tests removed under R4 (a))
  python3 tools/run_gtk_tests.py -- python3 -m pytest -q -p no:cacheprovider tests >"$WORK/suite.txt" 2>&1
  local status=$?
  local line; line="$(grep -E '^[0-9]+ (passed|failed)|[0-9]+ passed' "$WORK/suite.txt" | tail -1)"
  echo "suite: $line"
  python3 - "$line" "$REF/baseline_counts.json" <<'EOF'
import json, re, sys
line, baseline = sys.argv[1], json.load(open(sys.argv[2]))
count = lambda word: int((re.search(rf"(\d+) {word}", line) or [0, 0])[1])
passed, failed, errors = count("passed"), count("failed"), count("error")
floor = baseline["passed"] - baseline.get("removed_legacy_tests", 0)
print(f"passed {passed} (floor {floor}), failed {failed}, errors {errors}")
sys.exit(0 if passed >= floor and not failed and not errors else 1)
EOF
  local counted=$?
  [ "$status" -eq 0 ] && [ "$counted" -eq 0 ]
}

security() {
  python3 tools/run_gtk_tests.py -- python3 -m pytest -q -p no:cacheprovider \
    tests/refactor/test_security_invariants.py tests/refactor/test_spawn_golden.py \
    tests/refactor/test_persistence_golden.py tests/test_secret_mask.py tests/test_auth_flow.py \
    tests/test_keybox.py tests/test_secure_store.py tests/test_password_store.py tests/test_secrets_store.py \
    tests/test_persistence.py tests/test_credentials_export.py tests/test_native_export.py
}

snapshots() { python3 tools/refactor_snapshots.py check; }
benchmarks() { python3 tools/refactor_bench.py check; }

gate 1 "compileall" python3 -m compileall -q ophion ssh_manager tools tests
gate 2 "string patch targets resolve" python3 tools/run_gtk_tests.py -- python3 -m pytest -q -p no:cacheprovider \
  tests/refactor/test_patch_targets.py
gate 3 "API surface ⊇ baseline" python3 tools/api_surface.py --check "$REF/api_surface.json"
gate 4 "architecture ratchets" python3 tools/arch_metrics.py --check "$REF/metrics_baseline.json"
if [ "$QUICK" -eq 1 ]; then skip 5 "full suite (--quick)"; else gate 5 "full suite (private X server)" suite; fi
gate 6 "security invariants (R8)" security
gate 7 "cross-version harness (R9)" tools/refactor_crossversion.sh
gate 8 "smoke" tools/refactor_smoke.sh
if [ "$SNAPSHOTS" -eq 1 ]; then gate 9 "UI snapshots (R11)" snapshots; else skip 9 "UI snapshots (no GTK change)"; fi
if [ "$PHASE_END" -eq 1 ]; then gate 10 "benchmarks (R10)" benchmarks; else skip 10 "benchmarks (not a phase end)"; fi

echo "refactor gate — $(git rev-parse --short HEAD) $(git log -1 --format=%s | cut -c1-60)"
printf '%s\n' "${RESULTS[@]}"
[ -f "$WORK/suite.txt" ] && grep -E '[0-9]+ passed' "$WORK/suite.txt" | tail -1 | sed 's/^/  suite: /'
echo "  time: $(( $(date +%s) - START ))s"
if [ "$FAILED" -ne 0 ]; then
  cat "$WORK/failures"
  echo "refactor gate: FAILED"
  exit 1
fi
echo "refactor gate: ok"
