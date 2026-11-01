# Ophion AppImage

One file, `Ophion-<version>-x86_64.AppImage`, that runs Ophion on a desktop Linux
without installing Python, GTK, VTE, PyGObject, paramiko, OpenSSH, sshpass or FreeRDP.

## Build

```bash
./packaging/appimage/build.sh            # needs docker or podman; writes dist/
VERSION=1.0.0 ./packaging/appimage/build.sh
packaging/appimage/tests/smoke.sh        # clean-room tests (ubuntu:22.04, debian:12-slim, fedora:40)
```

`build.sh` builds the image from `Dockerfile.build` (Ubuntu 22.04), then runs itself inside it. The
application is taken from the **committed** `HEAD` (`git archive`), so uncommitted changes in
`main.py` / `ssh_manager/` are not packaged (the script says so). Outputs in `dist/`:

| File | What |
|---|---|
| `Ophion-<version>-x86_64.AppImage` | The application |
| `SHA256SUMS` | Checksum of the AppImage |
| `MANIFEST.txt` | Every file inside, with its SHA-256 (compare two builds with `diff`) |
| `BUNDLE-VERSIONS.txt` | Versions of everything bundled (also inside, `usr/share/ophion/`) |
| `host-libraries-used.txt` | The libraries the AppImage takes from the host |

The default version is `git<commits>.<short hash>` (the repository has no release tags yet).
Downloads are cached in `~/.cache/ophion-appimage-build` and always checked against their
SHA-256.

### Pinned inputs

* Base image `ubuntu:22.04` by digest; apt from the dated snapshot `snapshot.ubuntu.com/ubuntu/20261020T000000Z`
  (`SNAPSHOT` in `Dockerfile.build`).
* FreeRDP 3.32.2, appimagetool 1.9.1 and the static type-2 runtime 20251108, by version and SHA-256.
* Python libraries: `requirements.lock` (wheels, every dependency, `--require-hashes`); PyGObject 3.50.0 and
  pycairo 1.27.0 are built from their hashed source releases with the build tools in
  `requirements-build.lock`.

Two builds of the same commit give a byte-identical AppImage (same SHA-256; checked): timestamps are
set to the commit time, Python byte code is written with `unchecked-hash` in a fixed build folder,
and every input is pinned. `MANIFEST.txt` lists each file's SHA-256 to find the cause if they ever differ.

## Supported systems

x86_64 with glibc 2.35 or newer: Ubuntu 22.04+, Debian 12+, Fedora 38+, current Arch, openSUSE
Tumbleweed / Leap 15.6, Linux Mint 21+. X11 or Wayland (Wayland through GTK's own backend; the
embedded remote desktop needs XWayland, see below).

The host must provide (every desktop does): glibc, the X11 client library and libxcb, fontconfig,
freetype and at least one font, the C++ runtime (`libstdc++`), and for 3D-accelerated drawing its own
GL drivers. See `host-libraries.txt` for the exact list; the build fails if anything else would be
taken from the host.

## Run

```bash
chmod +x Ophion-*-x86_64.AppImage
./Ophion-*-x86_64.AppImage
./Ophion-*-x86_64.AppImage --appimage-extract-and-run     # no FUSE (containers, some servers)
./Ophion-*-x86_64.AppImage --rdp host --user alice        # every command-line option works as usual
```

Settings, the inventory, credentials and caches live in the usual places (`~/.config/ophion`,
`~/.local/share/ophion`, `~/.cache`), exactly as with a normal install, so an AppImage and an
installed copy share them. The first start moves an SSH Manager installation's folders there (see
"Upgrading from SSH Manager" in the main README). Nothing is written inside the AppImage. The only extra
files are two tiny GTK module caches with the current mount path in `~/.cache/ophion-appimage/` (stale
ones are removed at the next start).

The taskbar and dock show Ophion's icon once the AppImage is integrated into the desktop (for example
with AppImageLauncher), which installs `io.github.ophion.Ophion.desktop` (`Icon=ophion`). GNOME on
Wayland finds the icon through that desktop file and the application id. Without integration, the
window still has the icon: the app sets it itself from the bundled `usr/share/icons/hicolor` (AppRun
puts `usr/share` on `XDG_DATA_DIRS`).

`OPHION_APPIMAGE_EXEC=1 ./Ophion.AppImage <program> [args]` (formerly `SSH_MANAGER_APPIMAGE_EXEC`) runs a bundled program with the
AppImage's environment instead of the app (support and tests), e.g. `... ssh -V` or `... python3 script.py`.
The program runs as a child of the launcher, which keeps the AppImage mounted: `ssh` closes every
inherited file descriptor at start, and run directly by the AppImage runtime it would release the
mount and die with SIGBUS (inside the app this cannot happen: ssh is started by the running app).

## What is bundled

| Component | Version | From |
|---|---|---|
| Python | 3.10.12 (+ sqlite3, ssl/OpenSSL 3.0.2, ctypes) | Ubuntu 22.04 |
| GTK / GDK | 3.24.33 | Ubuntu 22.04 |
| ATK, the AT-SPI bridge and libatspi (screen-reader support) | at-spi2-core 2.56.2 | built from source (Ubuntu 22.04's 2.38 bridge cannot parse a current desktop's accessibility registry) |
| VTE | 0.68.0 | Ubuntu 22.04 |
| librsvg, GdkPixbuf (+ loaders), Pango, cairo, HarfBuzz, GLib, GObject-introspection | Ubuntu 22.04 | Ubuntu 22.04 |
| Adwaita and hicolor icons, shared MIME data, GSettings schemas (compiled), dconf GIO module | Ubuntu 22.04 | Ubuntu 22.04 |
| libsecret + its typelib (for keyring's libsecret backend) | 0.20.5 | Ubuntu 22.04 |
| libcanberra, canberra-gtk-module and its ALSA / PulseAudio drivers (GNOME asks every GTK app to load the module for event sounds) | 0.30 | Ubuntu 22.04 |
| PyGObject / pycairo | 3.50.0 / 1.27.0 | built from source |
| paramiko, cryptography, bcrypt, PyNaCl, pexpect, keyring (+ SecretStorage, jeepney), certifi | see `requirements.lock` | PyPI wheels |
| OpenSSH client (`ssh`, `ssh-keygen`, `ssh-keyscan`, `ssh-add`) | 8.9p1 | Ubuntu 22.04 |
| sshpass | 1.09 | Ubuntu 22.04 |
| xdotool (+ libxdo) | 3.20160805.1 | Ubuntu 22.04 |
| FreeRDP 3 X11 client, shipped as `xfreerdp3` | 3.32.2 | built from source |

The bundled programs come first in `PATH`, so the app always uses this one consistent set.

GObject-introspection only sees the AppImage's typelibs: the host's typelib folders, compiled into
libgirepository, are disabled at build time (`/usr/lib/` becomes `/no-host/` in the bundled library).
Otherwise a namespace the AppImage does not ship would load the host's library, built against a
newer GLib (for example "Failed to load shared library 'libsecret-1.so.0' … undefined symbol:
g_task_set_static_name"). A `GI_TYPELIB_PATH` you set yourself is still honoured.

The same applies to libcanberra's sound drivers: its compiled-in driver path is shortened to a bare
name at build time, so they are found through `LTDL_LIBRARY_PATH` inside the AppImage (they talk to the
host's PulseAudio / PipeWire server as usual).

**Not bundled:**

* **gtk-frdp** (the "Embedded (gtk-frdp)" remote-desktop engine). It is not packaged for Ubuntu 22.04 and
  not trivial to build. Settings → Remote Desktop → Diagnostics then says "gtk-frdp (the Frdp typelib)
  is not installed", and the app uses FreeRDP inside the tab (X11/XWayland) or in its own window.
* The legacy terminal (Textual) interface and its `rich` / `textual` packages; only the GTK app is packaged.
* `wmctrl` (the RDP tab's "Focus Window" also works with the bundled xdotool).
* Translations: Ophion has none yet; GTK's own few strings use the host's GTK translations if present.

## How it behaves on the host

* **ssh** reads the user's `~/.ssh/config`, `~/.ssh/known_hosts`, `/etc/ssh/ssh_config` and the agent
  (`SSH_AUTH_SOCK`) exactly as the host's ssh does: it never looks inside the AppImage. ssh takes the
  home folder from the password database (not `$HOME`), like any OpenSSH.
* **Passwords** are handed to `sshpass -e` through the environment, as in a normal install; host keys
  are verified the same way.
* **Certificates**: the host's CA bundle (`/etc/ssl/certs/ca-certificates.crt`,
  `/etc/pki/tls/certs/ca-bundle.crt`, …) is used; without one, the bundled certifi bundle.
* **Keyring**: with a D-Bus session and a Secret Service (GNOME Keyring, KWallet), passwords go to the
  desktop keyring; without one the app uses its own encrypted store, as it always does.
* **Theme**: no theme is forced; the app's own CSS is used and GTK reads the desktop's settings.

## Known limits and failure modes

| Situation | What happens |
|---|---|
| No FUSE (containers, hardened servers) | `--appimage-extract-and-run`, or `--appimage-extract` and run `squashfs-root/AppRun` |
| No D-Bus session / no Secret Service | The app starts; passwords use its encrypted store (tested) |
| Wayland without XWayland | SSH, terminals and file transfer work (GTK's Wayland backend). Remote desktops do not: the bundled FreeRDP is the X11 client only and the "FreeRDP in the tab" engine needs X11, so a session fails with FreeRDP's "cannot open display" error. Every Wayland desktop session ships XWayland by default. (Not tested here: no Wayland compositor was available.) |
| No display at all | A clean "cannot open display" error, exit code 1 (tested) |
| No fonts on the host | Text is unreadable; install any font (fontconfig + e.g. DejaVu) |
| A host `ssh_config` with options newer than OpenSSH 8.9 | ssh reports "Bad configuration option" for that line, as an older host ssh would; such options can be guarded with `IgnoreUnknown` |
| Security updates | Bundled libraries are fixed at the snapshot date; rebuild with a newer `SNAPSHOT` to pick up fixes |

## Files

| File | Purpose |
|---|---|
| `Dockerfile.build` | The pinned build image (Ubuntu 22.04, FreeRDP 3, appimagetool) |
| `build.sh` | The one build command (host and container side) |
| `AppRun` | The launcher: environment for the bundled Python / GTK / programs |
| `host-libraries.txt` | Libraries allowed from the host (library audit) |
| `requirements.lock`, `requirements-build.lock` | Hashed Python dependencies |
| `io.github.ophion.Ophion.desktop`, `io.github.ophion.Ophion.metainfo.xml` | Desktop integration, AppStream (named after the application id; the icon, `ophion`, comes from `ssh_manager/gtkui/assets/app-icon/hicolor`) |
| `tests/smoke.sh`, `tests/selfcheck.py`, `tests/e2e_ssh.py` | Clean-room tests |

There is no CI configuration: the repository does not use GitHub Actions yet. A CI job would run
`./packaging/appimage/build.sh && packaging/appimage/tests/smoke.sh` and upload `dist/`.
