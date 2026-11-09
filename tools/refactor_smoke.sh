#!/usr/bin/env bash
# Smoke checks of the refactor gate (§2.6). Exit status 0 = all passed.
#   1. compileall of ophion/ and ssh_manager/
#   2. python3 -m ophion --help / --version and python3 -m ssh_manager --help
#   3. the main window with a sample inventory on the private X server for ~5 s: mapped, no traceback
#   4. ./install.sh --dry-run (with a temporary HOME)
#   5. the AppImage smoke test (packaging/appimage/tests/smoke.sh) only when asked for: it pulls container images
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/ophion-smoke.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
cd "$ROOT"
failed=0
step() {  # step <name> <command…>: runs it, prints ok / FAILED with its output
  local name="$1"; shift
  if "$@" >"$WORK/out" 2>&1; then
    echo "smoke: ok      $name"
  else
    echo "smoke: FAILED  $name"; sed 's/^/    /' "$WORK/out" | tail -20; failed=1
  fi
}

step "compileall" python3 -m compileall -q ophion ssh_manager
step "ophion --help" python3 -m ophion --help
step "ophion --version" python3 -m ophion --version
step "ssh_manager --help" python3 -m ssh_manager --help
app_start() {
  python3 tools/run_gtk_tests.py -- python3 tools/refactor_smoke_app.py >"$WORK/app" 2>&1 || { cat "$WORK/app"; return 1; }
  if grep -q "Traceback" "$WORK/app"; then cat "$WORK/app"; return 1; fi
  grep -q "mapped=True" "$WORK/app" || { cat "$WORK/app"; return 1; }
}
step "app start (5 s, sample inventory)" app_start
mkdir -p "$WORK/home"
step "install.sh --dry-run" env HOME="$WORK/home" XDG_DATA_HOME="$WORK/home/.local/share" ./install.sh --dry-run
# The AppImage smoke runs in fresh containers (it pulls images) on an AppImage built from this tree: only
# when asked for, with a container engine present.
if [ "${OPHION_SMOKE_APPIMAGE:-}" = "1" ] && { command -v docker || command -v podman; } >/dev/null 2>&1; then
  step "AppImage smoke" packaging/appimage/tests/smoke.sh
else
  echo "smoke: skipped AppImage smoke (tooling: set OPHION_SMOKE_APPIMAGE=1 with docker/podman and a current dist/ build)"
fi
exit "$failed"
