#!/usr/bin/env bash
# Clean-room smoke test of the AppImage in fresh containers with no Python and no GTK.
#
#   packaging/appimage/tests/smoke.sh [dist/Ophion-*.AppImage] [image ...]
#
# Default images: ubuntu:22.04 debian:12-slim fedora:40. Each container installs only test tools
# (Xvfb, xauth, D-Bus, a font, xwd, strace, and an OpenSSH *server* for the login test), removes any
# Python, and then checks (results and screenshots in dist/smoke/):
#   1. --appimage-extract-and-run --help exits 0 (no FUSE in containers)
#   2. selfcheck.py in the AppImage environment (imports, typelibs, bundled programs, certificates ...)
#   3. the main window opens under Xvfb with and without a D-Bus session, and Ctrl+Q quits it (exit 0)
#   4. nothing is written inside the AppImage directory (strace)
#   5. a real password login through the app's terminal path (VTE + bundled sshpass + ssh) to a local sshd
#   6. the transfer engine tests run on the bundled Python
#   7. without any display the app fails cleanly (an error, no crash)
#   ubuntu:22.04 only: 8. the whole unit test suite on the bundled Python
set -uo pipefail

TESTS="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REPO="$(cd "$TESTS/../../.." && pwd)"

# ----------------------------------------------------------------------------------------------- host side
if [ "${1:-}" != "--inside" ]; then
    APPIMAGE="${1:-$(ls -t "$REPO"/dist/Ophion-*-x86_64.AppImage 2>/dev/null | head -1)}"
    [ -f "$APPIMAGE" ] || { echo "usage: $0 [AppImage] [image ...]" >&2; exit 2; }
    shift || true
    IMAGES=("$@")
    [ "${#IMAGES[@]}" -gt 0 ] || IMAGES=(ubuntu:22.04 debian:12-slim fedora:40)
    ENGINE="${CONTAINER_ENGINE:-$(command -v docker || command -v podman)}"
    OUTDIR="$REPO/dist/smoke"
    mkdir -p "$OUTDIR"
    status=0
    for image in "${IMAGES[@]}"; do
        tag="${image//[:\/]/_}"
        echo "=== $image"
        "$ENGINE" run --rm -e SMOKE_IMAGE="$image" -e FULL_SUITE="$([ "$image" = ubuntu:22.04 ] && echo 1 || echo 0)" \
            -v "$(readlink -f "$APPIMAGE"):/smoke/Ophion.AppImage:ro" -v "$TESTS:/smoke/tests:ro" \
            -v "$REPO/tests:/smoke/repo-tests:ro" -v "$REPO/farid.py:/smoke/farid.py:ro" -v "$REPO/tools:/smoke/tools:ro" -v "$OUTDIR:/smoke/out" \
            "$image" bash /smoke/tests/smoke.sh --inside 2>&1 | tee "$OUTDIR/$tag.log"
        [ "${PIPESTATUS[0]}" = 0 ] || status=1
        grep -E '^(PASS|FAIL|SKIP) ' "$OUTDIR/$tag.log" > "$OUTDIR/$tag-results.txt" || true
    done
    exit "$status"
fi

# ------------------------------------------------------------------------------------------ container side
TAG="${SMOKE_IMAGE//[:\/]/_}"
OUT=/smoke/out
FAILURES=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; FAILURES=$((FAILURES + 1)); }
skip() { echo "SKIP $*"; }

# Test tools only (nothing the app needs). The distribution's own libsecret typelib is installed on purpose:
# the AppImage must never pick up a host typelib (Fedora 40's libsecret needs a newer GLib than the bundle's).
if command -v apt-get >/dev/null; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq --no-install-recommends xvfb xauth dbus fonts-dejavu-core \
        x11-apps openssh-server strace passwd procps gir1.2-secret-1 >/dev/null || { echo "FAIL install test tools"; exit 1; }
    for pkg in $(dpkg-query -W -f='${Package}\n' 'python3*' 'libpython3*' 2>/dev/null); do
        dpkg --purge --force-depends "$pkg" >/dev/null 2>&1
    done
elif command -v dnf >/dev/null; then
    dnf -y -q install --setopt=install_weak_deps=False xorg-x11-server-Xvfb xorg-x11-xauth dbus-daemon \
        dejavu-sans-fonts xwd openssh-server strace shadow-utils procps-ng util-linux libsecret >/dev/null \
        || { echo "FAIL install test tools"; exit 1; }
    rpm -qa 'python3*' | xargs -r rpm -e --nodeps >/dev/null 2>&1  # dnf itself needs it; not needed any more
fi
if command -v python3 >/dev/null || command -v python >/dev/null; then fail "host has no Python"; else pass "host has no Python"; fi
if ldconfig -p | grep -q 'libgtk-3.so.0'; then fail "host has no GTK 3"; else pass "host has no GTK 3"; fi

cp /smoke/Ophion.AppImage /tmp/Ophion.AppImage && chmod +x /tmp/Ophion.AppImage
export HOME=/tmp/home
mkdir -p "$HOME/.config/ophion"
echo '{"master_password_disabled": true}' > "$HOME/.config/ophion/settings.json"  # no first-run prompt

# 1. --help through the FUSE-less path
if /tmp/Ophion.AppImage --appimage-extract-and-run --help > /tmp/help.txt 2>&1 && grep -q -- "--rdp" /tmp/help.txt; then
    pass "--appimage-extract-and-run --help (exit 0)"
else
    fail "--appimage-extract-and-run --help: $(head -c 400 /tmp/help.txt)"
fi
(cd /tmp && ./Ophion.AppImage --appimage-extract >/dev/null 2>&1) || { fail "--appimage-extract"; exit 1; }
APP=/tmp/squashfs-root/AppRun
run() { OPHION_APPIMAGE_EXEC=1 "$APP" "$@"; }

Xvfb :99 -screen 0 1400x900x24 -nolisten tcp >/dev/null 2>&1 &
XVFB=$!
export DISPLAY=:99
for _ in $(seq 50); do [ -e /tmp/.X11-unix/X99 ] && break; sleep 0.1; done

# 2. self-check in the AppImage environment (with a user ssh config, which ssh must read)
ROOT_HOME="$(getent passwd "$(id -u)" | cut -d: -f6)"
mkdir -p "$ROOT_HOME/.ssh" && chmod 700 "$ROOT_HOME/.ssh"
printf 'Host smoke-config-alias\n    HostName 192.0.2.20\n' > "$ROOT_HOME/.ssh/config" && chmod 600 "$ROOT_HOME/.ssh/config"
if dbus-run-session -- env OPHION_APPIMAGE_EXEC=1 "$APP" python3 /smoke/tests/selfcheck.py > /tmp/selfcheck.txt 2>&1; then
    pass "selfcheck"
else
    fail "selfcheck"
fi
sed 's/^/    /' /tmp/selfcheck.txt

# 3. the main window, with and without D-Bus, then Ctrl+Q
gui_run() {  # label, command...
    local label="$1"; shift
    "$@" > "/tmp/gui-$label.log" 2>&1 &
    local pid=$! wid=""
    for _ in $(seq 120); do
        wid="$(run xdotool search --onlyvisible --name '^Ophion$' 2>/dev/null | head -1)"
        [ -n "$wid" ] && break
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.5
    done
    if [ -z "$wid" ]; then
        fail "main window ($label): not shown; $(tail -c 600 "/tmp/gui-$label.log")"
        kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; return
    fi
    sleep 2
    xwd -root -silent -out "$OUT/$TAG-$label.xwd" 2>/dev/null || true
    run xdotool windowfocus --sync "$wid" key --clearmodifiers ctrl+q >/dev/null 2>&1
    for _ in $(seq 40); do kill -0 "$pid" 2>/dev/null || break; sleep 0.25; done
    if kill -0 "$pid" 2>/dev/null; then
        fail "Ctrl+Q ($label): still running"; kill "$pid"; wait "$pid" 2>/dev/null; return
    fi
    wait "$pid"; local code=$?
    if [ "$code" = 0 ]; then pass "main window ($label): shown, screenshot, Ctrl+Q exit 0"
    else fail "main window ($label): exit $code; $(tail -c 600 "/tmp/gui-$label.log")"; fi
}
gui_run fuse-less-dbus dbus-run-session -- /tmp/Ophion.AppImage --appimage-extract-and-run
gui_run no-dbus env -u DBUS_SESSION_BUS_ADDRESS "$APP"

# 4. nothing written inside the AppImage directory
gui_run strace strace -f -qq -o /tmp/strace.log -e trace=open,openat,creat,mkdir,mkdirat,unlink,unlinkat,rename,renameat,renameat2,truncate "$APP"
writes="$(grep '/tmp/squashfs-root' /tmp/strace.log | grep -E 'O_WRONLY|O_RDWR|O_CREAT|creat\(|mkdir|unlink|rename|truncate' | grep -v ' = -1 ' | head -5)"
if [ -z "$writes" ]; then pass "no writes inside the AppImage directory"; else fail "writes inside the AppImage: $writes"; fi

# 5. real password login through the app's terminal path
useradd -m smoketest >/dev/null 2>&1; echo 'smoketest:Smoke-Test-Pw-1' | chpasswd
ssh-keygen -A >/dev/null 2>&1; mkdir -p /run/sshd /var/empty
/usr/sbin/sshd -p 2222 -o ListenAddress=127.0.0.1 -o PasswordAuthentication=yes -o KbdInteractiveAuthentication=no \
    -o PermitRootLogin=no -E /tmp/sshd.log
sleep 1
run ssh-keyscan -p 2222 127.0.0.1 > "$ROOT_HOME/.ssh/known_hosts" 2>/dev/null
if run python3 /smoke/tests/e2e_ssh.py 127.0.0.1 2222 smoketest 'Smoke-Test-Pw-1' > /tmp/e2e.txt 2>&1; then
    pass "end-to-end password login"
else
    fail "end-to-end password login"
fi
sed 's/^/    /' /tmp/e2e.txt | tail -6

# 6. transfer engine tests on the bundled Python
# The tests find repo files relative to their own folder: a real copy, the bundled app next to it.
mkdir -p /tmp/tt && cp -r /smoke/repo-tests /tmp/tt/tests && cp /smoke/farid.py /tmp/tt/ && cp -r /smoke/tools /tmp/tt/tools \
    && ln -sfn /tmp/squashfs-root/usr/share/ophion/ssh_manager /tmp/tt/ssh_manager
if (cd /tmp/tt && run python3 -m unittest tests.test_transfer_engine tests.test_transfer_backends > /tmp/transfer.txt 2>&1); then
    pass "transfer engine tests ($(grep -E '^Ran' /tmp/transfer.txt))"
else
    fail "transfer engine tests: $(tail -c 800 /tmp/transfer.txt)"
fi

# 7. no display at all: a clean error, not a crash
env -u DISPLAY -u WAYLAND_DISPLAY "$APP" > /tmp/nodisplay.txt 2>&1
code=$?
if [ "$code" -lt 128 ]; then pass "no display: exit $code, $(grep -m1 -iE 'display|cannot' /tmp/nodisplay.txt)"
else fail "no display: crashed with exit $code"; fi

# 8. the whole unit suite on the bundled Python (one image). Informational: some tests depend on the
#    machine (pixel colours with the container's only font, the legacy Textual UI's "rich", which is not
#    bundled, a VTE 0.76 API used by one test); every failing test is listed for review.
if [ "${FULL_SUITE:-0}" = 1 ]; then
    (cd /tmp/tt && run python3 -m unittest discover -s tests -t . > /tmp/suite.txt 2>&1)
    echo "INFO unit suite on the bundled Python: $(grep -E '^Ran' /tmp/suite.txt); $(tail -1 /tmp/suite.txt)"
    grep -E '^(FAIL|ERROR):' /tmp/suite.txt | sed -E 's/ \(icon=.*//' | sort | uniq -c | sed 's/^/    /'
    cp /tmp/suite.txt "$OUT/$TAG-suite.txt"
fi

kill "$XVFB" 2>/dev/null
echo "summary $SMOKE_IMAGE: $FAILURES failure(s)"
[ "$FAILURES" = 0 ]
