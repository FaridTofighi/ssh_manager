#!/usr/bin/env bash
# Build the Ophion AppImage: ./packaging/appimage/build.sh
#
# On the host (docker or podman present) it builds the pinned Ubuntu 22.04 build image, then runs itself
# inside it with the committed sources (git archive HEAD) and writes to dist/:
#   Ophion-<version>-x86_64.AppImage, SHA256SUMS, MANIFEST.txt (every bundled file and its SHA-256),
#   host-libraries-used.txt (what the AppImage takes from the host).
# Inside the build container it can also be run directly (SSHM_APPIMAGE_IN_CONTAINER=1, SRC_TAR, OUT).
#
# Environment: VERSION (default git<commits>.<hash>), OUT (default <repo>/dist), CONTAINER_ENGINE,
#   BUILD_PROXY (default http://127.0.0.1:10808; empty = no proxy): http_proxy / https_proxy of every download.
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
IMAGE="ophion-appimage-build:1"

log() { printf '\033[1m==> %s\033[0m\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# ----------------------------------------------------------------------------------------------- host side
if [ "${SSHM_APPIMAGE_IN_CONTAINER:-}" != "1" ] && [ ! -x /opt/appimage/appimagetool/AppRun ]; then
    REPO="$(cd "$HERE/../.." && pwd)"
    ENGINE="${CONTAINER_ENGINE:-$(command -v docker || command -v podman || true)}"
    [ -n "$ENGINE" ] || die "docker or podman is needed (or run this script inside the build container)"
    git -C "$REPO" rev-parse HEAD >/dev/null 2>&1 || die "not a git checkout: $REPO"
    if [ -n "$(git -C "$REPO" status --porcelain -- main.py ophion)" ]; then
        log "note: uncommitted changes in main.py / ophion are NOT packaged (the build uses HEAD)"
    fi
    VERSION="${VERSION:-git$(git -C "$REPO" rev-list --count HEAD).$(git -C "$REPO" rev-parse --short=7 HEAD)}"
    OUT="${OUT:-$REPO/dist}"
    CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/ophion-appimage-build"  # downloads (checked by SHA-256)
    mkdir -p "$OUT" "$CACHE"
    # Every download (apt, curl and pip in the image and the container; podman's image pull) goes through the
    # proxy. It listens on the host's 127.0.0.1, so the image build and the container use the host's network.
    PROXY="${BUILD_PROXY-http://127.0.0.1:10808}"
    NET=() PROXY_BUILD=() PROXY_RUN=()
    if [ -n "$PROXY" ]; then
        export http_proxy="$PROXY" https_proxy="$PROXY" HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY"
        NET=(--network host)
        for var in http_proxy https_proxy HTTP_PROXY HTTPS_PROXY; do
            PROXY_BUILD+=(--build-arg "$var=$PROXY")
            PROXY_RUN+=(-e "$var=$PROXY")
        done
        log "proxy $PROXY"
    fi
    STAGE="$(mktemp -d)"
    trap 'rm -rf "$STAGE"' EXIT
    git -C "$REPO" archive --format=tar HEAD -- main.py ophion > "$STAGE/src.tar"
    log "build image $IMAGE"
    "$ENGINE" build "${NET[@]}" "${PROXY_BUILD[@]}" -f "$HERE/Dockerfile.build" -t "$IMAGE" "$HERE"
    log "build the AppImage (version $VERSION)"
    "$ENGINE" run --rm "${NET[@]}" "${PROXY_RUN[@]}" \
        -e SSHM_APPIMAGE_IN_CONTAINER=1 -e VERSION="$VERSION" -e OUT=/out -e SRC_TAR=/stage/src.tar \
        -e SSHM_COMMIT="$(git -C "$REPO" rev-parse HEAD)" \
        -e SOURCE_DATE_EPOCH="$(git -C "$REPO" log -1 --format=%ct HEAD)" \
        -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" \
        -v "$HERE:/packaging:ro" -v "$STAGE:/stage:ro" -v "$OUT:/out" -v "$CACHE:/cache" \
        "$IMAGE" /packaging/build.sh
    log "done: $OUT"
    exit 0
fi

# ------------------------------------------------------------------------------------------ container side
PACKAGING="$HERE"
VERSION="${VERSION:-dev}"
OUT="${OUT:-/out}"
SRC_TAR="${SRC_TAR:?SRC_TAR: a tar of main.py and ophion/ (git archive HEAD -- main.py ophion)}"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-$(date +%s)}"
export SOURCE_DATE_EPOCH PIP_DEFAULT_TIMEOUT=180 PIP_RETRIES=10 PIP_DISABLE_PIP_VERSION_CHECK=1
CACHE="${CACHE:-/cache}"
if mkdir -p "$CACHE/pip" "$CACHE/sdist" 2>/dev/null && [ -w "$CACHE" ]; then export PIP_CACHE_DIR="$CACHE/pip"
else CACHE="$(mktemp -d)"; mkdir -p "$CACHE/sdist"; fi
retry() { local n; for n in 1 2 3; do "$@" && return 0; log "retry $n/3: $*"; sleep 5; done; return 1; }
ARCH_TRIPLET=x86_64-linux-gnu
PY=python3.10

WORK=/tmp/ophion-appimage.build  # a fixed path: it is recorded in .pyc files and wheel metadata
rm -rf "$WORK" && mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT
APPDIR="$WORK/AppDir"
LIBDIR="$APPDIR/usr/lib"
ARCHLIB="$APPDIR/usr/lib/$ARCH_TRIPLET"
SITE="$APPDIR/usr/lib/python3/dist-packages"
DATA="$APPDIR/usr/share/ophion"
mkdir -p "$APPDIR/usr/bin" "$ARCHLIB" "$SITE" "$DATA/caches" "$APPDIR/usr/share"

# ---- Python: the interpreter and its standard library (no tests, IDLE, Tk, ensurepip, static lib)
log "Python $($PY -c 'import platform; print(platform.python_version())')"
cp -a "/usr/bin/$PY" "$APPDIR/usr/bin/"
ln -s "$PY" "$APPDIR/usr/bin/python3"
tar -C /usr/lib -cf - --exclude="$PY/test" --exclude="$PY/idlelib" --exclude="$PY/tkinter" \
    --exclude="$PY/turtledemo" --exclude="$PY/ensurepip" --exclude="$PY/lib2to3" --exclude="$PY/config-*" \
    --exclude="$PY/sitecustomize.py" --exclude="$PY/site-packages" --exclude="$PY/dist-packages" \
    --exclude='__pycache__' "$PY" | tar -C "$APPDIR/usr/lib" -xf -

# ---- Python libraries: hashed wheels only
log "Python libraries (requirements.lock)"
retry $PY -m pip install --no-compile --no-deps --require-hashes --only-binary=:all: --target "$SITE" \
    -r "$PACKAGING/requirements.lock" >/dev/null

# ---- PyGObject + pycairo: from hashed sdists, against the bundled GObject stack
log "PyGObject / pycairo"
$PY -m venv "$WORK/bvenv"
retry "$WORK/bvenv/bin/pip" install -q --no-deps --require-hashes --only-binary=:all: -r "$PACKAGING/requirements-build.lock"
mkdir -p "$WORK/sdist" "$WORK/wheels"
fetch_sdist() {  # name version sha256
    local file="$CACHE/sdist/$1-$2.tar.gz"
    if ! echo "$3  $file" | sha256sum -c - >/dev/null 2>&1; then
        retry curl -fsSL --retry 10 --retry-delay 3 -o "$file.part" \
            "https://files.pythonhosted.org/packages/source/${1:0:1}/$1/$1-$2.tar.gz" && mv -f "$file.part" "$file"
    fi
    echo "$3  $file" | sha256sum -c - >/dev/null || die "checksum mismatch: $1-$2"
    echo "$file"
}
sdist_value() { awk -v n="$1" '$1 == "#" && $2 ~ "^"n"==" {print $'"$2"'}' "$PACKAGING/requirements-build.lock"; }
for pkg in pycairo PyGObject; do
    version="$(sdist_value "$pkg" 2 | cut -d= -f3)"
    sha="$(sdist_value "$pkg" 4)"
    tarball="$(fetch_sdist "$(echo "$pkg" | tr 'A-Z' 'a-z')" "$version" "$sha")"
    PATH="$WORK/bvenv/bin:$PATH" "$WORK/bvenv/bin/pip" wheel -q --no-deps --no-build-isolation -w "$WORK/wheels" "$tarball"
    "$WORK/bvenv/bin/pip" install -q --no-deps "$WORK/wheels/$(echo "$pkg" | tr 'A-Z' 'a-z')"-*.whl
done
$PY -m pip install --no-compile --no-deps --target "$SITE" "$WORK"/wheels/*.whl >/dev/null

# ---- the application (committed sources)
log "Ophion sources"
tar -C "$DATA" -xf "$SRC_TAR"
[ -f "$DATA/main.py" ] && [ -d "$DATA/ophion" ] || die "SRC_TAR does not contain main.py and ophion/"

# ---- GObject data: typelibs, GdkPixbuf loaders, GTK input methods, GIO modules, schemas, icons, MIME
log "GTK / VTE data"
cp -a "/usr/lib/$ARCH_TRIPLET/girepository-1.0" "$ARCHLIB/"
cp -a "/usr/lib/$ARCH_TRIPLET/gdk-pixbuf-2.0" "$ARCHLIB/"
rm -f "$ARCHLIB/gdk-pixbuf-2.0/2.10.0/loaders.cache"
"/usr/lib/$ARCH_TRIPLET/gdk-pixbuf-2.0/gdk-pixbuf-query-loaders" "$ARCHLIB"/gdk-pixbuf-2.0/2.10.0/loaders/*.so \
    | sed "s|$APPDIR|@APPDIR@|g" > "$DATA/caches/gdk-pixbuf-loaders.cache"
mkdir -p "$ARCHLIB/gtk-3.0/3.0.0"
cp -a "/usr/lib/$ARCH_TRIPLET/gtk-3.0/3.0.0/immodules" "$ARCHLIB/gtk-3.0/3.0.0/"
"/usr/lib/$ARCH_TRIPLET/libgtk-3-0/gtk-query-immodules-3.0" "$ARCHLIB"/gtk-3.0/3.0.0/immodules/*.so \
    | sed "s|$APPDIR|@APPDIR@|g" > "$DATA/caches/gtk-immodules.cache"
cp -a "/usr/lib/$ARCH_TRIPLET/gtk-3.0/modules" "$ARCHLIB/gtk-3.0/"  # canberra-gtk-module: GNOME requests it
cp -a "/usr/lib/$ARCH_TRIPLET/libcanberra-0.30" "$ARCHLIB/"            # its sound drivers (AppRun: LTDL_LIBRARY_PATH)
mkdir -p "$ARCHLIB/gio/modules"
cp -a "/usr/lib/$ARCH_TRIPLET/gio/modules/libdconfsettings.so" "$ARCHLIB/gio/modules/"
"/usr/lib/$ARCH_TRIPLET/glib-2.0/gio-querymodules" "$ARCHLIB/gio/modules"
mkdir -p "$APPDIR/usr/share/glib-2.0/schemas"
cp -a /usr/share/glib-2.0/schemas/*.xml "$APPDIR/usr/share/glib-2.0/schemas/"
glib-compile-schemas "$APPDIR/usr/share/glib-2.0/schemas"
mkdir -p "$APPDIR/usr/share/icons"
cp -a /usr/share/icons/Adwaita /usr/share/icons/hicolor "$APPDIR/usr/share/icons/"
cp -a /usr/share/mime "$APPDIR/usr/share/"

# ---- programs: OpenSSH client, sshpass, xdotool, FreeRDP 3 (X11 client)
log "programs"
for prog in ssh ssh-keygen ssh-keyscan ssh-add sshpass xdotool; do
    cp -a "/usr/bin/$prog" "$APPDIR/usr/bin/"
done
# at-spi2-core 2.56 (ATK, atk-bridge, libatspi), copied before the closure so it replaces Ubuntu 22.04's 2.38.
cp -a /opt/atspi/lib/libatk-1.0.so* /opt/atspi/lib/libatk-bridge-2.0.so* /opt/atspi/lib/libatspi.so* "$LIBDIR/"
# FreeRDP installs "xfreerdp"; it is shipped once, under the FreeRDP 3 name the app looks for first.
cp -a /opt/freerdp/bin/xfreerdp "$APPDIR/usr/bin/xfreerdp3"
cp -a /opt/freerdp/lib/lib*.so* "$LIBDIR/"

# ---- desktop integration
cp "$PACKAGING/AppRun" "$APPDIR/AppRun"
chmod 755 "$APPDIR/AppRun"
# The application icon "ophion": the bundled hicolor PNGs (raster only; see gtkui/assets/app-icon/README.md).
APP_ICONS="$DATA/ophion/gtkui/assets/app-icon/hicolor"
DESKTOP_ID="io.github.faridtofighi.Ophion"  # identity.APP_ID: the desktop file's and AppStream's name
cp "$PACKAGING/$DESKTOP_ID.desktop" "$APPDIR/"
cp "$APP_ICONS/256x256/apps/ophion.png" "$APPDIR/ophion.png"  # appimagetool: the Icon= file at the top
ln -s ophion.png "$APPDIR/.DirIcon"
install -Dm644 "$PACKAGING/$DESKTOP_ID.desktop" "$APPDIR/usr/share/applications/$DESKTOP_ID.desktop"
for size_dir in "$APP_ICONS"/*x*; do
    install -Dm644 "$size_dir/apps/ophion.png" "$APPDIR/usr/share/icons/hicolor/$(basename "$size_dir")/apps/ophion.png"
done
if command -v gtk-update-icon-cache >/dev/null 2>&1; then  # the copied hicolor cache would not list it
    gtk-update-icon-cache -f -t "$APPDIR/usr/share/icons/hicolor" || log "note: gtk-update-icon-cache failed"
fi
install -Dm644 "$PACKAGING/$DESKTOP_ID.metainfo.xml" "$APPDIR/usr/share/metainfo/$DESKTOP_ID.appdata.xml"

# ---- shared libraries: everything the ELF files need, except the host's (host-libraries.txt)
HOST_PATTERNS="$(grep -v '^\s*#' "$PACKAGING/host-libraries.txt" | grep -v '^\s*$')"
is_host_lib() {
    local name="$1" pattern
    while IFS= read -r pattern; do
        # shellcheck disable=SC2254
        case "$name" in $pattern) return 0 ;; esac
    done <<< "$HOST_PATTERNS"
    return 1
}
elf_files() { find "$APPDIR" -type f \( -name '*.so*' -o -perm -u+x \) -print0 | xargs -0 file -N | awk -F': ' '/ELF/ {print $1}'; }
export LD_LIBRARY_PATH="$LIBDIR:$ARCHLIB:/opt/freerdp/lib"
log "library closure"
typelib_libs() {  # every library a typelib names (they are comma-separated: "libharfbuzz-gobject.so.0,libharfbuzz.so.0")
    grep -aohE 'lib[A-Za-z0-9_+-]+(-[0-9.]+)?\.so\.[0-9]+' "$ARCHLIB"/girepository-1.0/*.typelib | sort -u
}
seeds="$(typelib_libs)"
for soname in $seeds; do  # libraries GObject-introspection opens by name (libgtk-3.so.0, libvte-2.91.so.0 ...)
    is_host_lib "$soname" && continue
    path="$(ldconfig -p | awk -v n="$soname" '$1 == n && /x86-64/ && !found {print $NF; found = 1}')"
    [ -n "$path" ] && [ ! -e "$LIBDIR/$soname" ] && cp -L "$path" "$LIBDIR/$soname"
done
while :; do
    added=0
    while IFS= read -r elf; do
        while read -r soname _arrow path _rest; do
            [ "$_arrow" = "=>" ] || continue
            is_host_lib "$soname" && continue
            [ "$path" = "not" ] && continue  # reported by the audit
            case "$path" in "$APPDIR"/*) continue ;; esac
            if [ ! -e "$LIBDIR/$soname" ]; then cp -L "$path" "$LIBDIR/$soname"; added=1; fi
        done < <(ldd "$elf" 2>/dev/null)
    done < <(elf_files)
    [ "$added" = 0 ] && break
done

# ---- GObject-introspection sees only the AppImage's typelibs: libgirepository has the host's typelib
#      folders compiled in, and a host typelib would load a host library built against a newer GLib
#      (e.g. keyring's libsecret: "undefined symbol: g_task_set_static_name"). The "/usr/lib/" prefix of the
#      two defaults becomes "/no-host/" (same length; the rest is left alone because the compiler shares
#      "girepository-1.0" with the tail of one of them); GI_TYPELIB_PATH (set by AppRun) still works.
log "GObject-introspection: no host typelibs"
#      libcanberra opens its drivers by a compiled-in full path ("/usr/lib/<triplet>/libcanberra-0.30/
#      libcanberra-%s"): that string is shortened in place to "libcanberra-%s", so libltdl looks the driver
#      up by name, in LTDL_LIBRARY_PATH (the bundled drivers, set by AppRun) and never in the host's folder.
no_host() {  # file folder...: "/usr/lib/" -> "/no-host/" at the start of each NUL-terminated folder string
    $PY - "$@" <<'PYEOF'
import sys
path, folders = sys.argv[1], [f.encode() for f in sys.argv[2:]]
data = open(path, "rb").read()
for folder in folders:
    whole = b"\0" + folder + b"\0"
    assert data.count(whole) == 1, f"{whole!r} found {data.count(whole)} times in {path}"
    start = data.index(whole) + 1
    data = data[:start] + b"/no-host/" + data[start + len(b"/usr/lib/"):]
with open(path, "wb") as handle:
    handle.write(data)
PYEOF
}
no_host "$LIBDIR/libgirepository-1.0.so.1" "/usr/lib/$ARCH_TRIPLET" /usr/lib/girepository-1.0
$PY - "$LIBDIR/libcanberra.so.0" "/usr/lib/$ARCH_TRIPLET/libcanberra-0.30/libcanberra-%s" <<'PYEOF'
import sys
path, full = sys.argv[1], sys.argv[2].encode()
data = open(path, "rb").read()
whole = b"\0" + full + b"\0"
assert data.count(whole) == 1, f"{whole!r} found {data.count(whole)} times in {path}"
start = data.index(whole) + 1
short = b"libcanberra-%s"
data = data[:start] + short + b"\0" * (len(full) - len(short)) + data[start + len(full):]
with open(path, "wb") as handle:
    handle.write(data)
PYEOF

# ---- hygiene: strip, drop docs, precompile Python (reproducible: unchecked-hash .pyc, no timestamps)
log "strip and clean"
elf_files | xargs -r strip --strip-unneeded 2>/dev/null || true
find "$APPDIR" -name '__pycache__' -type d -prune -exec rm -rf {} +
rm -rf "$APPDIR/usr/share/doc" "$APPDIR/usr/share/man" "$APPDIR/usr/share/icons/Adwaita/cursors"
find "$SITE" -maxdepth 2 -type d \( -name tests -o -name testing \) -prune -exec rm -rf {} +
$PY -m compileall -q -j0 --invalidation-mode unchecked-hash "$APPDIR/usr/lib/$PY" "$SITE" "$DATA" >/dev/null || true

# ---- library audit: every ELF file resolves inside the AppDir or to an allowed host library
log "library audit"
: > "$WORK/host-libs"
failed=0
while IFS= read -r elf; do
    while read -r soname _arrow path _rest; do
        [ "$_arrow" = "=>" ] || continue
        if [ "$path" = "not" ]; then
            if is_host_lib "$soname"; then echo "$soname" >> "$WORK/host-libs"; continue; fi
            echo "MISSING ${elf#"$APPDIR"/}: $soname" >&2; failed=1; continue
        fi
        case "$path" in "$APPDIR"/*) continue ;; esac
        if is_host_lib "$soname"; then echo "$soname" >> "$WORK/host-libs"; else
            echo "OUTSIDE ${elf#"$APPDIR"/}: $soname => $path" >&2; failed=1; fi
    done < <(LD_LIBRARY_PATH="$LIBDIR:$ARCHLIB" ldd "$elf" 2>/dev/null)
done < <(elf_files)
for soname in $(typelib_libs); do  # opened by name at runtime: bundled, or explicitly from the host
    if [ ! -e "$LIBDIR/$soname" ] && ! is_host_lib "$soname"; then
        echo "TYPELIB library not bundled: $soname" >&2; failed=1
    fi
done
[ "$failed" = 0 ] || die "library audit failed (see above)"
sort -u "$WORK/host-libs" > "$WORK/host-libraries-used.txt"
log "host libraries used: $(tr '\n' ' ' < "$WORK/host-libraries-used.txt")"

# ---- versions of everything bundled
{
    echo "Ophion $VERSION (commit ${SSHM_COMMIT:-unknown})"
    echo "Built $(date -u -d "@$SOURCE_DATE_EPOCH" +%Y-%m-%dT%H:%M:%SZ) on Ubuntu 22.04 (glibc $(ldd --version | awk 'NR == 1 {print $NF}'))"
    echo "FreeRDP: $(/opt/freerdp/bin/xfreerdp --version 2>/dev/null | awk 'NR == 1')"
    echo "at-spi2-core (ATK, atk-bridge, libatspi): $(PKG_CONFIG_PATH=/opt/atspi/lib/pkgconfig pkg-config --modversion atspi-2), from source"
    echo
    echo "Debian packages (Ubuntu 22.04 snapshot):"
    dpkg-query -W -f='  ${Package} ${Version}\n' python3.10 libgtk-3-0 libvte-2.91-0 librsvg2-2 libglib2.0-0 \
        libgirepository-1.0-1 libpango-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 openssh-client sshpass xdotool libxdo3 \
        libssl3 libsqlite3-0 adwaita-icon-theme hicolor-icon-theme dconf-gsettings-backend libsecret-1-0 \
        libcanberra0 libcanberra-gtk3-module
    echo
    echo "Python packages:"
    $PY -m pip list --path "$SITE" --format=freeze 2>/dev/null | sed 's/^/  /'
} > "$DATA/BUNDLE-VERSIONS.txt"

# ---- reproducible file list, fixed timestamps, the AppImage
find "$APPDIR" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +
NAME="Ophion-$VERSION-x86_64.AppImage"
mkdir -p "$OUT"
(cd "$APPDIR" && find . -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum) > "$WORK/MANIFEST.txt"
log "appimagetool -> $NAME"
ARCH=x86_64 VERSION="$VERSION" appimagetool --no-appstream --runtime-file /opt/appimage/runtime-x86_64 \
    "$APPDIR" "$WORK/$NAME" >"$WORK/appimagetool.log" 2>&1 || { cat "$WORK/appimagetool.log" >&2; die "appimagetool failed"; }
cp "$WORK/$NAME" "$WORK/MANIFEST.txt" "$WORK/host-libraries-used.txt" "$OUT/"
cp "$DATA/BUNDLE-VERSIONS.txt" "$OUT/BUNDLE-VERSIONS.txt"
(cd "$OUT" && sha256sum "$NAME" > SHA256SUMS)
if [ -n "${HOST_UID:-}" ]; then
    chown -R "$HOST_UID:${HOST_GID:-$HOST_UID}" "$CACHE" 2>/dev/null || true
    chown "$HOST_UID:${HOST_GID:-$HOST_UID}" "$OUT/$NAME" "$OUT/SHA256SUMS" "$OUT/MANIFEST.txt" \
        "$OUT/host-libraries-used.txt" "$OUT/BUNDLE-VERSIONS.txt"
fi
log "$(du -h "$OUT/$NAME" | cut -f1)  $OUT/$NAME"
