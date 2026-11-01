"""Trust decisions: TLS certificates (FTPS, RDP) and SFTP host keys.

Everything shown comes from the network (subject, issuer, fingerprints): it
is set as plain label text, never markup. A *changed* certificate or host
key defaults to **Cancel**.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ...transfer.backend import CertificateInfo  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402


class TrustDialog(BaseDialog):
    """A question with a details grid (label → value) and two buttons."""

    def __init__(self, parent: Optional[Gtk.Window], title: str, message: str, details: List[Tuple[str, str]], *,
                 accept_label: str, dangerous: bool) -> None:
        BaseDialog.__init__(self, parent, title, size=DialogSize.MEDIUM, resizable=False)
        heading = Gtk.Label(label=title)
        heading.set_xalign(0.0)
        heading.set_line_wrap(True)
        heading.get_style_context().add_class("section-title")
        self.add_body(heading)
        self.message_label = Gtk.Label(label=message)
        self.message_label.set_xalign(0.0)
        self.message_label.set_line_wrap(True)
        self.message_label.set_width_chars(50)
        self.add_body(self.message_label)
        grid = Gtk.Grid(column_spacing=Space.M, row_spacing=Space.XS)
        for row, (label, value) in enumerate(details):
            key = Gtk.Label(label=label)
            key.set_xalign(1.0)
            key.set_valign(Gtk.Align.START)
            key.get_style_context().add_class("dim-label")
            text = Gtk.Label(label=value or "—")
            text.set_xalign(0.0)
            text.set_line_wrap(True)
            text.set_selectable(True)
            text.set_max_width_chars(60)
            grid.attach(key, 0, row, 1, 1)
            grid.attach(text, 1, row, 1, 1)
        self.add_body(grid)
        self.add_cancel()
        # Dangerous: the safe choice (Cancel) is the default; Enter never accepts by accident.
        self.add_primary(accept_label, destructive=dangerous, default=not dangerous)
        if dangerous:
            self.set_default_response(Gtk.ResponseType.CANCEL)

    def ask(self) -> bool:
        return is_accept(self.run_and_destroy())


def _certificate_details(info: CertificateInfo) -> List[Tuple[str, str]]:
    return [(_("Subject"), info.subject), (_("Issuer"), info.issuer), (_("Valid from"), info.not_before),
            (_("Valid until"), info.not_after), (_("SHA-256"), info.sha256)]


def trust_certificate(parent: Optional[Gtk.Window], host_label: str, info: CertificateInfo, *,
                      changed: bool = False, expected: str = "") -> bool:
    if changed:
        details = _certificate_details(info) + [(_("Trusted before"), expected)]
        return TrustDialog(
            parent, _("The certificate of “{host}” has CHANGED").format(host=host_label),
            _("This can mean someone is intercepting the connection. Connect only if you know the server's "
              "certificate was renewed, and compare the fingerprint with the administrator."),
            details, accept_label=_("_Trust the New Certificate"), dangerous=True).ask()
    reason = info.reason or _("not issued by a trusted authority")
    return TrustDialog(
        parent, _("Trust the certificate of “{host}”?").format(host=host_label),
        _("The certificate could not be verified ({reason}). Compare the fingerprint with the server's "
          "administrator. If you trust it, it is remembered for this host.").format(reason=reason),
        _certificate_details(info), accept_label=_("_Trust and Connect"), dangerous=False).ask()


def host_key_message(key_type: str, other_key_types=()) -> str:
    if other_key_types:  # known with other key types: a new key for a known host (OpenSSH asks the same way)
        return _("You trusted {old} for this host before; the server now presents {new}. Compare the new "
                 "fingerprint with the server's administrator. If you accept it, it is added to "
                 "~/.ssh/known_hosts beside the old one.").format(old=", ".join(other_key_types), new=key_type)
    return _("The server's host key is not known yet. Compare the fingerprint with the server's administrator. "
             "It is remembered in ~/.ssh/known_hosts, like ssh does.")


def trust_host_key(parent: Optional[Gtk.Window], host_label: str, key_type: str, fingerprint: str, *,
                   other_key_types=(), other_fingerprints=()) -> bool:
    details = [(_("Key type"), key_type), (_("Fingerprint"), fingerprint)]
    if other_fingerprints:
        details.append((_("Trusted before"), "\n".join(other_fingerprints)))
    title = (_("Trust a new host key for “{host}”?") if other_key_types
             else _("Connect to “{host}” for the first time?")).format(host=host_label)
    return TrustDialog(parent, title, host_key_message(key_type, other_key_types), details,
                       accept_label=_("_Connect"), dangerous=False).ask()


TRUST_ONCE = 1


def rdp_trust_choice(parent: Optional[Gtk.Window], host_label: str, info: CertificateInfo) -> str:
    """First connection to an RDP server: ``"once"``, ``"remember"`` or ``"cancel"`` (Escape = cancel)."""

    dialog = TrustDialog(
        parent, _("Trust the certificate of “{host}”?").format(host=host_label),
        _("Remote Desktop servers usually have a certificate no authority vouches for. Compare the SHA-256 "
          "fingerprint with the server's administrator before you trust it. A certificate you remember is "
          "checked on every connection; if it ever changes, you are warned."),
        _certificate_details(info), accept_label=_("Trust and _Remember"), dangerous=False)
    dialog.add_secondary(_("Trust _Once"), TRUST_ONCE)
    response = dialog.run_and_destroy()
    if response == TRUST_ONCE:
        return "once"
    return "remember" if is_accept(response) else "cancel"


def confirm_unchecked_certificate(parent: Optional[Gtk.Window], host_label: str) -> bool:
    """Policy "Do not check": asked on every connection (Cancel is the default)."""

    return TrustDialog(
        parent, _("Connect to “{host}” without checking its certificate?").format(host=host_label),
        _("This host is set to not check the server's certificate. Anyone between you and the server could "
          "pretend to be it and read your password. The tab shows “Certificate not verified” while connected."),
        [], accept_label=_("Connect _Without Checking"), dangerous=True).ask()
