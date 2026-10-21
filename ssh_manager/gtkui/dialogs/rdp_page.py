"""The host dialog's **Remote Desktop** pages (RDP) and their "Test connection" button.

Four pages edit one :class:`RdpSettings` (``host.metadata["rdp"]``):

* **Remote Desktop** — domain, security, certificate policy and the test;
* **Display & Input** — size, colors, scaling, performance preset, keyboard;
* **Devices** — clipboard, shared folders, printers, sound, USB, smart cards;
* **RDP Connection** — timeouts, reconnect, gateway, SSH tunnel, programs and
  advanced options.

Options the chosen engine cannot do are disabled with the reason in the
tooltip (:meth:`RdpPages.apply_engine`). "Test connection" runs the
negotiation probe (:mod:`ssh_manager.rdp.probe`) in a worker thread; it never
sends a credential and saves nothing.
"""

from __future__ import annotations

import os
import threading
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

from ...rdp.engines import CAPABILITY_LABELS, ENGINE_LABELS, Engine  # noqa: E402
from ...rdp.drives import check_folder  # noqa: E402
from ...rdp.probe import ProbeResult, probe, probe_tls  # noqa: E402
from ...rdp.settings import (  # noqa: E402
    KEYBOARD_LAYOUTS, PRESET_TOGGLES, Gateway, RdpSettings, RemoteApp, SharedFolder,
)
from ..i18n import _  # noqa: E402
from ..theme.tokens import Size, Space  # noqa: E402
from ..widgets.forms import FormRow, FormSection, entry  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from .confirm import confirm  # noqa: E402

PAGES = ("rdp", "rdp_display", "rdp_devices", "rdp_connection")
COMMON_SIZES = ((1280, 720), (1366, 768), (1440, 900), (1600, 900), (1920, 1080), (1920, 1200), (2560, 1440),
                (3840, 2160))
COL_NAME, COL_PATH, COL_RO, COL_SENSITIVE = range(4)


def _spin(low: int, high: int, value: int, step: int = 1) -> Gtk.SpinButton:
    spin = Gtk.SpinButton.new_with_range(low, high, step)
    spin.set_digits(0)
    spin.set_value(value)
    spin.set_halign(Gtk.Align.START)
    return spin


def _combo(choices: Sequence[Tuple[str, str]], active: str) -> Gtk.ComboBoxText:
    combo = Gtk.ComboBoxText()
    for choice_id, label in choices:
        combo.append(choice_id, label)
    if not combo.set_active_id(active):
        combo.set_active(0)
    return combo


def _check(label: str, active: bool) -> Gtk.CheckButton:
    check = Gtk.CheckButton.new_with_mnemonic(label)
    check.set_active(active)
    return check


class RdpPages:
    """Widgets of the RDP pages; :meth:`settings` reads them back."""

    def __init__(self, parent: Gtk.Window, settings: RdpSettings, labels: Gtk.SizeGroup, *,
                 ssh_hosts: Sequence[Tuple[str, str]] = (), credentials: Sequence[Tuple[str, str]] = (),
                 target: Callable[[], Tuple[str, int]] = lambda: ("", 3389)) -> None:
        self._parent = parent
        self._base = settings
        self._labels = labels
        self._target = target  # (host name, port) as typed on the General page
        self._closed = False
        self._suppress = False
        self._gated: Dict[str, List[Gtk.Widget]] = {}  # capability → controls it enables
        self._disabled: Set[Gtk.Widget] = set()  # controls the engine cannot use
        self.pages: Dict[str, List[FormSection]] = {page: [] for page in PAGES}
        self.last_test: Optional[Tuple[bool, str]] = None
        self._build_identity(settings)
        self._build_security(settings)
        self._build_display(settings)
        self._build_performance(settings)
        self._build_input(settings)
        self._build_devices(settings)
        self._credentials = list(credentials)  # (id, label) for the gateway's own credential
        self._build_connection(settings, ssh_hosts)
        self._build_programs(settings)
        self._build_advanced(settings)
        self._build_test()

    # ----------------------------------------------------------- helpers
    def _section(self, page: str, title: str, description: str = "") -> FormSection:
        section = FormSection(title, description)
        self.pages[page].append(section)
        return section

    def _row(self, section: FormSection, label: str, control: Gtk.Widget, help_text: str = "",
             capability: str = "") -> FormRow:
        row = section.add(label or " ", control, label_group=self._labels, help_text=help_text)
        if capability:
            self._gated.setdefault(capability, []).append(control)
        return row

    def _widget(self, section: FormSection, widget: Gtk.Widget, capability: str = "") -> Gtk.Widget:
        section.add_widget(widget)
        if capability:
            self._gated.setdefault(capability, []).append(widget)
        return widget

    # ------------------------------------------------------------ pages
    def _build_identity(self, s: RdpSettings) -> None:
        section = self._section("rdp", _("Sign-in"))
        self.domain = entry(s.domain, _("CORP or corp.example.com (optional)"))
        self._row(section, _("_Domain"), self.domain,
                  _("Overrides a DOMAIN\\ prefix of the user name. A user@domain name is sent as it is."))
        self.use_credential_domain = _check(_("Use the domain from a DOMAIN\\user _name"), s.use_credential_domain)
        self._widget(section, self.use_credential_domain)
        self.prompt_for_credentials = _check(_("_Ask for the password every time (ignore saved passwords)"),
                                             s.prompt_for_credentials)
        self._widget(section, self.prompt_for_credentials)

    def _build_security(self, s: RdpSettings) -> None:
        section = self._section("rdp", _("Security"))
        self.security = _combo([("auto", _("Automatic (NLA when the server offers it)")),
                                ("nla", _("Network Level Authentication (NLA)")),
                                ("nla_ext", _("NLA with early user authorization")),
                                ("tls", _("TLS without NLA")),
                                ("rdp", _("Standard RDP security (weak)"))], s.security)
        self._last_security = s.security
        self.security.connect("changed", self._on_security_changed)
        self._row(section, _("_Security"), self.security,
                  _("NLA signs in before the remote desktop starts; standard RDP security is not encrypted with TLS."))
        self.nla_fallback = _check(_("Allow _TLS when NLA fails (automatic mode)"), s.nla_fallback)
        self._widget(section, self.nla_fallback)
        self.tls_level = _combo([("default", _("Default")), ("0", _("0 — accept old servers")),
                                 ("1", "1"), ("2", _("2 — strict"))], s.tls_security_level)
        self._row(section, _("TLS _level"), self.tls_level, _("OpenSSL security level for the TLS connection."))
        self.cert_policy = _combo([("ask", _("Ask the first time, then remember")),
                                   ("tofu", _("Trust the first certificate, refuse a different one")),
                                   ("pinned", _("Only the pinned certificate")),
                                   ("deny", _("Refuse unknown certificates")),
                                   ("ignore", _("Do not check (insecure)"))], s.cert_policy)
        self._last_cert = s.cert_policy
        self.cert_policy.connect("changed", self._on_cert_changed)
        pins = len(s.pinned_sha256)
        self._row(section, _("_Certificate"), self.cert_policy,
                  _("{n} trusted certificate(s) remembered for this host.").format(n=pins) if pins else
                  _("A certificate you trust when connecting is remembered (pinned) for this host."))
        self.admin_session = _check(_("Connect to the _console (admin) session"), s.admin_session)
        self.restricted_admin = _check(_("_Restricted admin mode (no credentials sent to the server)"),
                                       s.restricted_admin)
        self._last_restricted = s.restricted_admin
        self.restricted_admin.connect("toggled", self._on_restricted_toggled)
        self.restricted_admin.set_tooltip_text(_("Your password is not delegated to the server; network resources "
                                                 "then use the computer's identity. Requires an administrator "
                                                 "account and server support."))
        self.remote_guard = _check(_("Windows Defender Remote Credential _Guard"), s.remote_credential_guard)
        self.remote_guard.set_tooltip_text(_("Needs FreeRDP 3 and Kerberos."))
        for check in (self.admin_session, self.restricted_admin):
            self._widget(section, check)
        self._widget(section, self.remote_guard)

    def _build_display(self, s: RdpSettings) -> None:
        section = self._section("rdp_display", _("Display"))
        self.size_mode = _combo([("window", _("Fit the tab and follow its size")),
                                 ("fixed", _("Fixed size")), ("fullscreen", _("Full screen"))], s.size_mode)
        self.size_mode.connect("changed", lambda *_: self._sync_size())
        self._row(section, _("_Size"), self.size_mode)
        size_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.width = _spin(200, 8192, s.width)
        self.height = _spin(200, 8192, s.height)
        set_accessible_name(self.width, _("Width"))
        set_accessible_name(self.height, _("Height"))
        self.common_size = Gtk.ComboBoxText()
        self.common_size.append("", _("Common sizes…"))
        for w, h in COMMON_SIZES:
            self.common_size.append(f"{w}x{h}", f"{w} × {h}")
        self.common_size.set_active_id("")
        self.common_size.connect("changed", self._on_common_size)
        set_accessible_name(self.common_size, _("Common sizes"))
        times = Gtk.Label(label="×")
        for widget in (self.width, times, self.height, self.common_size):
            size_box.pack_start(widget, False, False, 0)
        self._size_row = self._row(section, _("_Resolution"), size_box)
        self._size_row.label.set_mnemonic_widget(self.width)
        self.color_depth = _combo([("32", _("32 bit (best)")), ("24", "24 bit"), ("16", _("16 bit (faster)")),
                                   ("15", "15 bit"), ("8", _("8 bit (256 colors)"))], str(s.color_depth))
        self._row(section, _("_Colors"), self.color_depth)
        self.scale = _combo([("100", "100 %"), ("140", "140 %"), ("180", "180 %")], str(s.scale_percent))
        self._row(section, _("Sc_aling"), self.scale, _("Remote text and icon size (for high-DPI screens)."))
        self.desktop_scale = _spin(100, 500, s.desktop_scale_factor, 25)
        self._row(section, _("_Desktop scale (%)"), self.desktop_scale)
        self.smart_sizing = _check(_("S_mart sizing: scale the remote screen to the window"), s.smart_sizing)
        self._widget(section, self.smart_sizing, "smart_sizing")
        self.multimon = _check(_("Use _all monitors (full screen)"), s.multimon)
        self._widget(section, self.multimon, "multimon")
        self.monitors = entry(", ".join(str(m) for m in s.monitors), _("0, 1 (empty = all)"))
        self._row(section, _("_Monitors"), self.monitors, _("Monitor numbers to use with all monitors."),
                  capability="multimon")
        self._sync_size()

    def _build_performance(self, s: RdpSettings) -> None:
        section = self._section("rdp_display", _("Performance"))
        self.preset = _combo([("auto", _("Automatic (detect the connection)")), ("lan", _("LAN (best quality)")),
                              ("broadband", _("Broadband")), ("wan", _("WAN")), ("modem", _("Slow link")),
                              ("custom", _("Custom"))], s.preset)
        self.preset.connect("changed", self._on_preset)
        self._row(section, _("_Connection"), self.preset, _("A preset sets the options below; you can still change "
                                                             "them."))
        self.gfx = _combo([("auto", _("Automatic")), ("avc444", _("H.264 AVC444 (best)")),
                           ("avc420", _("H.264 AVC420")), ("rfx", "RemoteFX"), ("off", _("Off (legacy drawing)"))],
                          s.gfx)
        self._row(section, _("_Graphics"), self.gfx)
        self.toggles: Dict[str, Gtk.CheckButton] = {}
        for name, label in (("compression", _("C_ompression")), ("wallpaper", _("Desktop _wallpaper")),
                            ("themes", _("_Themes")), ("font_smoothing", _("_Font smoothing")),
                            ("menu_animations", _("Menu _animations")), ("window_drag", _("Show window contents "
                                                                                          "while _dragging")),
                            ("aero", _("Desktop _composition (Aero)"))):
            check = _check(label, getattr(s, name))
            check.connect("toggled", self._on_toggle_changed)
            self.toggles[name] = check
            self._widget(section, check)

    def _build_input(self, s: RdpSettings) -> None:
        section = self._section("rdp_display", _("Keyboard and mouse"))
        self.keyboard = _combo([(lid, _(label)) for lid, label, _klid in KEYBOARD_LAYOUTS], s.keyboard_layout)
        self._row(section, _("_Keyboard layout"), self.keyboard,
                  _("The layout announced to the server; it should match the layout you type with."))
        self.unicode_input = _check(_("Send typed _characters as Unicode"), s.unicode_input)
        self.grab_keyboard = _check(_("_Grab the keyboard while the remote screen has focus"), s.grab_keyboard)
        self.grab_keyboard.set_tooltip_text(_("Alt+Tab and the Windows key reach the remote computer. Right Ctrl "
                                              "gives the keyboard back."))
        self.swap_alt_super = _check(_("Swap _Alt and Windows keys"), s.swap_alt_super)
        self.mouse_motion = _check(_("Send mouse _motion (off saves bandwidth)"), s.mouse_motion)
        self.touch = _check(_("Forward _touch input"), s.touch)
        self._widget(section, self.unicode_input)
        self._widget(section, self.grab_keyboard, "keyboard_grab")
        for check in (self.swap_alt_super, self.mouse_motion, self.touch):
            self._widget(section, check)

    def _build_devices(self, s: RdpSettings) -> None:
        section = self._section("rdp_devices", _("Clipboard and sound"))
        self.clipboard = _combo([("both", _("Both directions")), ("to_remote", _("Only to the remote computer")),
                                 ("from_remote", _("Only from the remote computer")), ("off", _("Off"))], s.clipboard)
        self._row(section, _("C_lipboard"), self.clipboard, capability="clipboard")
        self.audio_out = _combo([("local", _("Play on this computer")), ("remote", _("Play on the remote computer")),
                                 ("off", _("Do not play"))], s.audio_out)
        self._row(section, _("_Sound"), self.audio_out, capability="audio_out")
        self.audio_in = _check(_("Share the _microphone"), s.audio_in)
        self._widget(section, self.audio_in, "audio_in")

        folders = self._section("rdp_devices", _("Shared folders"),
                                _("Folders of this computer the remote session can open. Off by default: the remote "
                                  "computer can read (and unless read-only, change) everything in them."))
        self.drive_store = Gtk.ListStore(str, str, bool, bool)
        for folder in s.drives:
            self.drive_store.append([folder.name, folder.path, folder.read_only, folder.allow_sensitive])
        self.drive_view = Gtk.TreeView(model=self.drive_store)
        set_accessible_name(self.drive_view, _("Shared folders"))
        name_cell = Gtk.CellRendererText()
        name_cell.set_property("editable", True)
        name_cell.connect("edited", self._on_drive_name_edited)
        self.drive_view.append_column(Gtk.TreeViewColumn(_("Name"), name_cell, text=COL_NAME))
        path_column = Gtk.TreeViewColumn(_("Folder"), Gtk.CellRendererText(), text=COL_PATH)
        path_column.set_expand(True)
        self.drive_view.append_column(path_column)
        ro_cell = Gtk.CellRendererToggle()
        ro_cell.connect("toggled", self._on_drive_ro_toggled)
        self.drive_view.append_column(Gtk.TreeViewColumn(_("Read-only"), ro_cell, active=COL_RO))
        scroller = Gtk.ScrolledWindow()
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(3 * Size.TREE_ROW)
        scroller.add(self.drive_view)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.add_drive_button = Gtk.Button.new_with_mnemonic(_("_Add Folder…"))
        self.add_drive_button.connect("clicked", lambda *_: self.add_drive())
        self.remove_drive_button = Gtk.Button.new_with_mnemonic(_("_Remove"))
        self.remove_drive_button.connect("clicked", lambda *_: self.remove_drive())
        buttons.pack_start(self.add_drive_button, False, False, 0)
        buttons.pack_start(self.remove_drive_button, False, False, 0)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        box.pack_start(scroller, True, True, 0)
        box.pack_start(buttons, False, False, 0)
        self._drive_row = self._row(folders, _("_Folders"), box, capability="drives")
        self._drive_row.label.set_mnemonic_widget(self.drive_view)
        self.home_drive = _check(_("Share my whole _home folder"), s.home_drive)
        self._last_home = s.home_drive
        self.home_drive.connect("toggled", self._on_home_toggled)
        self._widget(folders, self.home_drive, "drives")

        devices = self._section("rdp_devices", _("Devices"),
                                _("Printers, USB devices and smart cards of this computer appear on the remote one."))
        self.printers = _check(_("Share _printers"), s.printers)
        self._widget(devices, self.printers, "printers")
        self.smartcard = _check(_("Share s_mart cards"), s.smartcard)
        self.smartcard.set_tooltip_text(_("The remote computer can use your smart card while connected."))
        self._widget(devices, self.smartcard, "smartcard")
        self.usb = entry(", ".join(s.usb), _("id:054c:0268 or auto (advanced)"))
        self._row(devices, _("_USB devices"), self.usb,
                  _("FreeRDP USB selectors, comma-separated. The device is unavailable here while it is shared."),
                  capability="usb")

    def _build_connection(self, s: RdpSettings, ssh_hosts: Sequence[Tuple[str, str]]) -> None:
        section = self._section("rdp_connection", _("Connection"))
        self.connect_timeout = _spin(1, 600, s.connect_timeout_s)
        self._row(section, _("Connect _timeout (s)"), self.connect_timeout)
        self.auto_reconnect = _check(_("_Reconnect automatically when the connection drops"), s.auto_reconnect)
        self._widget(section, self.auto_reconnect)
        self.reconnect_max = _spin(0, 1000, s.reconnect_max)
        self._row(section, _("Reconnect _attempts"), self.reconnect_max, _("0 means unlimited."))
        self.network_autodetect = _check(_("Detect the _network speed"), s.network_autodetect)
        self._widget(section, self.network_autodetect)

        gateway = self._section("rdp_connection", _("RD Gateway"),
                                _("A Remote Desktop Gateway in front of the server (leave empty when not used)."))
        self.gw_host = entry(s.gateway.host, _("gateway.example.com"))
        self._row(gateway, _("Gate_way"), self.gw_host, capability="gateway")
        self.gw_port = _spin(1, 65535, s.gateway.port)
        self._row(gateway, _("Gateway _port"), self.gw_port, capability="gateway")
        self.gw_same = _check(_("Use the _same credentials as the server"), s.gateway.use_same_credentials)
        self.gw_same.connect("toggled", lambda *_: self._sync_gateway())
        self._widget(gateway, self.gw_same, "gateway")
        self.gw_user = entry(s.gateway.user, _("user or DOMAIN\\user"))
        self._row(gateway, _("Gateway _user"), self.gw_user, capability="gateway")
        self.gw_domain = entry(s.gateway.domain)
        self._row(gateway, _("Gateway do_main"), self.gw_domain, capability="gateway")
        self.gw_credential = Gtk.ComboBoxText()
        self.gw_credential.append("", _("Ask when connecting"))
        for cred_id, label in self._credentials:
            self.gw_credential.append(cred_id, label)
        if not self.gw_credential.set_active_id(s.gateway.credential_id or ""):
            self.gw_credential.set_active_id("")
        self._row(gateway, _("Gateway _password"), self.gw_credential,
                  _("A saved credential's password; it reaches FreeRDP only through its stdin pipe."),
                  capability="gateway")
        self.gw_transport = _combo([("auto", _("Automatic")), ("http", "HTTP"), ("rpc", "RPC")], s.gateway.transport)
        self._row(gateway, _("_Transport"), self.gw_transport, capability="gateway")
        self._sync_gateway()

        tunnel = self._section("rdp_connection", _("SSH tunnel"),
                               _("Reach the server through one of your SSH hosts (port forwarding)."))
        self.via_ssh = Gtk.ComboBoxText()
        self.via_ssh.append("", _("Connect directly"))
        for host_id, label in ssh_hosts:
            self.via_ssh.append(host_id, label)
        if s.via_ssh and not self.via_ssh.set_active_id(s.via_ssh):
            self.via_ssh.append(s.via_ssh, _("(a host that no longer exists)"))
            self.via_ssh.set_active_id(s.via_ssh)
        elif not s.via_ssh:
            self.via_ssh.set_active_id("")
        self._row(tunnel, _("Th_rough"), self.via_ssh)

    def _build_programs(self, s: RdpSettings) -> None:
        section = self._section("rdp_connection", _("Programs"))
        self.initial_program = entry(s.initial_program, _("C:\\Tools\\app.exe (optional)"))
        self._row(section, _("Start _program"), self.initial_program,
                  _("Runs instead of the desktop; the session ends when it exits."))
        self.working_dir = entry(s.working_dir, _("C:\\Work"))
        self._row(section, _("_Working folder"), self.working_dir)
        self.app_program = entry(s.remote_app.program, _("||notepad or C:\\Windows\\notepad.exe"))
        self._row(section, _("Remote_App"), self.app_program,
                  _("Shows one remote program in its own window instead of a desktop."), capability="remoteapp")
        self.app_name = entry(s.remote_app.name, _("Notepad"))
        self._row(section, _("RemoteApp _name"), self.app_name, capability="remoteapp")
        self.app_args = entry(s.remote_app.args)
        self._row(section, _("RemoteApp ar_guments"), self.app_args, capability="remoteapp")
        self.load_balance = entry(s.load_balance_info, _("tsv://MS Terminal Services Plugin.1.Pool"))
        self._row(section, _("_Load balancing"), self.load_balance, _("Load-balance information for a broker."))

    def _build_advanced(self, s: RdpSettings) -> None:
        section = self._section("rdp_connection", _("Advanced"))
        self.engine = _combo([("", _("As in Preferences"))] + [(eid, _(label)) for eid, label in ENGINE_LABELS.items()],
                             s.engine)
        self._row(section, _("_Engine"), self.engine, _("Which FreeRDP integration this host uses."))
        self.vm_id = entry(s.hyperv_vm_id, "1f3e7f8c-0d4b-4b63-9e57-2f0a5d6b8c11")
        self._row(section, _("Hyper-V _VM id"), self.vm_id,
                  _("Connects to a virtual machine's console through Hyper-V (set the port to 2179)."))
        self.extra_args = entry(s.extra_args, "+async-update /gdi:hw")
        self._row(section, _("E_xtra options"), self.extra_args,
                  _("FreeRDP options from a safe list. Credentials, the server and certificate options can never be "
                    "set here."))
        self.extra_unchecked = _check(_("Allow _any option (I know what I am doing)"), s.extra_args_unchecked)
        self._last_unchecked = s.extra_args_unchecked
        self.extra_unchecked.connect("toggled", self._on_unchecked_toggled)
        self._widget(section, self.extra_unchecked)

    def _build_test(self) -> None:
        self.test_button = Gtk.Button.new_with_mnemonic(_("_Test Connection"))
        self.test_button.set_tooltip_text(_("Checks that the server answers, which security it uses and its "
                                            "certificate. No password is sent."))
        self.test_button.connect("clicked", lambda *_: self.test_connection())
        self.test_spinner = Gtk.Spinner()
        self.test_result = Gtk.Label()
        self.test_result.set_xalign(0.0)
        self.test_result.set_line_wrap(True)
        self.test_result.set_selectable(True)
        self.test_result.get_style_context().add_class("form-help")
        self.test_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        row.pack_start(self.test_button, False, False, 0)
        row.pack_start(self.test_spinner, False, False, 0)
        self.test_box.pack_start(row, False, False, 0)
        self.test_box.pack_start(self.test_result, False, False, 0)
        self.engine_note = Gtk.Label()
        self.engine_note.set_xalign(0.0)
        self.engine_note.set_line_wrap(True)
        self.engine_note.get_style_context().add_class("form-help")

    def append_to(self, layout, page_id: str, title: str, description: str):
        """Create one of the :data:`PAGES` in ``layout`` and add its sections."""

        page = layout.new_page(page_id, title, description)
        if page_id == "rdp":
            page.append(self.engine_note)
        for section in self.pages[page_id]:
            page.append(section)
        if page_id == "rdp":
            page.append(self.test_box)
        return page

    # ------------------------------------------------------------ events
    def _sync_size(self) -> None:
        fixed = self.size_mode.get_active_id() == "fixed"
        for widget in (self.width, self.height, self.common_size):
            widget.set_sensitive(fixed)

    def _on_common_size(self, combo: Gtk.ComboBoxText) -> None:
        value = combo.get_active_id() or ""
        if "x" in value:
            w, h = value.split("x")
            self.width.set_value(int(w))
            self.height.set_value(int(h))
            combo.set_active_id("")

    def _on_preset(self, combo: Gtk.ComboBoxText) -> None:
        toggles = PRESET_TOGGLES.get(combo.get_active_id() or "")
        if not toggles:
            return
        self._suppress = True
        for name, value in toggles.items():
            self.toggles[name].set_active(value)
        self._suppress = False

    def _on_toggle_changed(self, _check: Gtk.CheckButton) -> None:
        """A toggle that no longer matches the preset makes it "Custom" (the preset stays visible otherwise)."""

        if self._suppress:
            return
        toggles = PRESET_TOGGLES.get(self.preset.get_active_id() or "")
        if toggles and any(self.toggles[name].get_active() != value for name, value in toggles.items()):
            self.preset.set_active_id("custom")

    def _on_security_changed(self, combo: Gtk.ComboBoxText) -> None:
        if self._suppress:
            return
        choice = combo.get_active_id() or "auto"
        if choice == "rdp" and self._last_security != "rdp" and not confirm(
            self._parent,
            _("Use standard RDP security?"),
            _("Without TLS the connection uses old RDP encryption that can be broken, and the server's identity is "
              "not checked. Use it only for old servers on a trusted network."),
            confirm_label=_("_Use Standard Security"),
            destructive=True,
        ):
            self._revert(combo, self._last_security)
            return
        self._last_security = choice

    def _on_cert_changed(self, combo: Gtk.ComboBoxText) -> None:
        if self._suppress:
            return
        choice = combo.get_active_id() or "ask"
        if choice == "ignore" and self._last_cert != "ignore" and not confirm(
            self._parent,
            _("Stop checking the server's certificate?"),
            _("Anyone between you and the server could pretend to be it and read your password. The tab shows "
              "“Certificate not verified” while connected, and you are asked again every time you connect."),
            confirm_label=_("_Stop Checking"),
            destructive=True,
        ):
            self._revert(combo, self._last_cert)
            return
        self._last_cert = choice

    def _on_home_toggled(self, check: Gtk.CheckButton) -> None:
        if self._suppress:
            return
        if check.get_active() and not self._last_home and not confirm(
            self._parent,
            _("Share your whole home folder?"),
            _("The remote computer can read every file in your home folder, including saved keys and passwords of "
              "other programs, and change them. Share a single folder instead when you can."),
            confirm_label=_("_Share Home Folder"),
            destructive=True,
        ):
            self._suppress = True
            check.set_active(False)
            self._suppress = False
            return
        self._last_home = check.get_active()

    def _on_restricted_toggled(self, check: Gtk.CheckButton) -> None:
        if self._suppress:
            return
        if check.get_active() and not self._last_restricted and not confirm(
            self._parent,
            _("Use restricted admin mode?"),
            _("Your password is not sent to the server, but the session then acts as the computer, not as you, "
              "on the network (no access to your shares), and only administrators can connect. Someone who "
              "controls the server can still use the session's rights while you are connected."),
            confirm_label=_("_Use Restricted Admin Mode"),
        ):
            self._suppress = True
            check.set_active(False)
            self._suppress = False
            return
        self._last_restricted = check.get_active()

    def _on_unchecked_toggled(self, check: Gtk.CheckButton) -> None:
        if self._suppress:
            return
        if check.get_active() and not self._last_unchecked and not confirm(
            self._parent,
            _("Allow any FreeRDP option?"),
            _("Unchecked options can change how the connection is secured or which local files and devices the "
              "remote computer reaches. Credentials, the server and certificate options stay blocked."),
            confirm_label=_("_Allow Any Option"),
            destructive=True,
        ):
            self._suppress = True
            check.set_active(False)
            self._suppress = False
            return
        self._last_unchecked = check.get_active()

    def _revert(self, combo: Gtk.ComboBoxText, value: str) -> None:
        self._suppress = True
        combo.set_active_id(value)
        self._suppress = False

    def _sync_gateway(self) -> None:
        own = not self.gw_same.get_active()
        for widget in (self.gw_user, self.gw_domain, self.gw_credential):
            widget.set_sensitive(own and widget not in self._disabled)

    # ------------------------------------------------------------ drives
    def add_drive(self, path: Optional[str] = None) -> None:
        if path is None:
            chooser = Gtk.FileChooserNative.new(_("Share a Folder"), self._parent, Gtk.FileChooserAction.SELECT_FOLDER,
                                                _("_Share"), _("_Cancel"))
            try:
                if chooser.run() != Gtk.ResponseType.ACCEPT:
                    return
                path = chooser.get_filename()
            finally:
                chooser.destroy()
        if not path:
            return
        sensitive = False
        check = check_folder(path)
        if check.sensitive:
            if not confirm(
                self._parent,
                _("Share {place}?").format(place=check.sensitive),
                _("This folder holds private or system files ({place}): keys, passwords, configuration. The "
                  "remote computer — and anyone controlling it — can read them and, unless read-only, change "
                  "them.").format(place=check.sensitive),
                confirm_label=_("_Share It Anyway"),
                destructive=True,
            ):
                return
            sensitive = True
        taken = {row[COL_NAME].casefold() for row in self.drive_store}
        base = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in os.path.basename(path.rstrip("/")))[:24]
        name, counter = base or "share", 2
        while name.casefold() in taken:
            name, counter = f"{base or 'share'}{counter}", counter + 1
        self.drive_store.append([name, path, False, sensitive])

    def remove_drive(self) -> None:
        model, it = self.drive_view.get_selection().get_selected()
        if it is not None:
            model.remove(it)

    def _on_drive_name_edited(self, _cell, path: str, text: str) -> None:
        self.drive_store[path][COL_NAME] = text.strip()

    def _on_drive_ro_toggled(self, _cell, path: str) -> None:
        self.drive_store[path][COL_RO] = not self.drive_store[path][COL_RO]

    # ------------------------------------------------------------ engine
    def apply_engine(self, engine: Optional[Engine], note: str = "") -> None:
        """Disable what the engine cannot do; the tooltip says why. ``None``: no engine known (all enabled)."""

        self._disabled = set()
        for capability, widgets in self._gated.items():
            reason = "" if engine is None or engine.supports(capability) else \
                _("{feature} is not available with {engine}.").format(
                    feature=_(CAPABILITY_LABELS.get(capability, capability)), engine=_(engine.label))
            for widget in widgets:
                widget.set_sensitive(not reason)
                if reason:
                    self._disabled.add(widget)
                    widget.set_tooltip_text(reason)
        self._sync_gateway()
        self._sync_size()
        if engine is None:
            text = note or _("No RDP engine was detected yet; all options are shown. See Preferences → Remote "
                             "Desktop → Diagnostics.")
        else:
            text = _("Engine: {engine}.").format(engine=_(engine.label)) + (f" {note}" if note else "")
        self.engine_note.set_text(text)

    # ------------------------------------------------------------ values
    def settings(self) -> RdpSettings:
        base = self._base
        toggles = {name: check.get_active() for name, check in self.toggles.items()}
        monitors = [int(part) for part in self.monitors.get_text().replace(" ", "").split(",") if part.isdigit()]
        drives = [SharedFolder(row[COL_NAME], row[COL_PATH], bool(row[COL_RO]), bool(row[COL_SENSITIVE]))
                  for row in self.drive_store]
        return RdpSettings(
            domain=self.domain.get_text().strip(),
            use_credential_domain=self.use_credential_domain.get_active(),
            security=self.security.get_active_id() or "auto",
            nla_fallback=self.nla_fallback.get_active(),
            tls_security_level=self.tls_level.get_active_id() or "default",
            cert_policy=self.cert_policy.get_active_id() or "ask",
            pinned_sha256=list(base.pinned_sha256),
            restricted_admin=self.restricted_admin.get_active(),
            remote_credential_guard=self.remote_guard.get_active(),
            admin_session=self.admin_session.get_active(),
            prompt_for_credentials=self.prompt_for_credentials.get_active(),
            size_mode=self.size_mode.get_active_id() or "window",
            width=int(self.width.get_value()),
            height=int(self.height.get_value()),
            color_depth=int(self.color_depth.get_active_id() or 32),
            scale_percent=int(self.scale.get_active_id() or 100),
            smart_sizing=self.smart_sizing.get_active(),
            multimon=self.multimon.get_active(),
            monitors=monitors,
            desktop_scale_factor=int(self.desktop_scale.get_value()),
            device_scale_factor=base.device_scale_factor,
            refresh_hz=base.refresh_hz,
            preset=self.preset.get_active_id() or "auto",
            gfx=self.gfx.get_active_id() or "auto",
            keyboard_layout=self.keyboard.get_active_id() or "auto",
            keyboard_type=base.keyboard_type,
            unicode_input=self.unicode_input.get_active(),
            grab_keyboard=self.grab_keyboard.get_active(),
            swap_alt_super=self.swap_alt_super.get_active(),
            mouse_motion=self.mouse_motion.get_active(),
            touch=self.touch.get_active(),
            clipboard=self.clipboard.get_active_id() or "both",
            drives=drives,
            home_drive=self.home_drive.get_active(),
            printers=self.printers.get_active(),
            audio_out=self.audio_out.get_active_id() or "local",
            audio_in=self.audio_in.get_active(),
            usb=[part.strip() for part in self.usb.get_text().split(",") if part.strip()],
            smartcard=self.smartcard.get_active(),
            connect_timeout_s=int(self.connect_timeout.get_value()),
            auto_reconnect=self.auto_reconnect.get_active(),
            reconnect_max=int(self.reconnect_max.get_value()),
            network_autodetect=self.network_autodetect.get_active(),
            gateway=Gateway(self.gw_host.get_text().strip(), int(self.gw_port.get_value()),
                            self.gw_user.get_text().strip(), self.gw_domain.get_text().strip(),
                            self.gw_same.get_active(), self.gw_transport.get_active_id() or "auto",
                            self.gw_credential.get_active_id() or ""),
            load_balance_info=self.load_balance.get_text().strip(),
            via_ssh=self.via_ssh.get_active_id() or "",
            initial_program=self.initial_program.get_text().strip(),
            working_dir=self.working_dir.get_text().strip(),
            remote_app=RemoteApp(self.app_program.get_text().strip(), self.app_name.get_text().strip(),
                                 self.app_args.get_text().strip()),
            hyperv_vm_id=self.vm_id.get_text().strip(),
            extra_args=self.extra_args.get_text().strip(),
            extra_args_unchecked=self.extra_unchecked.get_active(),
            engine=self.engine.get_active_id() or "",
            extra=dict(base.extra),
            **toggles,
        )

    def problems(self) -> List[str]:
        problems = self.settings().problems()
        if self.monitors.get_text().strip() and any(
                not part.strip().isdigit() for part in self.monitors.get_text().split(",")):
            problems.append("Monitors must be numbers separated by commas.")
        return problems

    # --------------------------------------------------- test connection
    def test_connection(self) -> None:
        host, port = self._target()
        if not host:
            self._show_test(False, _("Enter the host name on the General page first."))
            return
        settings = self.settings()
        self.test_button.set_sensitive(False)
        self.test_spinner.start()
        self.test_result.set_text(_("Connecting…"))

        def work() -> None:
            try:
                if settings.gateway.enabled:  # the server is usually only reachable through the gateway
                    result = probe_tls(settings.gateway.host, settings.gateway.port,
                                       timeout=float(settings.connect_timeout_s))
                    result.notes.append("The server behind the RD Gateway is checked when connecting.")
                else:
                    result = probe(host, port, timeout=float(settings.connect_timeout_s),
                                   pinned=tuple(settings.pinned_sha256), configured_security=settings.security)
                if settings.via_ssh and not settings.gateway.enabled:
                    result.notes.append("Tested directly; through the SSH tunnel it is tested when connecting.")
                text, ok = self._describe(result), result.reachable and not result.error and not result.failure
            except Exception as exc:  # never let a worker die silently
                text, ok = _("The test failed: {error}").format(error=type(exc).__name__), False
            GLib.idle_add(self._show_test, ok, text)

        threading.Thread(target=work, daemon=True, name="sshm-rdp-test").start()

    @staticmethod
    def _describe(result: ProbeResult) -> str:
        return "\n".join(_(line) if not line.startswith(("Server:", "Reachable", "SHA-256", "Valid:", "Issuer:",
                                                         "Certificate subject:", "TLS:", "Security:", "Refused:"))
                         else line for line in result.lines())

    def _show_test(self, ok: bool, text: str) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        self.test_spinner.stop()
        self.test_button.set_sensitive(True)
        self.test_result.set_text(text)  # plain text: the server's data is never markup
        context = self.test_result.get_style_context()
        context.remove_class("form-error" if ok else "form-help")
        context.add_class("form-help" if ok else "form-error")
        self.last_test = (ok, text)
        return GLib.SOURCE_REMOVE

    def close(self) -> None:
        self._closed = True
