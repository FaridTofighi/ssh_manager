"""Create or edit a host entry.

Pages: General, Connection, Credentials, Tags & Notes, SSH Options and
Permissions, built on :class:`SettingsPageLayout`. Fields are validated inline
(alias, host name, port and ``key=value`` option lines); on Save the dialog
jumps to the first page with an error instead of closing.

``ProxyCommand`` stays in the SSH options list instead of being shown in the
jump-host field, so editing a host no longer rewrites a ProxyCommand into a
ProxyJump.

The first field is the **Protocol** (SSH, FTP, FTPS, SFTP, RDP — the registry in
:mod:`ssh_manager.protocols`). It swaps the protocol-specific pages (keys and
jump hosts, SSH options; file transfer; the Remote Desktop pages), shows the protocol's default port as the port
placeholder, and asks before a switch on an existing host would drop settings
that only the old protocol uses. Folder inventories are OpenSSH config files:
SSH only.

The Credentials page also chooses where the host's **password** comes from:
the bound credential's password (default) or a password specific to this host
(a private credential, see ``docs/passwords.md``). Nothing is stored here: the
caller applies :meth:`HostDialog.password_choice` after Save; Cancel discards it.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ... import protocols  # noqa: E402
from ...config import HostEntry  # noqa: E402
from ...credentials import Credential, PasswordChoice  # noqa: E402
from ...host_utils import host_option  # noqa: E402
from ...profiles import SessionProfile  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..validation import validate_alias, validate_hostname, validate_port  # noqa: E402
from ..widgets.forms import FormRow, FormSection, entry  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from ..widgets.secret_entry import SecretEntry  # noqa: E402
from ...transfer.settings import TransferSettings, default_port_for  # noqa: E402
from .transfer_page import TransferPage  # noqa: E402
from .rdp_page import PAGES as RDP_PAGES, RdpPages  # noqa: E402
from ...rdp.engines import cached_report, choose_engine  # noqa: E402
from ...rdp.settings import RdpSettings  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402
from .confirm import confirm  # noqa: E402
from .settings_layout import SettingsPageLayout  # noqa: E402

# Options edited through dedicated fields (everything else goes to "SSH options").
DEDICATED_OPTIONS = {"hostname", "user", "port", "identityfile", "proxyjump", "jump_host", "tags"}


def parse_option_lines(text: str) -> Tuple[Dict[str, str], List[str]]:
    """Parse ``key=value`` lines; return ``(options, invalid_lines)``."""

    options: Dict[str, str] = {}
    invalid: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            invalid.append(line)
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or any(ch.isspace() for ch in key):
            invalid.append(line)
            continue
        options[key] = value.strip()
    return options, invalid


def validate_option_lines(text: str) -> Optional[str]:
    _options, invalid = parse_option_lines(text)
    if invalid:
        return _("Use key=value on each line. Check: {line}").format(line=invalid[0])
    return None


def _show(widget: Gtk.Widget, visible: bool) -> None:
    """Show (with its children) or hide a widget that ``show_all`` must leave alone (``no_show_all``)."""

    if visible:
        widget.set_no_show_all(False)
        widget.show_all()
        widget.set_no_show_all(True)
    else:
        widget.hide()


def _combo(rows: List[Tuple[str, str]], active_id: Optional[str]) -> Gtk.ComboBoxText:
    combo = Gtk.ComboBoxText()
    combo.append("", _("None"))
    for row_id, label in rows:
        combo.append(row_id, label)
    if not (active_id and combo.set_active_id(active_id)):
        combo.set_active_id("")
    return combo


class HostDialog(BaseDialog):
    """Dialog for creating or editing a host entry."""

    def __init__(
        self,
        parent: Gtk.Window,
        host: Optional[HostEntry] = None,
        *,
        credentials: Optional[List[Credential]] = None,
        selected_credential_id: Optional[str] = None,
        metadata: Optional[Dict[str, object]] = None,
        template_defaults: Optional[Dict[str, str]] = None,
        profiles: Optional[Dict[str, SessionProfile]] = None,
        alias_validator: Optional[Callable[[str], Optional[str]]] = None,
        private_credential: Optional[Credential] = None,
        private_has_password: Optional[bool] = False,
        password_check: Optional[Callable[[], Optional[str]]] = None,
        can_edit: bool = True,
        protocols_enabled: bool = True,
        transfer_backend_factory: Optional[Callable[[HostEntry, Optional[str]], object]] = None,
        rdp_ssh_hosts: Sequence[Tuple[str, str]] = (),
        rdp_engine_preference: str = "auto",
        forwards_host_id: str = "",
        forwards_others: Sequence[Tuple[str, object]] = (),
    ) -> None:
        title = _("Edit Host") if host else _("New Host")
        self._editing = host is not None
        self._protocols_enabled = protocols_enabled
        self._transfer_backend_factory = transfer_backend_factory  # (host, credential id) -> backend
        self._rdp_ssh_hosts = list(rdp_ssh_hosts)  # (inventory id, label) of SSH hosts an RDP host can tunnel through
        self._rdp_engine_preference = rdp_engine_preference
        self._forwards_host_id = forwards_host_id  # Port Forwarding: this host's key, the inventory's forwards
        self._forwards_others = list(forwards_others)
        self.forwards_page = None  # built when the page is first shown
        self._protocol = protocols.protocol_of(host) if host else protocols.DEFAULT_PROTOCOL
        self._port_edited = False  # the user typed a port in this dialog
        self._suppress_protocol_events = False
        self._alias_validator = alias_validator or validate_alias  # app store: display names
        BaseDialog.__init__(self, parent, title, size=DialogSize.LARGE, height=560)
        self.get_content_area().set_margin_start(0)
        self.get_content_area().set_margin_end(0)
        self.get_content_area().set_margin_top(0)
        self._credential_choices = [c for c in (credentials or []) if not c.private]
        self._private = private_credential  # the host's own password (a private credential), if any
        self._private_has_password = private_has_password  # None: unknown (store locked)
        self._password_check = password_check
        self._can_edit = can_edit
        self._clear_password = False  # "Clear" pressed: applied on Save
        self._changing_password = False  # "Change…" pressed: the entry is shown
        self._suppress_password_events = False
        self._last_credential_id = selected_credential_id or ""
        self._profiles = profiles or {}
        meta = dict(metadata or (host.metadata if host else {}) or {})
        self._metadata = meta
        self._sections: Dict[str, FormSection] = {}

        self._build_fields(meta, selected_credential_id)
        self.layout = SettingsPageLayout()
        self._build_pages()
        self.add_body(self.layout, expand=True)

        if host:
            self._fill_from_host(host)
        elif template_defaults:
            self._fill_from_template(template_defaults)
        self._port_edited = False  # filled in, not typed
        self._apply_identity_source()

        self.add_cancel()
        self.add_primary(_("_Save"))
        self.set_initial_focus(self.alias_entry)
        self.set_response_validator(lambda r: not is_accept(r) or self.validate())
        self.show_all()
        self._update_password_ui()
        self._apply_protocol()

    # ---------------------------------------------------------- protocol
    def selected_protocol(self) -> str:
        return self.protocol_combo.get_active_id() or protocols.DEFAULT_PROTOCOL

    def _on_port_changed(self, _entry: Gtk.Entry) -> None:
        if not self._suppress_protocol_events:
            self._port_edited = True

    def _discarded_by(self, new_protocol: str) -> List[str]:
        """Settings of the current protocol that ``new_protocol`` does not use (names for the dialog)."""

        new = protocols.get(new_protocol)
        lost: List[str] = []
        keeps_ssh = new is not None and (new.supports_keys or new.supports_jump_host)
        if not keeps_ssh:
            if self.identity_entry.get_text().strip():
                lost.append(_("private key"))
            if self.proxy_entry.get_text().strip():
                lost.append(_("jump host"))
            if self._extras_text().strip():
                lost.append(_("SSH options"))
        if (new is None or new.family != "transfer") and self._metadata.get("transfer"):
            lost.append(_("file-transfer settings"))
        if (new is None or new.id != "rdp") and (self._metadata.get("rdp") or self._protocol == "rdp"):
            lost.append(_("remote desktop settings"))
        return lost

    def _on_protocol_changed(self, _combo: Gtk.ComboBoxText) -> None:
        if self._suppress_protocol_events:
            return
        new_protocol = self.selected_protocol()
        lost = self._discarded_by(new_protocol)
        if self._editing and lost and not confirm(
            self,
            _("Change the protocol?"),
            _("These settings are not used by {protocol} and are removed when you save: {items}.").format(
                protocol=self.protocol_combo.get_active_text(), items=", ".join(lost)),
            confirm_label=_("_Change Protocol"),
            destructive=True,
        ):
            self._suppress_protocol_events = True
            self.protocol_combo.set_active_id(self._protocol)
            self._suppress_protocol_events = False
            return
        old_default = str(protocols.default_port(self._protocol))
        self._protocol = new_protocol
        if not self._port_edited and self.port_entry.get_text().strip() == old_default:
            self._suppress_protocol_events = True
            self.port_entry.set_text("")  # the new protocol's default (shown as the placeholder)
            self._suppress_protocol_events = False
        self._apply_protocol()

    def _apply_protocol(self) -> None:
        """Placeholder port, help texts and the pages the protocol uses."""

        kind = protocols.get(self._protocol)
        transfer = kind is not None and kind.family == "transfer"
        page = getattr(self, "transfer_page", None)
        if transfer and page is not None:  # implicit FTPS defaults to 990
            port = default_port_for(self._protocol, TransferSettings(tls_mode=page.tls_mode_id()))
        else:
            port = protocols.default_port(self._protocol)
        self.port_entry.set_placeholder_text(str(port))
        ssh_like = kind is None or kind.supports_keys or kind.supports_jump_host
        self.layout.set_page_visible("connection", ssh_like)
        self.layout.set_page_visible("ssh_options", ssh_like)
        self.layout.set_page_visible("forwarding", self._protocol == "ssh")
        self.layout.set_page_visible("transfer", transfer)
        rdp = self._protocol == "rdp"
        for page_id in RDP_PAGES:
            self.layout.set_page_visible(page_id, rdp)
        if rdp:
            self._apply_rdp_engine()
        if page is not None:
            page.set_protocol(self._protocol)
            anonymous = transfer and page.anonymous_active()
            self._credential_combo.set_sensitive(not anonymous)
        is_ssh = self._protocol == protocols.DEFAULT_PROTOCOL
        self._hostname_row.help.set_text(_("Leave empty to connect to the alias itself.") if is_ssh
                                         else _("Required for {protocol}.").format(
                                             protocol=kind.badge if kind else self._protocol))

    # ------------------------------------------------------------ fields
    def _build_fields(self, meta: Dict[str, object], credential_id: Optional[str]) -> None:
        self.alias_entry = entry(placeholder=_("web-01"))
        self.hostname_entry = entry(placeholder=_("example.com or 10.0.0.5"))
        self.user_entry = entry(placeholder=_("ubuntu"))
        self.port_entry = entry(placeholder="22")
        self.port_entry.set_width_chars(6)
        self.port_entry.connect("changed", self._on_port_changed)
        self.protocol_combo = Gtk.ComboBoxText()
        for kind in protocols.all_kinds():
            self.protocol_combo.append(kind.id, _(kind.label))
        if protocols.get(self._protocol) is None:  # written by a newer version: keep it, do not offer others
            self.protocol_combo.append(self._protocol, self._protocol)
        self.protocol_combo.set_active_id(self._protocol)
        self.protocol_combo.connect("changed", self._on_protocol_changed)
        self.identity_entry = entry(placeholder="~/.ssh/id_ed25519")
        self.proxy_entry = entry(placeholder=_("jump-host or user@jump:2222"))
        self.tags_entry = entry(placeholder=_("production, web, eu"))
        self.environment_entry = entry(str(meta.get("environment", "") or ""), _("prod / staging"))
        self.owner_entry = entry(str(meta.get("owner", "") or ""), _("team or person"))
        self.service_entry = entry(str(meta.get("service", "") or ""), _("service or component"))
        self.notes_entry = entry(str(meta.get("notes", "") or ""), _("Notes"))

        self.extras_view = Gtk.TextView()
        self.extras_view.set_monospace(True)
        self.extras_view.set_wrap_mode(Gtk.WrapMode.NONE)
        self.extras_view.set_left_margin(Space.S)
        self.extras_view.set_top_margin(Space.XS)

        self._credential_combo = _combo([(c.id, f"{c.name} ({c.username})") for c in self._credential_choices], credential_id)
        self._credential_combo.connect("changed", self._on_credential_changed)
        self._build_password_fields()
        self._profile_combo = _combo([(pid, prof.name) for pid, prof in self._profiles.items()],
                                     str(meta.get("default_session_profile_id", "") or ""))

        perms = meta.get("permissions", {})
        perms = perms if isinstance(perms, dict) else {}
        self._perm_view_check = Gtk.CheckButton.new_with_mnemonic(_("Allow _viewing details"))
        self._perm_connect_check = Gtk.CheckButton.new_with_mnemonic(_("Allow _connecting"))
        self._perm_edit_check = Gtk.CheckButton.new_with_mnemonic(_("Allow _editing"))
        self._perm_view_check.set_active(bool(perms.get("can_view", True)))
        self._perm_connect_check.set_active(bool(perms.get("can_connect", True)))
        self._perm_edit_check.set_active(bool(perms.get("can_edit", True)))

    def _build_pages(self) -> None:
        labels = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        for build in (self._page_general, self._page_connection, self._page_transfer, self._page_rdp,
                      self._page_credentials, self._page_tags, self._page_ssh_options, self._page_forwarding,
                      self._page_permissions):
            build(labels)

    def _page_transfer(self, labels: Gtk.SizeGroup) -> None:
        page = self.layout.new_page("transfer", _("File transfer"),
                                    _("FTP, FTPS and SFTP: security, connection, folders and transfers."))
        settings, warnings = TransferSettings.from_json(self._metadata.get("transfer"))
        self._transfer_error = Gtk.Label(label="\n".join(_(w) for w in warnings))
        self._transfer_error.set_xalign(0.0)
        self._transfer_error.set_line_wrap(True)
        self._transfer_error.get_style_context().add_class("form-error")
        self._transfer_error.set_no_show_all(not warnings)
        page.append(self._transfer_error)
        self.transfer_page = TransferPage(self, settings, labels, on_default_port=self._apply_protocol,
                                          on_anonymous=self._on_anonymous)
        self.transfer_page.append_to(page)
        self.transfer_page.test_button.connect("clicked", lambda *_: self._test_connection())
        self.connect("destroy", lambda *_: self.transfer_page.close())

    def _page_rdp(self, labels: Gtk.SizeGroup) -> None:
        settings, warnings = RdpSettings.from_json(self._metadata.get("rdp"))
        self.rdp_pages = RdpPages(self, settings, labels, ssh_hosts=self._rdp_ssh_hosts, target=self._rdp_target,
                                  credentials=[(c.id, f"{c.name} ({c.username})") for c in self._credential_choices])
        self._rdp_error = Gtk.Label(label="\n".join(_(w) for w in warnings))
        self._rdp_error.set_xalign(0.0)
        self._rdp_error.set_line_wrap(True)
        self._rdp_error.get_style_context().add_class("form-error")
        self._rdp_error.set_no_show_all(not warnings)
        first = self.rdp_pages.append_to(self.layout, "rdp", _("Remote Desktop"),
                                         _("RDP sign-in, security and the server's certificate."))
        first.append(self._rdp_error)
        self.rdp_pages.append_to(self.layout, "rdp_display", _("Display & Input"),
                                 _("Screen size, colors, performance and keyboard."))
        self.rdp_pages.append_to(self.layout, "rdp_devices", _("Devices"),
                                 _("What the remote computer may use from this one. Everything but the clipboard is "
                                   "off by default."))
        self.rdp_pages.append_to(self.layout, "rdp_connection", _("RDP Connection"),
                                 _("Reconnecting, RD Gateway, SSH tunnel, programs and advanced options."))
        self.rdp_pages.engine.connect("changed", lambda *_: self._apply_rdp_engine())
        self.connect("destroy", lambda *_: self.rdp_pages.close())

    def _rdp_target(self) -> Tuple[str, int]:
        port = self.port_entry.get_text().strip()
        return self.hostname_entry.get_text().strip(), int(port) if port.isdigit() else 3389

    def _apply_rdp_engine(self) -> None:
        report = cached_report()
        if report is None:
            self.rdp_pages.apply_engine(None)
            return
        engine, note = choose_engine(report.engines, self._rdp_engine_preference,
                                     self.rdp_pages.engine.get_active_id() or "")
        if engine is None:
            self.rdp_pages.apply_engine(None, _("No FreeRDP client was found; the host can be saved, but not opened "
                                                "until FreeRDP is installed (Preferences → Remote Desktop)."))
            return
        self.rdp_pages.apply_engine(engine, _(note) if note else "")

    def _on_anonymous(self, anonymous: bool) -> None:
        self._credential_combo.set_sensitive(not anonymous)
        self._apply_identity_source()

    def _test_connection(self) -> None:
        factory = self._transfer_backend_factory
        if factory is None:
            self.transfer_page.test_result.set_text(_("Testing is not available here."))
            return
        host = self.build_host("", "")
        self.transfer_page.test_connection(lambda: factory(host, self.selected_credential_id())
                                           if host is not None and host.option("hostname") else None)

    def _page_general(self, labels: Gtk.SizeGroup) -> None:
        general = self.layout.new_page("general", _("General"), _("How the host appears in the tree and where it lives."))
        sec = FormSection()
        self._protocol_row = sec.add(
            _("Pro_tocol"), self.protocol_combo, label_group=labels,
            help_text="" if self._protocols_enabled else _("This inventory is an OpenSSH config folder: SSH only."))
        self.protocol_combo.set_sensitive(self._protocols_enabled)
        self._alias_row = sec.add(_("_Alias"), self.alias_entry, validator=self._alias_validator, label_group=labels,
                                  help_text=_("The name used in the tree and with “ssh <alias>”."))
        self._hostname_row = sec.add(_("_Host name"), self.hostname_entry, validator=validate_hostname, label_group=labels,
                                     help_text=_("Leave empty to connect to the alias itself."))
        self._user_row = sec.add(_("_User"), self.user_entry, label_group=labels)
        self._port_row = sec.add(_("_Port"), self.port_entry, validator=validate_port, label_group=labels)
        general.append(sec)
        self._sections["general"] = sec

    def _page_connection(self, labels: Gtk.SizeGroup) -> None:
        connection = self.layout.new_page("connection", _("Connection"), _("Keys and jump hosts."))
        sec = FormSection()
        sec.add(_("Private _key"), self.identity_entry, label_group=labels,
                help_text=_("Path of the identity file (IdentityFile)."))
        sec.add(_("_Jump host"), self.proxy_entry, label_group=labels,
                help_text=_("ProxyJump target (-J). Use the SSH options page for ProxyCommand."))
        connection.append(sec)
        self._sections["connection"] = sec

    def _page_credentials(self, labels: Gtk.SizeGroup) -> None:
        creds = self.layout.new_page("credentials", _("Credentials"),
                                     _("Reuse a saved credential; its username replaces the User field."))
        sec = FormSection()
        sec.add(_("_Credential"), self._credential_combo, label_group=labels)
        sec.add(_("Session _profile"), self._profile_combo, label_group=labels)
        creds.append(sec)
        self._sections["credentials"] = sec

        pw = FormSection()
        choices = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        for widget in (self._pw_shared_radio, self._pw_shared_hint, self._pw_host_radio, self._pw_secret,
                       self._pw_status_box):
            choices.pack_start(widget, False, False, 0)
        self._pw_row = pw.add(_("Password"), choices, label_group=labels,
                              help_text=_("Where the password for this host comes from. It is never shown or exported."))
        self._pw_row.label.set_mnemonic_widget(self._pw_shared_radio)
        set_accessible_name(choices, _("Password"))
        set_accessible_name(self._pw_secret.entry, _("Host password"))
        creds.append(pw)
        self._update_password_ui()

    # ---------------------------------------------------------- password
    def _build_password_fields(self) -> None:
        self._pw_shared_radio = Gtk.RadioButton.new_with_mnemonic_from_widget(None, _("Use the _credential’s password"))
        self._pw_host_radio = Gtk.RadioButton.new_with_mnemonic_from_widget(
            self._pw_shared_radio, _("Use a password _specific to this host"))
        self._pw_shared_hint = Gtk.Label()
        self._pw_shared_hint.set_xalign(0.0)
        self._pw_shared_hint.set_line_wrap(True)
        self._pw_shared_hint.get_style_context().add_class("form-help")
        self._pw_shared_hint.set_no_show_all(True)
        self._pw_secret = SecretEntry(_("Password for this host"))
        self._pw_secret.set_margin_start(Space.XL)  # belongs to the radio above it
        self._pw_secret.entry.connect("changed", lambda *_: self._pw_row.set_error(None))
        self._pw_status = Gtk.Label()
        self._pw_status.set_xalign(0.0)
        self._pw_status.set_line_wrap(True)
        self._pw_status.set_hexpand(True)
        self._pw_status.get_style_context().add_class("form-help")
        self._pw_change_button = Gtk.Button.new_with_mnemonic(_("C_hange…"))
        self._pw_change_button.set_tooltip_text(_("Type a new password for this host"))
        self._pw_change_button.connect("clicked", self._on_change_password)
        self._pw_clear_button = Gtk.Button.new_with_mnemonic(_("C_lear"))
        self._pw_clear_button.set_tooltip_text(_("Remove the saved password; you will be asked when you connect"))
        self._pw_clear_button.connect("clicked", self._on_clear_password)
        self._pw_status_box: Gtk.Box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self._pw_status_box.set_margin_start(Space.XL)
        self._pw_status_box.pack_start(self._pw_status, True, True, 0)
        self._pw_status_box.pack_start(self._pw_change_button, False, False, 0)
        self._pw_status_box.pack_start(self._pw_clear_button, False, False, 0)
        for widget in (self._pw_change_button, self._pw_clear_button, self._pw_secret, self._pw_status_box):
            widget.set_no_show_all(True)  # shown by _update_password_ui
        if self._private is not None:
            self._pw_host_radio.set_active(True)
        self._pw_host_radio.connect("toggled", self._on_password_mode_toggled)
        if not self._can_edit:
            tip = _("Editing is disabled for this host by its local permissions.")
            for widget in (self._pw_shared_radio, self._pw_host_radio, self._pw_secret, self._pw_status_box):
                widget.set_sensitive(False)
                widget.set_tooltip_text(tip)

    def _selected_credential(self) -> Optional[Credential]:
        return next((c for c in self._credential_choices if c.id == self.selected_credential_id()), None)

    def host_specific(self) -> bool:
        return self._pw_host_radio.get_active()

    def _has_saved_password(self) -> bool:
        return self._private is not None and self._private_has_password is not False and not self._clear_password

    def _update_password_ui(self) -> None:
        cred = self._selected_credential()
        host_mode = self.host_specific()
        if cred:
            self._pw_shared_radio.set_label(_("Use the _credential’s password (‘{name}’)").format(name=cred.name))
        else:
            self._pw_shared_radio.set_label(_("Use the _credential’s password"))
        self._pw_shared_radio.set_use_underline(True)
        missing = self._private is not None and self._private.based_on and cred is None
        hint = ""
        if missing:
            hint = _("The credential this host used before no longer exists; without a host password it has none.")
        elif cred is None:
            hint = _("Choose a credential above to use its password.")
        self._pw_shared_hint.set_text(hint)
        self._pw_shared_hint.set_visible(bool(hint))
        if self._can_edit:
            self._pw_shared_radio.set_sensitive(cred is not None or host_mode)

        saved = self._has_saved_password()
        show_entry = host_mode and (not saved or self._changing_password)
        _show(self._pw_secret, show_entry)
        _show(self._pw_status_box, host_mode)
        self._pw_change_button.set_visible(host_mode and saved and not self._changing_password)
        self._pw_clear_button.set_visible(host_mode and saved)
        if not host_mode:
            self._pw_row.set_error(None)
            return
        if saved and self._private_has_password is None:
            status = _("Saved password unavailable while the password store is locked.")
        elif saved and self._changing_password:
            status = _("Saved — type a new password to replace it, or leave it empty to keep it.")
        elif saved:
            status = _("Saved")
        else:
            status = _("Not set — you will be asked when you connect")
        self._pw_status.set_text(status)

    def _on_password_mode_toggled(self, _radio: Gtk.RadioButton) -> None:
        if self._suppress_password_events:
            return
        if not self.host_specific() and self._private is not None:
            if not confirm(
                self,
                _("Use the credential’s password?"),
                _("The saved password for this host will be deleted when you save."),
                confirm_label=_("_Use Credential’s Password"),
                destructive=True,
            ):
                self._set_host_mode(True)
                return
        self._update_password_ui()

    def _on_credential_changed(self, _combo: Gtk.ComboBoxText) -> None:
        if self._suppress_password_events:
            return
        new_id = self._credential_combo.get_active_id() or ""
        if self.host_specific() and self._private is not None and new_id != self._last_credential_id:
            cred = self._selected_credential()
            if not confirm(
                self,
                _("Change the credential?"),
                (_("The saved password for this host will be deleted and it will use the password of ‘{name}’.")
                 .format(name=cred.name) if cred else
                 _("The saved password for this host will be deleted and it will have no credential.")),
                confirm_label=_("_Change Credential"),
                destructive=True,
            ):
                self._suppress_password_events = True
                self._credential_combo.set_active_id(self._last_credential_id)
                self._suppress_password_events = False
                return
            self._set_host_mode(False)
        self._last_credential_id = new_id
        self._apply_identity_source()
        self._update_password_ui()

    def _set_host_mode(self, host_mode: bool) -> None:
        self._suppress_password_events = True
        (self._pw_host_radio if host_mode else self._pw_shared_radio).set_active(True)
        self._suppress_password_events = False
        self._update_password_ui()

    def _on_change_password(self, _button: Gtk.Button) -> None:
        self._changing_password = True
        self._update_password_ui()
        self._pw_secret.entry.grab_focus()

    def _on_clear_password(self, _button: Gtk.Button) -> None:
        self._clear_password = True
        self._changing_password = False
        self._pw_secret.entry.set_text("")
        self._update_password_ui()

    def password_choice(self) -> PasswordChoice:
        """What to do with the host's password on Save (nothing is stored by the dialog)."""

        if not self.host_specific():
            return PasswordChoice(host_specific=False)
        password = self._pw_secret.entry.get_text() or None
        return PasswordChoice(host_specific=True, password=password, clear=self._clear_password and not password)

    def _validate_password(self) -> bool:
        choice = self.password_choice()
        if not (choice.host_specific and (choice.password or choice.clear)) or self._password_check is None:
            return True
        problem = self._password_check()
        self._pw_row.set_error(problem)
        return problem is None

    def _page_tags(self, labels: Gtk.SizeGroup) -> None:
        tags = self.layout.new_page("tags", _("Tags & Notes"), _("Used by search, filters and smart groups."))
        sec = FormSection()
        sec.add(_("_Tags"), self.tags_entry, label_group=labels, help_text=_("Comma-separated."))
        sec.add(_("_Environment"), self.environment_entry, label_group=labels)
        sec.add(_("_Owner"), self.owner_entry, label_group=labels)
        sec.add(_("_Service"), self.service_entry, label_group=labels)
        sec.add(_("_Notes"), self.notes_entry, label_group=labels)
        tags.append(sec)
        self._sections["tags"] = sec

    def _page_ssh_options(self, labels: Gtk.SizeGroup) -> None:
        options = self.layout.new_page(
            "ssh_options",
            _("SSH Options"),
            _("Any other ssh_config option, one key=value per line — for example ProxyCommand, "
              "ServerAliveInterval, LocalForward, RemoteForward or DynamicForward."),
        )
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.set_min_content_height(220)
        scroller.add(self.extras_view)
        options.append(scroller, expand=True)
        self._extras_row = FormRow(
            _("SSH options"),
            scroller,
            validator=validate_option_lines,
            value_getter=self._extras_text,
        )
        options.append(self._extras_row.error)
        self.extras_view.get_buffer().connect("changed", lambda *_: self._extras_row.validate())

    def _page_forwarding(self, labels: Gtk.SizeGroup) -> None:
        self._forwarding_page = self.layout.new_page(
            "forwarding", _("Port Forwarding"),
            _("Local, remote and SOCKS forwards of this host. SSH Manager runs them beside the terminal, so they "
              "can be stopped without disconnecting (see Tunnels…)."))
        self.layout.on_page_shown.append(self._show_forwarding)

    def _show_forwarding(self, page_id: str) -> None:
        if page_id != "forwarding" or self.forwards_page is not None:
            return
        from ...tunnels.model import forwards_from_metadata
        from .forwards_page import ForwardsPage

        self.forwards_page = ForwardsPage(self, forwards_from_metadata(self._metadata), host_id=self._forwards_host_id,
                                          others=self._forwards_others, host_label=self.alias_entry.get_text().strip(),
                                          ssh_options=lambda: parse_option_lines(self._extras_text())[0])
        self._forwarding_page.append(self.forwards_page, expand=True)
        self.forwards_page.show_all()

    def _page_permissions(self, labels: Gtk.SizeGroup) -> None:
        perms = self.layout.new_page("permissions", _("Permissions"),
                                     _("Local, advisory restrictions for this host. They are stored as comments in the config file."))
        for check in (self._perm_view_check, self._perm_connect_check, self._perm_edit_check):
            perms.append(check)

    # ---------------------------------------------------------- populate
    def _fill_from_host(self, host: HostEntry) -> None:
        self.alias_entry.set_text(host.alias)
        self.hostname_entry.set_text(host_option(host, "hostname") or "")
        self.user_entry.set_text(host_option(host, "user") or "")
        self.port_entry.set_text(host_option(host, "port") or "")
        self.identity_entry.set_text(host_option(host, "identityfile") or "")
        self.proxy_entry.set_text(host_option(host, "proxyjump") or host_option(host, "jump_host") or "")
        self.tags_entry.set_text(host_option(host, "tags") or "")
        extras = [f"{key}={value}" for key, value in host.options.items() if str(key).lower() not in DEDICATED_OPTIONS]
        self.extras_view.get_buffer().set_text("\n".join(extras))

    def _fill_from_template(self, defaults: Dict[str, str]) -> None:
        for key, widget in (
            ("hostname", self.hostname_entry),
            ("user", self.user_entry),
            ("port", self.port_entry),
            ("identityfile", self.identity_entry),
            ("proxyjump", self.proxy_entry),
            ("tags", self.tags_entry),
            ("environment", self.environment_entry),
            ("owner", self.owner_entry),
            ("service", self.service_entry),
            ("notes", self.notes_entry),
        ):
            widget.set_text(defaults.get(key, ""))

    def set_alias(self, alias: str) -> None:
        self.alias_entry.set_text(alias)

    def _apply_identity_source(self) -> None:
        cred = self._selected_credential()
        source = cred.username if cred else None
        if source is not None:
            self.user_entry.set_text(source)
        self.user_entry.set_sensitive(source is None)

    # -------------------------------------------------------- validation
    def _extras_text(self) -> str:
        buf = self.extras_view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def _validate_transfer(self) -> bool:
        kind = protocols.get(self._protocol)
        if kind is None or kind.family != "transfer":
            return True
        problems = self.transfer_page.problems()
        self._transfer_error.set_text("\n".join(_(p) for p in problems))
        self._transfer_error.set_visible(bool(problems))
        return not problems

    def _validate_rdp(self) -> bool:
        if self._protocol != "rdp":
            return True
        problems = self.rdp_pages.problems()
        self._rdp_error.set_text("\n".join(_(p) for p in problems))
        self._rdp_error.set_visible(bool(problems))
        return not problems

    def validate(self) -> bool:
        """Validate every field; show the first page with an error."""

        general_ok = self._sections["general"].validate()
        if general_ok and self._protocol != protocols.DEFAULT_PROTOCOL and not self.hostname_entry.get_text().strip():
            self._hostname_row.set_error(_("Enter the server's host name or address."))
            general_ok = False
        checks = [
            ("general", general_ok),
            ("ssh_options", self._extras_row.validate()),
            ("credentials", self._validate_password()),
            ("transfer", self._validate_transfer()),
            ("rdp", self._validate_rdp()),
        ]
        for page_id, ok in checks:
            if not ok:
                self.layout.show_page(page_id)
                return False
        return True

    # ------------------------------------------------------------ result
    def build_host(self, parent: str, child: str) -> Optional[HostEntry]:
        alias = self.alias_entry.get_text().strip()
        if not alias:
            return None
        kind = protocols.get(self._protocol)
        ssh_like = kind is None or kind.supports_keys or kind.supports_jump_host
        options: Dict[str, str] = {}
        fields = [("hostname", self.hostname_entry), ("user", self.user_entry), ("port", self.port_entry)]
        if ssh_like:  # FTP / FTPS have no keys, jump hosts or ssh -o options
            fields += [("identityfile", self.identity_entry), ("proxyjump", self.proxy_entry)]
        for key, widget in fields:
            value = widget.get_text().strip()
            if value:
                options[key] = value
        tags = self.tags_entry.get_text().strip()
        if tags:
            options["tags"] = ",".join(part.strip() for part in tags.split(",") if part.strip())
        cred = next((c for c in self._credential_choices if c.id == self.selected_credential_id()), None)
        if cred:
            options["user"] = cred.username
        if ssh_like:
            extra_options, _invalid = parse_option_lines(self._extras_text())
            options.update(extra_options)

        metadata: Dict[str, object] = {
            "environment": self.environment_entry.get_text().strip(),
            "owner": self.owner_entry.get_text().strip(),
            "service": self.service_entry.get_text().strip(),
            "notes": self.notes_entry.get_text().strip(),
        }
        profile_id = self._profile_combo.get_active_id() or ""
        if profile_id:
            metadata["default_session_profile_id"] = profile_id
        perms = {
            "can_view": self._perm_view_check.get_active(),
            "can_connect": self._perm_connect_check.get_active(),
            "can_edit": self._perm_edit_check.get_active(),
        }
        if not all(perms.values()):
            metadata["permissions"] = perms
        if kind is not None and kind.family == "transfer":
            metadata["transfer"] = self.transfer_page.settings().to_json()  # protocol settings, never ssh options
        if self._protocol == "rdp":
            metadata["rdp"] = self.rdp_pages.settings().to_json()
        if self.forwards_page is not None:  # edited on the Port Forwarding page
            metadata["forwards"] = [forward.to_dict() for forward in self.forwards_page.forwards()]
        elif self._metadata.get("forwards"):  # not opened: kept exactly as stored
            metadata["forwards"] = self._metadata.get("forwards")
        return HostEntry(
            alias=alias,
            options=options,
            metadata={k: v for k, v in metadata.items() if v},
            parent_group=parent,
            child_group=child,
            protocol=self._protocol,
        )

    def selected_credential_id(self) -> Optional[str]:
        page = getattr(self, "transfer_page", None)
        if page is not None and self._protocol in ("ftp", "ftps") and page.anonymous_active():
            return None  # anonymous FTP needs no credential
        return self._credential_combo.get_active_id() or None
