"""Which RDP engines exist on this computer, what they can do, and which one to use (pure Python).

Three engines, tried in this order unless the preference or the host says
otherwise (``docs/rdp.md``):

1. ``frdp`` — the embedded ``gtk-frdp`` widget (GObject Introspection
   namespace ``Frdp``; X11 and Wayland).
2. ``xfreerdp-embed`` — ``xfreerdp`` / ``xfreerdp3`` inside a ``Gtk.Socket``
   with ``/parent-window:<XID>`` (X11 / XWayland only).
3. ``external`` — any FreeRDP client in its own window; the tab shows a
   session card. Works whenever a client binary exists.

Discovery runs each binary with ``/version`` and ``/help`` (never with
credentials, a short timeout, a minimal environment) and parses the FreeRDP
version and the options the binary knows, because 2.x and 3.x spell several
options differently and builds differ in what they include (USB, smart cards…).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

BINARIES = ("xfreerdp3", "xfreerdp", "sdl-freerdp3", "sdl-freerdp", "wlfreerdp3", "wlfreerdp")
EMBEDDABLE = ("xfreerdp3", "xfreerdp")  # clients with /parent-window (X11 window embedding)
ENGINE_IDS = ("frdp", "xfreerdp-embed", "external")
ENGINE_LABELS = {"frdp": "Embedded (gtk-frdp)", "xfreerdp-embed": "Embedded (FreeRDP in the tab, X11)",
                 "external": "External window"}
CAPABILITIES = ("embedded", "dynamic_resize", "smart_sizing", "fullscreen", "clipboard", "drives", "printers",
                "audio_out", "audio_in", "usb", "smartcard", "gateway", "multimon", "remoteapp", "keyboard_grab",
                "screenshot", "send_keys")
CAPABILITY_LABELS = {
    "embedded": "Remote screen inside the tab", "dynamic_resize": "Follows window size changes",
    "smart_sizing": "Smart sizing (scale to the window)", "fullscreen": "Full screen",
    "clipboard": "Clipboard sharing", "drives": "Shared folders", "printers": "Printers",
    "audio_out": "Sound", "audio_in": "Microphone", "usb": "USB devices", "smartcard": "Smart cards",
    "gateway": "RD Gateway", "multimon": "Multiple monitors", "remoteapp": "RemoteApp",
    "keyboard_grab": "Keyboard grab", "screenshot": "Screenshots", "send_keys": "Send Ctrl+Alt+Del",
}
# capability → option the client's /help must list (any of them)
_OPTION_FOR = {
    "dynamic_resize": ("dynamic-resolution",), "smart_sizing": ("smart-sizing",), "fullscreen": ("f",),
    "clipboard": ("clipboard",), "drives": ("drive",), "printers": ("printer",), "audio_out": ("sound",),
    "audio_in": ("microphone",), "usb": ("usb",), "smartcard": ("smartcard",), "gateway": ("gateway", "g"),
    "multimon": ("multimon",), "remoteapp": ("app",), "keyboard_grab": ("grab-keyboard",),
}
VERSION_TIMEOUT = 3.0
_VERSION = re.compile(r"FreeRDP version (\d+)\.(\d+)\.(\d+)")
_OPTION_LINE = re.compile(r"^ {4}(?:\[[-|/]+\]|[/+-])([A-Za-z][A-Za-z0-9_-]*)", re.MULTILINE)

Runner = Callable[[Sequence[str], float], Tuple[int, str]]  # (argv, timeout) -> (exit code, stdout+stderr)


def _run(argv: Sequence[str], timeout: float) -> Tuple[int, str]:
    """Run a client with no credentials, no stdin, a minimal environment and a timeout."""

    env = {key: os.environ[key] for key in ("PATH", "HOME", "LD_LIBRARY_PATH") if key in os.environ}
    env.update(LANG="C", LC_ALL="C")
    try:
        done = subprocess.run(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout, env=env, close_fds=True, start_new_session=True, check=False)
    except subprocess.TimeoutExpired:
        return -1, "timed out"
    except OSError as exc:
        return -1, exc.strerror or type(exc).__name__
    return done.returncode, done.stdout[:262144].decode("utf-8", "replace")


# --------------------------------------------------------------- binaries
@dataclass(frozen=True)
class ClientBinary:
    """One FreeRDP client found on this computer."""

    name: str  # xfreerdp3, sdl-freerdp, …
    path: str
    version: Tuple[int, int, int] = (0, 0, 0)
    version_text: str = ""
    options: FrozenSet[str] = frozenset()  # lower-case option names from /help
    error: str = ""  # why it is unusable ("" = usable)

    @property
    def major(self) -> int:
        return self.version[0]

    @property
    def usable(self) -> bool:
        return not self.error and self.major in (2, 3)

    def has(self, option: str) -> bool:
        return option.lower() in self.options

    @property
    def secret_channel(self) -> str:
        """How a password reaches this client without argv: ``args-from`` (3.x), ``from-stdin`` or ""."""

        if self.has("args-from"):
            return "args-from"
        if self.has("from-stdin"):
            return "from-stdin"
        return ""


def parse_version(text: str) -> Tuple[Tuple[int, int, int], str]:
    match = _VERSION.search(text or "")
    if not match:
        return (0, 0, 0), ""
    version = tuple(int(part) for part in match.groups())
    return (version[0], version[1], version[2]), ".".join(match.groups())


def parse_help_options(text: str) -> FrozenSet[str]:
    """Option names from ``/help`` (2.x and 3.x layouts: ``/name``, ``+name``, ``-name``, ``[-|/]name``)."""

    return frozenset(name.lower() for name in _OPTION_LINE.findall(text or ""))


def find_binaries(path_env: Optional[str] = None, override: str = "") -> List[Tuple[str, str]]:
    """``[(name, absolute path)]`` of FreeRDP clients: the override first, then ``PATH`` in :data:`BINARIES` order."""

    found: List[Tuple[str, str]] = []
    if override:
        candidate = os.path.expanduser(override)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            found.append((os.path.basename(candidate), os.path.abspath(candidate)))
    for name in BINARIES:
        path = shutil.which(name, path=path_env if path_env is not None else os.environ.get("PATH", ""))
        if path and all(os.path.realpath(path) != os.path.realpath(p) for _n, p in found):
            found.append((name, path))
    return found


def inspect_binary(name: str, path: str, run: Runner = _run) -> ClientBinary:
    """Version and options of one client (``/version``, then ``--version`` for very old builds; ``/help``)."""

    code, output = run([path, "/version"], VERSION_TIMEOUT)
    version, version_text = parse_version(output)
    if version == (0, 0, 0):
        code, output = run([path, "--version"], VERSION_TIMEOUT)
        version, version_text = parse_version(output)
    if version == (0, 0, 0):
        reason = "it did not answer in time" if output == "timed out" else "it did not report a FreeRDP version"
        return ClientBinary(name, path, error=f"{name}: {reason}.")
    if version[0] not in (2, 3):
        return ClientBinary(name, path, version, version_text,
                            error=f"{name} {version_text}: only FreeRDP 2.x and 3.x are supported.")
    _code, help_text = run([path, "/help"], VERSION_TIMEOUT)
    options = parse_help_options(help_text)
    if not options:
        return ClientBinary(name, path, version, version_text, error=f"{name}: its option list could not be read.")
    return ClientBinary(name, path, version, version_text, options)


# ------------------------------------------------------------- platform
@dataclass(frozen=True)
class Platform:
    session_type: str = ""  # "x11" | "wayland" | ""
    gdk_x11: bool = False  # GTK runs on the X11 backend (X11 or XWayland)
    frdp_version: str = ""  # gtk-frdp typelib version, "" = not installed
    frdp_problem: str = ""  # why an installed gtk-frdp cannot be used ("" = usable)
    flatpak: bool = False
    distro: str = ""  # os-release ID (and ID_LIKE)


def detect_platform(env: Optional[Mapping[str, str]] = None, *, gdk_x11: Optional[bool] = None,
                    frdp_version: Optional[str] = None, os_release: Optional[str] = None,
                    frdp_problem: Optional[str] = None) -> Platform:
    """The session type, GTK backend, gtk-frdp and distribution (each overridable for tests)."""

    env = os.environ if env is None else env
    session = (env.get("XDG_SESSION_TYPE") or "").lower()
    if session not in ("x11", "wayland"):
        session = "wayland" if env.get("WAYLAND_DISPLAY") else ("x11" if env.get("DISPLAY") else "")
    if gdk_x11 is None:
        gdk_x11 = _gdk_is_x11()
    if frdp_version is None:
        frdp_version = frdp_typelib_version()
    if os_release is None:
        try:
            os_release = Path("/etc/os-release").read_text(encoding="utf-8", errors="replace")[:4096]
        except OSError:
            os_release = ""
    fields_ = dict(re.findall(r'^([A-Z_]+)="?([^"\n]*)"?$', os_release, re.MULTILINE))
    distro = " ".join(part for part in (fields_.get("ID", ""), fields_.get("ID_LIKE", "")) if part).lower()
    problem = frdp_check(frdp_version) if frdp_version and frdp_problem is None else (frdp_problem or "")
    return Platform(session, bool(gdk_x11), frdp_version or "", problem, os.path.exists("/.flatpak-info"), distro)


def _gdk_is_x11() -> bool:
    try:
        import gi

        gi.require_version("Gdk", "3.0")
        gi.require_version("GdkX11", "3.0")
        from gi.repository import Gdk, GdkX11  # type: ignore

        display = Gdk.Display.get_default()
        return display is not None and isinstance(display, GdkX11.X11Display)
    except (ImportError, ValueError, AttributeError):
        return False


# What the gtk-frdp adapter uses (gtk-frdp 0.2, src/frdp-display.{h,c}); checked on the installed typelib.
FRDP_REQUIRED_METHODS = ("open_host", "close", "is_open", "set_scaling")
FRDP_REQUIRED_PROPERTIES = ("username", "password", "domain", "scaling")
FRDP_REQUIRED_SIGNALS = ("rdp-connected", "rdp-disconnected", "rdp-error", "rdp-auth-failure",
                         "rdp-needs-certificate-verification")


def frdp_missing(display_class: Any) -> List[str]:
    """Members the adapter needs that ``display_class`` (``Frdp.Display``) lacks ("" names = all there)."""

    missing = [f"method {name}" for name in FRDP_REQUIRED_METHODS if not callable(getattr(display_class, name, None))]
    try:
        from gi.repository import GObject  # type: ignore

        props = {spec.name for spec in display_class.list_properties()}
        missing += [f"property {name}" for name in FRDP_REQUIRED_PROPERTIES if name not in props]
        missing += [f"signal {name}" for name in FRDP_REQUIRED_SIGNALS
                    if not GObject.signal_lookup(name, display_class)]
    except Exception as exc:  # not a GObject class at all
        missing.append(f"introspection ({type(exc).__name__})")
    return missing


def frdp_check(version: str) -> str:
    """Load the ``Frdp`` typelib and return why it cannot be used ("" = usable)."""

    try:
        import gi

        gi.require_version("Frdp", version)
        from gi.repository import Frdp  # type: ignore
    except (ImportError, ValueError) as exc:
        return f"gtk-frdp {version} could not be loaded ({type(exc).__name__})."
    display = getattr(Frdp, "Display", None)
    if display is None:
        return f"gtk-frdp {version} has no Display widget."
    missing = frdp_missing(display)
    return f"gtk-frdp {version} lacks {', '.join(missing)}." if missing else ""


def frdp_typelib_version() -> str:
    """The installed ``Frdp`` typelib version ("" when gtk-frdp is not installed); nothing is loaded."""

    try:
        import gi

        gi.require_version("GIRepository", "2.0")
        from gi.repository import GIRepository  # type: ignore

        versions = GIRepository.Repository.get_default().enumerate_versions("Frdp")
    except (ImportError, ValueError, AttributeError):
        return ""
    return sorted(versions)[-1] if versions else ""


# --------------------------------------------------------------- engines
@dataclass
class Engine:
    id: str  # frdp | xfreerdp-embed | external
    label: str
    available: bool
    reason: str = ""  # why it is not available (English)
    binary: Optional[ClientBinary] = None
    capabilities: Dict[str, bool] = field(default_factory=dict)

    @property
    def embedded(self) -> bool:
        return self.capabilities.get("embedded", False)

    def supports(self, capability: str) -> bool:
        return self.capabilities.get(capability, False)


def binary_capabilities(binary: ClientBinary, *, embedded: bool) -> Dict[str, bool]:
    caps = {name: any(binary.has(option) for option in _OPTION_FOR.get(name, ())) for name in CAPABILITIES}
    caps["embedded"] = embedded
    caps["screenshot"] = False  # no FreeRDP CLI option; the embedded widget may offer it (R2)
    caps["send_keys"] = False  # the external window receives keys itself
    if binary.name.startswith("wlfreerdp"):
        caps["keyboard_grab"] = caps["multimon"] = False  # the Wayland client has neither
    return caps


# gtk-frdp 0.2: it connects in-process with its own FreeRDP settings (no command line), so host options for
# devices, gateways, RemoteApp … do not apply; it resizes ("allow-resize"), scales ("scaling"), shares the
# clipboard, grabs the keyboard itself (is_keyboard_grabbed) and can render a pixbuf (get_pixbuf).
FRDP_CAPABILITIES = {name: name in ("embedded", "dynamic_resize", "fullscreen", "clipboard", "keyboard_grab",
                                    "smart_sizing", "screenshot") for name in CAPABILITIES}


def build_engines(binaries: Sequence[ClientBinary], platform: Platform) -> List[Engine]:
    """The three engines with availability, reasons and capabilities."""

    usable = [b for b in binaries if b.usable]
    engines: List[Engine] = []
    if platform.frdp_version and platform.frdp_problem:
        engines.append(Engine("frdp", ENGINE_LABELS["frdp"], False, platform.frdp_problem))
    elif platform.frdp_version:
        engines.append(Engine("frdp", ENGINE_LABELS["frdp"], True, capabilities=dict(FRDP_CAPABILITIES)))
    else:
        engines.append(Engine("frdp", ENGINE_LABELS["frdp"], False, "gtk-frdp (the Frdp typelib) is not installed."))
    x11 = [b for b in usable if b.name in EMBEDDABLE and b.has("parent-window")] or \
        [b for b in usable if b.has("parent-window") and "xfreerdp" in b.name]
    if not platform.gdk_x11:
        engines.append(Engine("xfreerdp-embed", ENGINE_LABELS["xfreerdp-embed"], False,
                              "Embedding a FreeRDP window needs X11 (this session runs GTK on Wayland)."))
    elif not x11:
        engines.append(Engine("xfreerdp-embed", ENGINE_LABELS["xfreerdp-embed"], False,
                              "No xfreerdp / xfreerdp3 client with /parent-window was found."))
    else:
        engines.append(Engine("xfreerdp-embed", ENGINE_LABELS["xfreerdp-embed"], True, binary=x11[0],
                              capabilities=binary_capabilities(x11[0], embedded=True)))
    external = preferred_external(usable, platform)
    if external is None:
        engines.append(Engine("external", ENGINE_LABELS["external"], False, "No FreeRDP client was found."))
    else:
        engines.append(Engine("external", ENGINE_LABELS["external"], True, binary=external,
                              capabilities=binary_capabilities(external, embedded=False)))
    return engines


def preferred_external(binaries: Sequence[ClientBinary], platform: Platform) -> Optional[ClientBinary]:
    """xfreerdp3 > xfreerdp > sdl > wl on X11; on a Wayland-only GTK, an X11 client still runs on XWayland,
    so the order holds — the Wayland clients come first only when no X11 client exists."""

    order = {name: index for index, name in enumerate(BINARIES)}
    ranked = sorted(binaries, key=lambda b: (b.major != 3, order.get(b.name, 99)))
    if platform.session_type == "wayland" and not any(b.name.startswith("xfreerdp") for b in ranked):
        ranked.sort(key=lambda b: (not b.name.startswith(("sdl-freerdp", "wlfreerdp")), b.major != 3))
    return ranked[0] if ranked else None


def choose_engine(engines: Sequence[Engine], preference: str = "auto", host_engine: str = "") -> Tuple[Optional[Engine], str]:
    """``(engine, note)``: the host's engine, else the preference (auto / embedded / external), else the first
    available one; the note says why another engine was used ("" when the wish was met)."""

    by_id = {engine.id: engine for engine in engines}
    wanted: List[str]
    if host_engine in by_id:
        wanted = [host_engine]
    elif preference == "external":
        wanted = ["external"]
    elif preference == "embedded":
        wanted = ["frdp", "xfreerdp-embed"]
    else:
        wanted = list(ENGINE_IDS)
    for engine_id in wanted:
        engine = by_id.get(engine_id)
        if engine is not None and engine.available:
            return engine, ""
    for engine_id in ENGINE_IDS:
        engine = by_id.get(engine_id)
        if engine is not None and engine.available:
            first = by_id.get(wanted[0])
            reason = first.reason if first is not None else ""
            return engine, f"Using {engine.label}: {reason}".rstrip(": ")
    return None, "No RDP engine is available."


# ----------------------------------------------------------------- report
INSTALL_HINTS = {
    "debian": "sudo apt install freerdp3-x11   (or freerdp2-x11 on older releases)",
    "ubuntu": "sudo apt install freerdp3-x11   (or freerdp2-x11 on older releases)",
    "fedora": "sudo dnf install freerdp",
    "rhel": "sudo dnf install freerdp",
    "arch": "sudo pacman -S freerdp",
    "opensuse": "sudo zypper install freerdp",
}
FLATPAK_HINT = ("This app runs as a Flatpak: FreeRDP clients installed on the host are not visible inside it. "
                "Install the app natively, or give it a FreeRDP build inside the sandbox.")


def install_hint(platform: Platform) -> str:
    for key, hint in INSTALL_HINTS.items():
        if key in platform.distro.split():
            return hint
    return "Install FreeRDP (the xfreerdp3 or xfreerdp client) with your distribution's package manager."


@dataclass
class EngineReport:
    platform: Platform
    binaries: List[ClientBinary]
    engines: List[Engine]
    chosen: Optional[Engine]
    note: str = ""
    preference: str = "auto"

    @property
    def any_available(self) -> bool:
        return any(engine.available for engine in self.engines)

    def engine(self, engine_id: str) -> Optional[Engine]:
        return next((e for e in self.engines if e.id == engine_id), None)

    def text(self) -> str:
        """Plain-text report for the Diagnostics dialog and its Copy button (no secrets, no user data)."""

        p = self.platform
        lines = ["SSH Manager — Remote Desktop diagnostics",
                 f"Session: {p.session_type or 'unknown'} · GTK backend: {'X11' if p.gdk_x11 else 'Wayland/other'}"
                 + (" · Flatpak" if p.flatpak else ""),
                 f"gtk-frdp: {p.frdp_version or 'not installed'}" + (f" ({p.frdp_problem})" if p.frdp_problem else ""),
                 f"Preference: {self.preference}", "", "FreeRDP clients:"]
        if not self.binaries:
            lines.append("  none found on PATH")
        for b in self.binaries:
            state = f"FreeRDP {b.version_text}" if b.usable else f"unusable ({b.error})"
            channel = b.secret_channel or "none (passwords cannot be passed safely)"
            lines.append(f"  {b.name}: {b.path} — {state}" + (f"; password channel: {channel}" if b.usable else ""))
        lines += ["", "Engines:"]
        for engine in self.engines:
            if engine.available:
                caps = ", ".join(name for name in CAPABILITIES if engine.capabilities.get(name))
                lines.append(f"  {engine.label}: available ({engine.binary.name if engine.binary else 'widget'})")
                lines.append(f"    can: {caps or '-'}")
            else:
                lines.append(f"  {engine.label}: unavailable — {engine.reason}")
        lines.append("")
        lines.append(f"Chosen: {self.chosen.label if self.chosen else 'none'}" + (f" ({self.note})" if self.note else ""))
        if not self.any_available or not any(b.usable for b in self.binaries):
            lines += ["", "Install: " + install_hint(p)]
        if p.flatpak:
            lines += ["", FLATPAK_HINT]
        return "\n".join(lines) + "\n"


def discover(*, preference: str = "auto", override: str = "", path_env: Optional[str] = None,
             run: Runner = _run, platform: Optional[Platform] = None) -> EngineReport:
    """Find clients, inspect them, build the engines and choose one (blocking: run it off the GTK thread)."""

    platform = platform or detect_platform()
    binaries = [inspect_binary(name, path, run) for name, path in find_binaries(path_env, override)]
    engines = build_engines(binaries, platform)
    chosen, note = choose_engine(engines, preference)
    return EngineReport(platform, binaries, engines, chosen, note, preference)


# ------------------------------------------------------------------ cache
_cache_lock = threading.Lock()
_cached: Optional[EngineReport] = None


def cached_report() -> Optional[EngineReport]:
    """The last discovery result (``None`` until one finished); dialogs read it, never block on it."""

    with _cache_lock:
        return _cached


def store_report(report: Optional[EngineReport]) -> None:
    global _cached
    with _cache_lock:
        _cached = report


def discover_async(callback: Callable[[EngineReport], None], **kwargs) -> threading.Thread:
    """:func:`discover` in a daemon thread; the result is cached, then passed to ``callback`` (on that thread)."""

    def work() -> None:
        try:
            report = discover(**kwargs)
        except Exception as exc:  # discovery must never take the app down
            report = EngineReport(Platform(), [], [], None, f"Discovery failed: {type(exc).__name__}")
        store_report(report)
        callback(report)

    thread = threading.Thread(target=work, daemon=True, name="sshm-rdp-discovery")
    thread.start()
    return thread


__all__ = ["discover", "discover_async", "cached_report", "store_report", "EngineReport", "Engine", "ClientBinary", "Platform", "detect_platform", "build_engines",
           "choose_engine", "inspect_binary", "find_binaries", "parse_help_options", "parse_version",
           "install_hint", "CAPABILITIES", "CAPABILITY_LABELS", "ENGINE_IDS", "ENGINE_LABELS"]
