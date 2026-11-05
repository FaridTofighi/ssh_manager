#!/usr/bin/env bash
# Install Ophion for the current user (no root): ./install.sh [options]
#
# Two modes, chosen automatically:
#   AppImage  an Ophion*.AppImage next to this script (or --appimage PATH) is copied to
#             <prefix>/opt/ophion/Ophion.AppImage, and <bin>/ophion links to it.
#   source    (default) this checkout is copied to <prefix>/opt/ophion/releases/<id>/app, with a virtualenv
#             that sees the system's GTK / VTE / PyGObject (--system-site-packages). Python libraries
#             missing from the system (paramiko, cryptography, pexpect) are installed into it with pip.
# Both add the application-menu entry (<data>/applications/<APP_ID>.desktop) and the icon
# (<data>/icons/hicolor). Every file written is listed in <prefix>/opt/ophion/manifest.txt, which
# uninstall.sh reads; your hosts, passwords and settings (~/.config/ophion, ~/.local/share/ophion) are
# never touched. Running it again updates in place: the new version is switched in only when complete.
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]

  --prefix DIR      Install under DIR (default ~/.local; $XDG_BIN_HOME / $XDG_DATA_HOME are used when set)
  --appimage PATH   Install this AppImage (default: an Ophion*.AppImage next to install.sh)
  --source          Install from this source tree even if an AppImage is present
  --dry-run         Print every action; change nothing
  --yes             Do not ask before replacing an 'ophion' command or menu entry you made yourself
  --uninstall       Run uninstall.sh (with the remaining options)
  -h, --help        Show this help
EOF
}

say() { printf '%s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------------------------------------ options
PREFIX="" APPIMAGE="" FORCE_SOURCE=0 DRY=0 YES=0
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) [ $# -ge 2 ] || die "--prefix needs a directory"; PREFIX="$2"; shift 2 ;;
        --prefix=*) PREFIX="${1#*=}"; shift ;;
        --appimage) [ $# -ge 2 ] || die "--appimage needs a file"; APPIMAGE="$2"; shift 2 ;;
        --appimage=*) APPIMAGE="${1#*=}"; shift ;;
        --source) FORCE_SOURCE=1; shift ;;
        --dry-run) DRY=1; shift ;;
        --yes|-y) YES=1; shift ;;
        --uninstall) shift; exec "$HERE/uninstall.sh" "$@" ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done

if [ "$(id -u)" -eq 0 ]; then
    die "do not run install.sh as root: it installs for one user, under that user's home (no sudo needed)"
fi
[ -n "${HOME:-}" ] && [ -d "$HOME" ] || die "HOME is not set to a directory"

# The names come from the identity module (ophion/identity.py), never typed here.
identity() { sed -n "s/^$1 = \"\\([^\"]*\\)\".*/\\1/p" "$HERE/ophion/identity.py"; }
APP_SLUG="$(identity APP_SLUG)"
APP_ID="$(identity APP_ID)"
APP_NAME="$(identity APP_NAME)"
LEGACY_SLUG="$(identity LEGACY_SLUG)"
LEGACY_APP_ID="$(identity LEGACY_APP_ID)"
[ -n "$APP_SLUG" ] && [ -n "$APP_ID" ] || die "cannot read the app's names from $HERE/ophion/identity.py"

if [ -n "$PREFIX" ]; then
    PREFIX="$(realpath -m "$PREFIX")"
    BIN_DIR="$PREFIX/bin"
    DATA_DIR="$PREFIX/share"
else
    PREFIX="$HOME/.local"
    BIN_DIR="${XDG_BIN_HOME:-$PREFIX/bin}"
    DATA_DIR="${XDG_DATA_HOME:-$PREFIX/share}"
fi
OPT_DIR="$PREFIX/opt/$APP_SLUG"
MANIFEST="$OPT_DIR/manifest.txt"
COMMAND="$BIN_DIR/$APP_SLUG"
DESKTOP="$DATA_DIR/applications/$APP_ID.desktop"
ICONS_SRC="$HERE/ophion/gtkui/assets/app-icon/hicolor"
TEMPLATE="$HERE/packaging/appimage/$APP_ID.desktop"
INSTALLED_MARKER="X-Ophion-Installed=true"  # this launcher belongs to install.sh: the app leaves it alone
GENERATED_MARKER="X-Ophion-Generated=true"  # written by the app itself (desktop_integration.py)
MIN_PYTHON="3.10"

# --------------------------------------------------------------------------------------------- mode
MODE=source
if [ -n "$APPIMAGE" ]; then
    [ -f "$APPIMAGE" ] || die "no such AppImage: $APPIMAGE"
    APPIMAGE="$(realpath "$APPIMAGE")"
    MODE=appimage
elif [ "$FORCE_SOURCE" -eq 0 ]; then
    found="$(find "$HERE" -maxdepth 1 -type f -name 'Ophion*.AppImage' -printf '%T@ %p\n' 2>/dev/null \
             | sort -rn | head -n 1 | cut -d' ' -f2-)"
    if [ -n "$found" ]; then
        APPIMAGE="$found"
        MODE=appimage
    fi
fi

# ----------------------------------------------------------------------------------------- helpers
STEP=0 STEPS=6
step() {
    STEP=$((STEP + 1))
    printf '[%d/%d] %s… ' "$STEP" "$STEPS" "$1"
    if [ "$DRY" -eq 1 ]; then printf '\n'; fi  # the actions it would take follow, one per line
}
ok() { if [ "$DRY" -eq 1 ]; then say "  -> ${1:-ok}"; else say "${1:-ok}"; fi; }

declare -a NEW_ENTRIES=()  # the manifest being built: "f|l|t|d <path>"
record() { NEW_ENTRIES+=("$1 $2"); }

OLD_ENTRIES=""
if [ -f "$MANIFEST" ]; then OLD_ENTRIES="$(cat "$MANIFEST")"; fi
in_old_manifest() { printf '%s\n' "$OLD_ENTRIES" | grep -qxF -- "$1 $2"; }

declare -a CREATED_DIRS=()
make_dir() {  # mkdir -p that records each folder it creates (uninstall removes them when empty)
    local dir="$1" missing=()
    while [ ! -d "$dir" ] && ! printf '%s\n' "${CREATED_DIRS[@]}" | grep -qxF -- "$dir"; do
        missing=("$dir" "${missing[@]}")
        dir="$(dirname "$dir")"
    done
    for dir in "${missing[@]}"; do
        if [ "$DRY" -eq 1 ]; then say "  would create $dir"; else mkdir -p "$dir"; fi
        CREATED_DIRS+=("$dir")
        record d "$dir"
    done
}

FAILED_RELEASE="" SWITCHED=0 ENTRY=""
cleanup_on_error() {
    local status=$?
    [ -n "$ENTRY" ] && rm -f -- "$ENTRY"
    if [ "$status" -ne 0 ] && [ "$DRY" -eq 0 ]; then
        [ -n "$FAILED_RELEASE" ] && rm -rf -- "$FAILED_RELEASE"
        if [ "$SWITCHED" -eq 0 ]; then
            local i
            for ((i = ${#CREATED_DIRS[@]} - 1; i >= 0; i--)); do rmdir -- "${CREATED_DIRS[$i]}" 2>/dev/null || true; done
            printf 'Nothing was changed: the installation stopped before the new version was switched in.\n' >&2
        else
            printf 'The installation stopped part-way; run ./install.sh again to finish it.\n' >&2
        fi
    fi
}
trap cleanup_on_error EXIT

put_file() {  # put_file SOURCE TARGET MODE: atomic replace (a temporary copy, then mv)
    if [ "$DRY" -eq 1 ]; then say "  would install $2"; return; fi
    local tmp
    tmp="$(dirname "$2")/.$(basename "$2").tmp.$$"
    cp -- "$1" "$tmp"
    chmod "$3" "$tmp"
    mv -f -- "$tmp" "$2"
}

put_link() {  # put_link TARGET LINK: atomic symlink replace
    if [ "$DRY" -eq 1 ]; then say "  would link $2 -> $1"; return; fi
    local tmp
    tmp="$(dirname "$2")/.$(basename "$2").tmp.$$"
    ln -sfn -- "$1" "$tmp"
    mv -fT -- "$tmp" "$2"
}

ours_or_ask() {  # an existing PATH that install.sh did not write (and the app did not generate): ask first
    local path="$1" what="$2"
    if [ ! -e "$path" ] && [ ! -L "$path" ]; then return 0; fi
    if in_old_manifest f "$path" || in_old_manifest l "$path"; then return 0; fi
    if [ -f "$path" ] && grep -qxF -e "$INSTALLED_MARKER" -e "$GENERATED_MARKER" "$path" 2>/dev/null; then return 0; fi
    if [ "$YES" -eq 1 ]; then return 0; fi
    if [ "$DRY" -eq 1 ]; then say "  would ask before replacing $path ($what)"; return 0; fi
    if [ -t 0 ]; then
        printf '\n%s already exists and was not made by this installer (%s). Replace it? [y/N] ' "$path" "$what"
        local answer=""
        read -r answer || true
        case "$answer" in y|Y|yes|YES) return 0 ;; esac
    fi
    die "$path already exists ($what); rerun with --yes to replace it"
}

desktop_exec_arg() {  # one Exec argument, quoted as the Desktop Entry spec says
    local value="${1//%/%%}"
    case "$value" in
        *[!A-Za-z0-9/._+-]*)
            value="${value//\\/\\\\}"; value="${value//\"/\\\"}"; value="${value//\`/\\\`}"; value="${value//\$/\\\$}"
            printf '"%s"' "$value" ;;
        *) printf '%s' "$value" ;;
    esac
}

sh_quote() { printf "'%s'" "${1//\'/\'\\\'\'}"; }

distro_hint() {
    local id="" like=""
    if [ -r /etc/os-release ]; then
        id="$(. /etc/os-release && printf '%s' "${ID:-}")"
        like="$(. /etc/os-release && printf '%s' "${ID_LIKE:-}")"
    fi
    case " $id $like " in
        *" debian "*|*" ubuntu "*)
            say "  sudo apt install python3 python3-venv python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-vte-2.91 gir1.2-rsvg-2.0" ;;
        *" fedora "*|*" rhel "*|*" centos "*)
            say "  sudo dnf install python3 python3-gobject python3-cairo gtk3 vte291 librsvg2" ;;
        *" arch "*|*" manjaro "*)
            say "  sudo pacman -S python python-gobject python-cairo gtk3 vte3 librsvg" ;;
        *" suse "*|*" opensuse "*|*" opensuse-tumbleweed "*|*" opensuse-leap "*)
            say "  sudo zypper install python3 python3-gobject python3-gobject-cairo typelib-1_0-Gtk-3_0 typelib-1_0-Vte-2_91 typelib-1_0-Rsvg-2_0" ;;
        *)
            say "  Install Python 3 with venv, PyGObject (with cairo), GTK 3, VTE 2.91 and librsvg (GObject introspection)" ;;
    esac
}

# --------------------------------------------------------------------------------------- 1. checks
say "Installing $APP_NAME for $(id -un) ($MODE mode)$([ "$DRY" -eq 1 ] && printf ', dry run: nothing is changed')"
PYTHON="${PYTHON:-python3}"
if [ "$MODE" = source ]; then
    step "Checking system libraries"
    command -v "$PYTHON" >/dev/null 2>&1 || { say "missing"; die "Python 3 ($PYTHON) is not installed"; }
    missing="$("$PYTHON" - "$MIN_PYTHON" <<'EOF' 2>/dev/null || printf 'python (cannot run)\n'
import sys
need = tuple(int(part) for part in sys.argv[1].split("."))
if sys.version_info[:2] < need:
    print(f"Python >= {sys.argv[1]} (this is {sys.version.split()[0]})")
for module in ("venv", "ensurepip"):
    try:
        __import__(module)
    except ImportError:
        print(f"{module} (python3-venv)")
try:
    import gi
except ImportError:
    print("gi (PyGObject)")
else:
    for name, version in (("Gtk", "3.0"), ("Gdk", "3.0"), ("GdkPixbuf", "2.0"), ("Pango", "1.0"),
                          ("PangoCairo", "1.0"), ("Vte", "2.91"), ("Rsvg", "2.0")):
        try:
            gi.require_version(name, version)
            __import__("gi.repository." + name)
        except (ImportError, ValueError):
            print(f"{name} {version} (GObject introspection)")
    try:
        import cairo  # noqa: F401
        gi.require_foreign("cairo")
    except ImportError:
        print("cairo for PyGObject (python3-gi-cairo)")
EOF
)"
    if [ -n "$missing" ]; then
        say "missing"
        say "These system libraries are missing:"
        printf '%s\n' "$missing" | sed 's/^/  - /'
        say "Install them with your package manager, then run ./install.sh again:"
        distro_hint
        exit 1
    fi
    ok
else
    step "Checking the AppImage"
    head -c 4 "$APPIMAGE" | grep -q 'ELF' || warn "$APPIMAGE does not look like an AppImage (no ELF header)"
    ok "$(basename "$APPIMAGE")"
fi
for tool in ssh sshpass; do
    command -v "$tool" >/dev/null 2>&1 || warn "'$tool' is not installed: $([ "$tool" = ssh ] \
        && printf 'SSH sessions need it (openssh-client)' || printf 'saved passwords cannot be typed for you')"
done

# The menu entry is made and checked before anything changes.
[ -f "$TEMPLATE" ] || die "missing template $TEMPLATE"
if [ "$MODE" = source ]; then EXEC_PATH="$COMMAND"; else EXEC_PATH="$OPT_DIR/Ophion.AppImage"; fi
ENTRY="$(mktemp --suffix=.desktop)"
exec_line="Exec=$(desktop_exec_arg "$EXEC_PATH") %U"
awk -v exec_line="$exec_line" -v marker="$INSTALLED_MARKER" '
    /^Exec=/ { print exec_line; next }
    { print }
    END { print marker }' "$TEMPLATE" > "$ENTRY"
if command -v desktop-file-validate >/dev/null 2>&1; then
    if ! report="$(desktop-file-validate "$ENTRY" 2>&1)"; then
        rm -f -- "$ENTRY"
        die "the generated menu entry is not valid: $report"
    fi
fi

# --------------------------------------------------------------------------------------- 2. the app
ours_or_ask "$COMMAND" "the '$APP_SLUG' command"
ours_or_ask "$DESKTOP" "the menu entry"
if [ "$MODE" = source ]; then
    step "Installing the app files"
    make_dir "$OPT_DIR/releases"
    RELEASE_ID="$(date +%Y%m%d-%H%M%S)-$$"
    RELEASE="$OPT_DIR/releases/$RELEASE_ID"
    VERSION="$(git -C "$HERE" describe --always --dirty 2>/dev/null || true)"
    [ -n "$VERSION" ] || VERSION="$(cat "$HERE/VERSION" 2>/dev/null || printf 'unknown')"
    if [ "$DRY" -eq 1 ]; then
        say "  would copy $HERE (main.py, ophion, ssh_manager) to $RELEASE/app (version $VERSION)"
        say "  would create the virtualenv $RELEASE/venv (--system-site-packages) and pip-install missing libraries"
    else
        FAILED_RELEASE="$RELEASE"
        mkdir -p "$RELEASE/app"
        tar -C "$HERE" --exclude=__pycache__ --exclude='*.pyc' -cf - main.py ophion ssh_manager | tar -C "$RELEASE/app" -xf -
        printf '%s\n' "$VERSION" > "$RELEASE/app/VERSION"
        "$PYTHON" -m venv --system-site-packages --without-pip "$RELEASE/venv"
        VENV_PY="$RELEASE/venv/bin/python"
        site="$("$VENV_PY" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
        printf '%s\n' "$RELEASE/app" > "$site/$APP_SLUG-app.pth"  # "python -m ophion" finds the copied sources
        needed=()
        for module in paramiko cryptography pexpect; do
            "$VENV_PY" -c "import $module" 2>/dev/null || needed+=("$module")
        done
        if [ ${#needed[@]} -gt 0 ]; then
            pins=()
            for module in "${needed[@]}"; do
                pin="$(grep -ioE "^$module==[^ \\]+" "$HERE/packaging/appimage/requirements.lock" || printf '%s' "$module")"
                pins+=("$pin")
            done
            printf '\n  installing %s with pip… ' "${pins[*]}"
            "$VENV_PY" -m ensurepip --default-pip >/dev/null 2>&1 || die "pip could not be set up in the virtualenv"
            if ! "$VENV_PY" -m pip install --disable-pip-version-check -q "${pins[@]}"; then
                say "failed"
                say "Install them from your distribution instead (e.g. python3-paramiko python3-cryptography python3-pexpect), then run ./install.sh again."
                exit 1
            fi
        fi
    fi
    record t "$RELEASE"
    put_link "releases/$RELEASE_ID" "$OPT_DIR/current"
    record l "$OPT_DIR/current"
    FAILED_RELEASE=""
    SWITCHED=1
    ok "$VERSION"
else
    step "Installing the AppImage"
    make_dir "$OPT_DIR"
    put_file "$APPIMAGE" "$OPT_DIR/Ophion.AppImage" 755
    record f "$OPT_DIR/Ophion.AppImage"
    SWITCHED=1
    ok
fi

# ------------------------------------------------------------------------------------ 3. the command
step "Installing the '$APP_SLUG' command"
make_dir "$BIN_DIR"
if [ "$MODE" = source ]; then
    launcher="$(mktemp)"
    printf '#!/bin/sh\n# %s (written by install.sh; uninstall.sh removes it)\nexec %s -m %s "$@"\n' \
        "$APP_NAME" "$(sh_quote "$OPT_DIR/current/venv/bin/python")" "$APP_SLUG" > "$launcher"
    if [ -L "$COMMAND" ] && [ "$DRY" -eq 0 ]; then rm -f -- "$COMMAND"; fi
    put_file "$launcher" "$COMMAND" 755
    rm -f -- "$launcher"
    record f "$COMMAND"
else
    if [ -f "$COMMAND" ] && [ ! -L "$COMMAND" ] && [ "$DRY" -eq 0 ]; then rm -f -- "$COMMAND"; fi
    put_link "$OPT_DIR/Ophion.AppImage" "$COMMAND"
    record l "$COMMAND"
fi
ok "$COMMAND"

# --------------------------------------------------------------------------------------- 4. icons
step "Installing the icon"
count=0
for size_dir in "$ICONS_SRC"/*x*; do
    [ -f "$size_dir/apps/$APP_SLUG.png" ] || continue
    target_dir="$DATA_DIR/icons/hicolor/$(basename "$size_dir")/apps"
    make_dir "$target_dir"
    put_file "$size_dir/apps/$APP_SLUG.png" "$target_dir/$APP_SLUG.png" 644
    record f "$target_dir/$APP_SLUG.png"
    count=$((count + 1))
done
[ "$count" -gt 0 ] || die "no icon files in $ICONS_SRC"
ok "$count sizes"

# ----------------------------------------------------------------------------------- 5. menu entry
step "Adding $APP_NAME to the application menu"
make_dir "$DATA_DIR/applications"
put_file "$ENTRY" "$DESKTOP" 644
rm -f -- "$ENTRY"
record f "$DESKTOP"
ok "$DESKTOP"

# ---------------------------------------------------------------------- old launchers and versions
legacy=()
for path in "$DATA_DIR/applications/"*"$LEGACY_SLUG"*.desktop "$DATA_DIR/applications/$LEGACY_APP_ID.desktop" \
            "$BIN_DIR/$LEGACY_SLUG" "$DATA_DIR/icons/hicolor/"*/apps/"$LEGACY_SLUG".png \
            "$DATA_DIR/icons/hicolor/"*/apps/"$LEGACY_SLUG".svg; do
    if [ -e "$path" ] || [ -L "$path" ]; then legacy+=("$path"); fi
done
if [ ${#legacy[@]} -gt 0 ]; then
    say "The old SSH Manager launcher files are replaced by $APP_NAME's (your data is not touched):"
    for path in "${legacy[@]}"; do
        say "  removing $path"
        [ "$DRY" -eq 1 ] || rm -f -- "$path"
    done
fi

if [ -n "$OLD_ENTRIES" ]; then
    while IFS=' ' read -r kind path; do  # the previous version's files that this one no longer has
        [ -n "$path" ] || continue
        if [ "$kind" = d ]; then
            printf '%s\n' "${NEW_ENTRIES[@]}" | grep -qxF -- "d $path" || NEW_ENTRIES+=("d $path")
            continue
        fi
        printf '%s\n' "${NEW_ENTRIES[@]}" | cut -d' ' -f2- | grep -qxF -- "$path" && continue  # still ours
        case "$path" in "$OPT_DIR"/*|"$BIN_DIR"/*|"$DATA_DIR"/applications/*|"$DATA_DIR"/icons/*) ;; *) continue ;; esac
        if [ "$DRY" -eq 1 ]; then say "  would remove the previous $path"; else rm -rf -- "$path"; fi
    done <<< "$OLD_ENTRIES"
fi

if [ "$DRY" -eq 0 ]; then
    make_dir "$OPT_DIR"
    printf '%s\n' "${NEW_ENTRIES[@]}" | LC_ALL=C sort -u > "$MANIFEST.tmp.$$"
    mv -f -- "$MANIFEST.tmp.$$" "$MANIFEST"
fi

# ------------------------------------------------------------------------------------ 6. caches
step "Refreshing the desktop's menu and icon caches"
refreshed=()
if [ "$DRY" -eq 1 ]; then
    ok "skipped (dry run)"
else
    # Only caches that already exist are refreshed: a new icon-theme.cache would hide icons other apps add
    # later without updating it, and a new mimeinfo.cache is not needed (the entry has no MimeType).
    if [ -f "$DATA_DIR/icons/hicolor/icon-theme.cache" ] && command -v gtk-update-icon-cache >/dev/null 2>&1 \
            && gtk-update-icon-cache -f -t -q "$DATA_DIR/icons/hicolor" >/dev/null 2>&1; then refreshed+=(icons); fi
    if [ -f "$DATA_DIR/applications/mimeinfo.cache" ] && command -v update-desktop-database >/dev/null 2>&1 \
            && update-desktop-database -q "$DATA_DIR/applications" >/dev/null 2>&1; then refreshed+=(menu); fi
    if command -v xdg-desktop-menu >/dev/null 2>&1 \
            && XDG_DATA_HOME="$DATA_DIR" xdg-desktop-menu forceupdate >/dev/null 2>&1; then refreshed+=(xdg-menu); fi
    ok "${refreshed[*]:-nothing to refresh}"
fi

trap - EXIT
say ""
if [ "$DRY" -eq 1 ]; then
    say "Dry run finished: nothing was changed."
    exit 0
fi
say "$APP_NAME is installed."
say "  Start it:   from the application menu (search \"$APP_NAME\"), or run: $APP_SLUG"
say "  Files:      $OPT_DIR (list: $MANIFEST)"
say "  Update:     run ./install.sh again"
say "  Uninstall:  ./uninstall.sh   (your hosts, passwords and settings are kept)"
case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR is not in your PATH, so the '$APP_SLUG' command is not found in a terminal. Add this line to ~/.profile, then log in again:"
       printf '  export PATH="%s:$PATH"\n' "$BIN_DIR" >&2 ;;
esac
