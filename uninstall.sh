#!/usr/bin/env bash
# Remove what install.sh installed for the current user: ./uninstall.sh [options]
#
# Exactly the files listed in <prefix>/opt/ophion/manifest.txt are removed (and the folders install.sh
# created, when empty). Your hosts, passwords and settings stay unless --purge is given and confirmed by
# typing DELETE; the desktop keyring is never touched.
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

usage() {
    cat <<'EOF'
Usage: ./uninstall.sh [options]

  --prefix DIR   The prefix given to install.sh (default ~/.local)
  --dry-run      Print every action; change nothing
  --purge        Also delete your hosts, passwords and settings (asks you to type DELETE)
  --yes          Accepted for symmetry with install.sh; --purge still asks for DELETE
  -h, --help     Show this help
EOF
}

say() { printf '%s\n' "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

PREFIX="" DRY=0 PURGE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --prefix) [ $# -ge 2 ] || die "--prefix needs a directory"; PREFIX="$2"; shift 2 ;;
        --prefix=*) PREFIX="${1#*=}"; shift ;;
        --dry-run) DRY=1; shift ;;
        --purge) PURGE=1; shift ;;
        --yes|-y) shift ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown option: $1" ;;
    esac
done

if [ "$(id -u)" -eq 0 ]; then
    die "do not run uninstall.sh as root: it removes one user's installation, under that user's home"
fi
[ -n "${HOME:-}" ] && [ -d "$HOME" ] || die "HOME is not set to a directory"

identity() { sed -n "s/^$1 = \"\\([^\"]*\\)\".*/\\1/p" "$HERE/ophion/identity.py" 2>/dev/null; }
APP_SLUG="$(identity APP_SLUG)"
APP_NAME="$(identity APP_NAME)"
APP_SLUG="${APP_SLUG:-ophion}"
APP_NAME="${APP_NAME:-Ophion}"

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
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
STATE_HOME="${XDG_STATE_HOME:-$HOME/.local/state}"
USER_DATA=("$CONFIG_HOME/$APP_SLUG" "$DATA_HOME/$APP_SLUG" "$STATE_HOME/$APP_SLUG")

remove() {
    if [ "$DRY" -eq 1 ]; then say "  would remove $1"; else rm -rf -- "$1"; fi
}

ours() {  # a manifest line may only name what install.sh writes
    case "$1" in
        *"/../"*|*"/.."|"") return 1 ;;
        "$OPT_DIR"|"$OPT_DIR"/*|"$BIN_DIR"/"$APP_SLUG"|"$DATA_DIR"/applications/*|"$DATA_DIR"/icons/*) return 0 ;;
        "$BIN_DIR"|"$DATA_DIR"|"$DATA_DIR"/applications|"$DATA_DIR"/icons|"$PREFIX"|"$PREFIX"/opt) return 0 ;;
    esac
    return 1
}

say "Removing $APP_NAME$([ "$DRY" -eq 1 ] && printf ' (dry run: nothing is changed)')"
if [ -f "$MANIFEST" ]; then
    removed=0
    dirs=()
    while IFS=' ' read -r kind path; do
        [ -n "$path" ] || continue
        if ! ours "$path"; then
            say "  skipping $path (not a place install.sh writes to)"
            continue
        fi
        case "$kind" in
            f|l|t)
                if [ -e "$path" ] || [ -L "$path" ]; then remove "$path"; removed=$((removed + 1)); fi ;;
            d) dirs+=("$path") ;;
        esac
    done < "$MANIFEST"
    remove "$MANIFEST"
    # Folders install.sh created, deepest first, only when empty (anything else in them is kept).
    while IFS= read -r dir; do
        [ -d "$dir" ] || continue
        if [ "$DRY" -eq 1 ]; then
            say "  would remove $dir if empty"
        else
            rmdir -- "$dir" 2>/dev/null || true
        fi
    done < <(printf '%s\n' "${dirs[@]}" | awk 'length { print length($0) " " $0 }' | sort -rn | cut -d' ' -f2- | uniq)
    say "Removed $removed installed files and links."
    if [ "$DRY" -eq 0 ]; then  # refresh the caches that exist (install.sh never creates them)
        if [ -f "$DATA_DIR/icons/hicolor/icon-theme.cache" ] && command -v gtk-update-icon-cache >/dev/null 2>&1; then
            gtk-update-icon-cache -f -t -q "$DATA_DIR/icons/hicolor" >/dev/null 2>&1 || true
        fi
        if [ -f "$DATA_DIR/applications/mimeinfo.cache" ] && command -v update-desktop-database >/dev/null 2>&1; then
            update-desktop-database -q "$DATA_DIR/applications" >/dev/null 2>&1 || true
        fi
        if command -v xdg-desktop-menu >/dev/null 2>&1; then
            XDG_DATA_HOME="$DATA_DIR" xdg-desktop-menu forceupdate >/dev/null 2>&1 || true
        fi
    fi
else
    say "$APP_NAME is not installed under $PREFIX (no $MANIFEST)."
    [ "$PURGE" -eq 1 ] || exit 1
fi

if [ "$PURGE" -eq 0 ]; then
    say "Your hosts, credentials and settings were kept in $CONFIG_HOME/$APP_SLUG and $DATA_HOME/$APP_SLUG."
    exit 0
fi

existing=()
for dir in "${USER_DATA[@]}"; do [ -e "$dir" ] && existing+=("$dir"); done
if [ ${#existing[@]} -eq 0 ]; then
    say "No hosts, credentials or settings to delete."
else
    say ""
    say "--purge deletes your hosts, saved passwords, macros and settings:"
    printf '  %s\n' "${existing[@]}"
    printf 'This cannot be undone. Type DELETE to delete them: '
    answer=""
    read -r answer || true
    if [ "$answer" = "DELETE" ]; then
        for dir in "${existing[@]}"; do remove "$dir"; done
        say "Deleted."
    else
        say ""
        say "Not confirmed: your hosts, credentials and settings were kept."
        exit 0
    fi
fi
say "The desktop keyring was not changed. If you used \"Remember on this computer\", remove that entry with:"
say "  secret-tool clear service ssh-manager    (or delete \"ssh-manager\" entries in Passwords and Keys / Seahorse)"
