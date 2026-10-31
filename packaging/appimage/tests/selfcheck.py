"""Self-check inside the AppImage environment (bundled Python, run by tests/smoke.sh under Xvfb).

    SSH_MANAGER_APPIMAGE_EXEC=1 ./SSH_Manager.AppImage python3 selfcheck.py

Prints one ``PASS`` / ``FAIL`` line per check and exits 1 if any check failed.
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

APPDIR = os.environ.get("SSH_MANAGER_APPDIR", "")
RESULTS = []


def check(name, func):
    try:
        detail = func()
        RESULTS.append((True, name, detail or ""))
    except Exception as exc:  # report every failure, keep going
        RESULTS.append((False, name, f"{type(exc).__name__}: {exc}"))


def inside(path):
    return bool(APPDIR) and os.path.realpath(path).startswith(os.path.realpath(APPDIR) + os.sep)


def imports():
    import cryptography
    import gi  # noqa: F401
    import keyring  # noqa: F401
    import paramiko
    import pexpect  # noqa: F401
    import sqlite3
    import ssl

    for module in (gi, paramiko, sqlite3):
        assert inside(module.__file__), f"{module.__name__} is not the bundled one: {module.__file__}"
    assert inside(sys.executable) and inside(ssl.__file__), sys.executable
    return (f"Python {sys.version.split()[0]}, PyGObject {gi.__version__}, paramiko {paramiko.__version__}, "
            f"cryptography {cryptography.__version__}, SQLite {sqlite3.sqlite_version}, {ssl.OPENSSL_VERSION}")


def namespaces():
    import gi

    loaded = []
    for namespace, version in (("Gtk", "3.0"), ("Gdk", "3.0"), ("GdkX11", "3.0"), ("Vte", "2.91"), ("Rsvg", "2.0"),
                               ("PangoCairo", "1.0"), ("GIRepository", "2.0"), ("GdkPixbuf", "2.0")):
        gi.require_version(namespace, version)
        module = __import__("gi.repository", fromlist=[namespace])
        getattr(module, namespace)
        loaded.append(f"{namespace}-{version}")
    from gi.repository import Gtk, Vte

    return (f"{', '.join(loaded)}; GTK {Gtk.get_major_version()}.{Gtk.get_minor_version()}.{Gtk.get_micro_version()}, "
            f"VTE {Vte.get_major_version()}.{Vte.get_minor_version()}.{Vte.get_micro_version()}")


def programs():
    found = {}
    for program in ("ssh", "ssh-keygen", "ssh-keyscan", "ssh-add", "sshpass", "xdotool", "xfreerdp3"):
        path = shutil.which(program)
        assert path and inside(path), f"{program} -> {path} (not inside the AppImage)"
        found[program] = path
    versions = {
        "ssh": subprocess.run(["ssh", "-V"], capture_output=True, text=True, timeout=20).stderr.strip(),
        "sshpass": subprocess.run(["sshpass", "-V"], capture_output=True, text=True, timeout=20).stdout.splitlines()[0],
        "xdotool": subprocess.run(["xdotool", "version"], capture_output=True, text=True, timeout=20).stdout.strip(),
        "xfreerdp3": subprocess.run(["xfreerdp3", "--version"], capture_output=True, text=True,
                                    timeout=20).stdout.strip().splitlines()[0],
    }
    for name, text in versions.items():
        assert text, f"{name} printed no version"
    return "; ".join(f"{k}: {v}" for k, v in versions.items())


def ssh_reads_the_users_config():
    home = Path(tempfile.mkdtemp())
    (home / ".ssh").mkdir(mode=0o700)
    (home / ".ssh" / "config").write_text("Host selfcheck-alias\n    HostName 192.0.2.10\n    User selfcheck-user\n")
    os.chmod(home / ".ssh" / "config", 0o600)
    env = dict(os.environ, HOME=str(home))
    out = subprocess.run(["ssh", "-G", "-F", str(home / ".ssh" / "config"), "selfcheck-alias"], capture_output=True,
                         text=True, env=env, timeout=20).stdout
    assert "user selfcheck-user" in out and "hostname 192.0.2.10" in out, out[:300]
    # Without -F: the user's own ~/.ssh/config is read (ssh takes the home folder from the password
    # database, not $HOME), never anything inside the AppImage.
    import pwd

    real = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".ssh" / "config"
    verbose = subprocess.run(["ssh", "-v", "-G", "localhost"], capture_output=True, text=True, timeout=20).stderr
    read = [line.split("data ", 1)[1] for line in verbose.splitlines() if "Reading configuration data" in line]
    if real.exists():
        assert str(real) in read, f"{real} was not read: {read}"
    assert not any(APPDIR in path for path in read), f"configuration read inside the AppImage: {read}"
    out = subprocess.run(["ssh", "-G", "localhost"], capture_output=True, text=True, timeout=20).stdout
    assert APPDIR not in out, "ssh -G mentions the AppImage directory"
    known = [line for line in out.splitlines() if line.startswith(("userknownhostsfile", "globalknownhostsfile"))]
    return f"reads {read}; " + "; ".join(known)


def terminal_and_icon():
    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Vte", "2.91")
    gi.require_version("Rsvg", "2.0")
    from gi.repository import GdkPixbuf, Gtk, Rsvg, Vte

    assert Gtk.init_check(sys.argv)[0], "no display"
    terminal = Vte.Terminal()
    window = Gtk.OffscreenWindow()
    window.add(terminal)
    window.show_all()
    pixbuf = GdkPixbuf.Pixbuf.new_from_file(os.path.join(APPDIR, "ophion.png"))
    svg = os.path.join(APPDIR, "usr/share/ssh-manager/ssh_manager/gtkui/assets/icons/sshm-server-symbolic.svg")
    svg_pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_size(svg, 64, 64)
    handle = Rsvg.Handle.new_from_file(svg)
    theme = Gtk.IconTheme.get_default().lookup_icon("window-close-symbolic", 16, 0)
    assert theme is not None, "Adwaita's window-close-symbolic not found"
    app_icon = Gtk.IconTheme.get_default().lookup_icon("ophion", 48, 0)
    assert app_icon is not None, "the application icon (ophion) is not in the bundled hicolor theme"
    window.destroy()
    return (f"VTE terminal created; icon {pixbuf.get_width()}x{pixbuf.get_height()}; SVG "
            f"{svg_pixbuf.get_width()}x{svg_pixbuf.get_height()} via GdkPixbuf, Rsvg {bool(handle)}")


def certificates():
    import ssl

    context = ssl.create_default_context()
    count = context.cert_store_stats()["x509_ca"]
    assert count > 0, "no CA certificates loaded"
    return f"{count} CA certificates ({os.environ.get('SSL_CERT_FILE')})"


def the_app_imports():
    sys.path.insert(0, os.path.join(APPDIR, "usr/share/ssh-manager"))
    import ssh_manager.gtk_app  # noqa: F401
    from ssh_manager.rdp import engines

    assert engines.frdp_typelib_version() == "", "an Frdp typelib was found (gtk-frdp is not bundled)"
    report = engines.discover(platform=engines.detect_platform())
    by_id = {engine.id: engine for engine in report.engines}
    assert by_id["xfreerdp-embed"].available, by_id["xfreerdp-embed"].reason
    assert not by_id["frdp"].available
    binaries = ", ".join(f"{b.name} {b.version}" for b in report.binaries)
    return f"engines: {', '.join(e.id for e in report.engines if e.available)}; frdp: {by_id['frdp'].reason}; {binaries}"


def keyring_backend():
    # keyring tries its libsecret backend (gi "Secret"): it must load the bundled libsecret, without warnings.
    # (Its priority raises when no Secret Service runs, as in a container; keyring then uses another backend.)
    probe = ("import keyring, keyring.backends.libsecret as ls; k = keyring.get_keyring(); "
             "print(type(k).__module__, 'libsecret-loaded' if ls.available else 'libsecret-NOT-loaded')")
    run = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-500:]
    assert "libsecret-loaded" in run.stdout, run.stdout + run.stderr[-500:]
    assert "Failed to load" not in run.stderr and "-WARNING" not in run.stderr, run.stderr[-500:]
    return run.stdout.strip()


def sound_module():
    # GNOME asks every GTK 3 app to load canberra-gtk-module (XSETTINGS / GTK_MODULES): it must load, and
    # libcanberra must find its drivers inside the AppImage. Without a sound server or device a driver may
    # still fail to open; it must never be "no driver" (CA_ERROR_NODRIVER = -5).
    probe = ("import gi; gi.require_version('Gtk', '3.0'); from gi.repository import Gtk; Gtk.init_check([]); "
             "import ctypes; ca = ctypes.CDLL('libcanberra.so.0')\n"
             "for driver in (b'alsa', b'pulse'):\n"
             "    ctx = ctypes.c_void_p(); assert ca.ca_context_create(ctypes.byref(ctx)) == 0\n"
             "    assert ca.ca_context_set_driver(ctx, driver) == 0\n"
             "    print(driver.decode(), ca.ca_context_open(ctx))")
    env = dict(os.environ, GTK_MODULES="canberra-gtk-module")
    run = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60, env=env)
    assert run.returncode == 0, run.stderr[-500:]
    assert "Failed to load module" not in run.stderr, run.stderr[-300:]
    results = dict(line.split() for line in run.stdout.splitlines())
    assert set(results) == {"alsa", "pulse"} and "-5" not in results.values(), f"a driver was not found: {results}"
    return f"canberra-gtk-module loaded; libcanberra drivers found (open results {results})"


def terminal_text_without_warnings():
    # The app reads terminal text all the time (prompts, sentinel); on VTE 0.68 the old calls printed
    # two VTE-WARNINGs each time.
    probe = ("import sys, time; sys.path.insert(0, %r); "
             "import gi; gi.require_version('Gtk', '3.0'); gi.require_version('Vte', '2.91'); "
             "from gi.repository import Gtk, Vte; Gtk.init_check([]); "
             "from ssh_manager.gtk_terminal import TerminalTab, terminal_text\n"
             "term = Vte.Terminal(); term.feed(b'selfcheck line\\r\\nPassword: ')\n"
             "end = time.monotonic() + 5\n"
             "while 'selfcheck line' not in terminal_text(term) and time.monotonic() < end:\n"
             "    Gtk.main_iteration_do(False)\n"
             "print(repr(terminal_text(term)[:40]), repr(TerminalTab._terminal_tail(object(), term)[-20:]))"
             % os.path.join(APPDIR, "usr/share/ssh-manager"))
    run = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-500:]
    assert "selfcheck line" in run.stdout and "Password:" in run.stdout, run.stdout
    assert "VTE-WARNING" not in run.stderr, run.stderr[-400:]
    return run.stdout.strip()


def accessibility_bridge():
    # GTK talks to the desktop's accessibility registry (screen readers) through atk-bridge; Ubuntu
    # 22.04's 2.38 bridge cannot parse a current registry's replies ("unknown signature").
    probe = ("import gi, time; gi.require_version('Gtk', '3.0'); gi.require_version('Atk', '1.0'); "
             "from gi.repository import Atk, Gtk; Gtk.init_check([])\n"
             "w = Gtk.Window(title='selfcheck'); w.add(Gtk.Button(label='ok')); w.show_all()\n"
             "end = time.monotonic() + 2\n"
             "while time.monotonic() < end:\n"
             "    Gtk.main_iteration_do(False); time.sleep(0.01)\n"
             "print('atk', Atk.get_major_version(), Atk.get_minor_version())")
    run = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr[-500:]
    major, minor = (int(x) for x in run.stdout.split()[1:3])
    assert (major, minor) >= (2, 50), f"bundled ATK / AT-SPI {major}.{minor} is older than 2.50"
    assert "atk-bridge" not in run.stderr, run.stderr[-400:]
    return f"ATK / AT-SPI {major}.{minor}; no atk-bridge warnings"


def typelibs_only_from_the_appimage():
    import gi

    gi.require_version("GIRepository", "2.0")
    from gi.repository import GIRepository

    path = GIRepository.Repository.get_default().get_search_path()
    host = [p for p in path if p.rstrip("/") in ("/usr/lib/x86_64-linux-gnu/girepository-1.0", "/usr/lib/girepository-1.0",
                                                  "/usr/lib64/girepository-1.0")]
    assert not host, f"the host's typelib folders are on the search path: {host}"
    return f"search path: {path}"


for name, func in (("imports", imports), ("gi namespaces", namespaces), ("bundled programs", programs),
                   ("ssh config", ssh_reads_the_users_config), ("terminal and icons", terminal_and_icon),
                   ("CA certificates", certificates), ("app and RDP engines", the_app_imports),
                   ("keyring", keyring_backend), ("typelib search path", typelibs_only_from_the_appimage),
                   ("sound module", sound_module), ("terminal text", terminal_text_without_warnings),
                   ("accessibility bridge", accessibility_bridge)):
    check(name, func)
for ok, name, detail in RESULTS:
    print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
sys.exit(0 if all(ok for ok, _n, _d in RESULTS) else 1)
