"""Create or edit a host entry.

Pages: General, Connection, Credentials, Tags & Notes, SSH Options and
Permissions, built on :class:`SettingsPageLayout`. Fields are validated inline
(alias, host name, port and ``key=value`` option lines); on Save the dialog
jumps to the first page with an error instead of closing.

``ProxyCommand`` stays in the SSH options list instead of being shown in the
jump-host field, so editing a host no longer rewrites a ProxyCommand into a
ProxyJump.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ...config import HostEntry  # noqa: E402
from ...credentials import Credential  # noqa: E402
from ...host_utils import host_option  # noqa: E402
from ...profiles import SessionProfile  # noqa: E402
from ...vault import VaultEntry  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..validation import validate_alias, validate_hostname, validate_port  # noqa: E402
from ..widgets.forms import FormRow, FormSection, entry  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402
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
        vault_entries: Optional[List[VaultEntry]] = None,
        vault_unlocked: bool = False,
        template_defaults: Optional[Dict[str, str]] = None,
        profiles: Optional[Dict[str, SessionProfile]] = None,
    ) -> None:
        title = _("Edit Host") if host else _("New Host")
        BaseDialog.__init__(self, parent, title, size=DialogSize.LARGE, height=560)
        self.get_content_area().set_margin_start(0)
        self.get_content_area().set_margin_end(0)
        self.get_content_area().set_margin_top(0)
        self._credential_choices = list(credentials or [])
        self._vault_entries = list(vault_entries or [])
        self._profiles = profiles or {}
        meta = dict(metadata or (host.metadata if host else {}) or {})
        self._metadata = meta
        self._sections: Dict[str, FormSection] = {}

        self._build_fields(meta, selected_credential_id, vault_unlocked)
        self.layout = SettingsPageLayout()
        self._build_pages()
        self.add_body(self.layout, expand=True)

        if host:
            self._fill_from_host(host)
        elif template_defaults:
            self._fill_from_template(template_defaults)
        self._apply_identity_source()

        self.add_cancel()
        self.add_primary(_("_Save"))
        self.set_initial_focus(self.alias_entry)
        self.set_response_validator(lambda r: not is_accept(r) or self.validate())
        self.show_all()

    # ------------------------------------------------------------ fields
    def _build_fields(self, meta: Dict[str, object], credential_id: Optional[str], vault_unlocked: bool) -> None:
        self.alias_entry = entry(placeholder=_("web-01"))
        self.hostname_entry = entry(placeholder=_("example.com or 10.0.0.5"))
        self.user_entry = entry(placeholder=_("ubuntu"))
        self.port_entry = entry(placeholder="22")
        self.port_entry.set_width_chars(6)
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
        self._credential_combo.connect("changed", lambda *_: self._apply_identity_source())
        self._vault_combo = _combo([(e.id, f"{e.label} ({e.username})") for e in self._vault_entries], str(meta.get("vault_id", "") or ""))
        self._vault_combo.set_sensitive(vault_unlocked and bool(self._vault_entries))
        self._vault_combo.connect("changed", lambda *_: self._apply_identity_source())
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
        for build in (self._page_general, self._page_connection, self._page_credentials,
                      self._page_tags, self._page_ssh_options, self._page_permissions):
            build(labels)

    def _page_general(self, labels: Gtk.SizeGroup) -> None:
        general = self.layout.new_page("general", _("General"), _("How the host appears in the tree and where it lives."))
        sec = FormSection()
        self._alias_row = sec.add(_("_Alias"), self.alias_entry, validator=validate_alias, label_group=labels,
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
                                     _("Reuse a saved credential or vault entry; its username replaces the User field."))
        sec = FormSection()
        sec.add(_("_Credential"), self._credential_combo, label_group=labels)
        sec.add(_("_Vault entry"), self._vault_combo, label_group=labels,
                help_text=_("Unlock the vault to choose an entry.") if not self._vault_combo.get_sensitive() else "")
        sec.add(_("Session _profile"), self._profile_combo, label_group=labels)
        creds.append(sec)
        self._sections["credentials"] = sec

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
        cred = next((c for c in self._credential_choices if c.id == self.selected_credential_id()), None)
        vault_entry = next((e for e in self._vault_entries if e.id == (self._vault_combo.get_active_id() or None)), None)
        source = vault_entry.username if vault_entry else (cred.username if cred else None)
        if source is not None:
            self.user_entry.set_text(source)
        self.user_entry.set_sensitive(source is None)

    # -------------------------------------------------------- validation
    def _extras_text(self) -> str:
        buf = self.extras_view.get_buffer()
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)

    def validate(self) -> bool:
        """Validate every field; show the first page with an error."""

        checks = [
            ("general", self._sections["general"].validate()),
            ("ssh_options", self._extras_row.validate()),
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
        options: Dict[str, str] = {}
        for key, widget in (
            ("hostname", self.hostname_entry),
            ("user", self.user_entry),
            ("port", self.port_entry),
            ("identityfile", self.identity_entry),
            ("proxyjump", self.proxy_entry),
        ):
            value = widget.get_text().strip()
            if value:
                options[key] = value
        tags = self.tags_entry.get_text().strip()
        if tags:
            options["tags"] = ",".join(part.strip() for part in tags.split(",") if part.strip())
        cred = next((c for c in self._credential_choices if c.id == self.selected_credential_id()), None)
        if cred:
            options["user"] = cred.username
        extra_options, _invalid = parse_option_lines(self._extras_text())
        options.update(extra_options)

        metadata: Dict[str, object] = {
            "environment": self.environment_entry.get_text().strip(),
            "owner": self.owner_entry.get_text().strip(),
            "service": self.service_entry.get_text().strip(),
            "notes": self.notes_entry.get_text().strip(),
        }
        vault_id = self._vault_combo.get_active_id() or ""
        if vault_id:
            metadata["vault_id"] = vault_id
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
        return HostEntry(
            alias=alias,
            options=options,
            metadata={k: v for k, v in metadata.items() if v},
            parent_group=parent,
            child_group=child,
        )

    def selected_credential_id(self) -> Optional[str]:
        return self._credential_combo.get_active_id() or None
