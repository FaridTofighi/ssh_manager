"""The host dialog's **File transfer** page (FTP, FTPS, SFTP) and its "Test connection" button.

The page edits a :class:`TransferSettings` (``host.metadata["transfer"]``):
security (FTPS: explicit / implicit TLS, certificate verification), connection
(passive / active, encoding, timeouts, keepalive, parallel connections),
paths, transfers and advanced options. Rows that do not apply to the chosen
protocol are hidden (SFTP has no TLS, PASV or encodings).

"Test connection" runs ``connect()`` in a worker thread with the values as
entered and reports the banner, TLS details, features and the time taken. It
never saves anything — not even a certificate or host key it was shown.
"""

from __future__ import annotations

import threading
from typing import Callable, Dict, List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

from ...transfer.backend import (  # noqa: E402
    BackendError, HostKeyChanged, RemoteFileBackend, UnknownHostKey, UntrustedCertificate,
)
from ...transfer.settings import ENCODINGS, TransferSettings  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.forms import FormRow, FormSection, entry  # noqa: E402
from .confirm import confirm  # noqa: E402

# (host, user, password) -> a backend that is not connected yet; supplied by the caller
BackendFactory = Callable[[object, str, str], RemoteFileBackend]


def _spin(low: int, high: int, value: int) -> Gtk.SpinButton:
    spin = Gtk.SpinButton.new_with_range(low, high, 1)
    spin.set_digits(0)
    spin.set_value(value)
    spin.set_halign(Gtk.Align.START)
    return spin


def _combo(choices: List[Tuple[str, str]], active: str) -> Gtk.ComboBoxText:
    combo = Gtk.ComboBoxText()
    for choice_id, label in choices:
        combo.append(choice_id, label)
    if not combo.set_active_id(active):
        combo.set_active(0)
    return combo


class TransferPage:
    """Widgets of the File transfer page; :meth:`settings` reads them back."""

    def __init__(self, parent: Gtk.Window, settings: TransferSettings, labels: Gtk.SizeGroup, *,
                 on_default_port: Callable[[], None] = lambda: None,
                 on_anonymous: Callable[[bool], None] = lambda _on: None) -> None:
        self._parent = parent
        self._base = settings
        self._on_default_port = on_default_port
        self._on_anonymous = on_anonymous
        self._protocol = "ftp"
        self._rows: Dict[str, List[FormRow]] = {}
        self._suppress = False
        self._closed = False
        self.sections: List[FormSection] = []

        # ------------------------------------------------------ security (FTPS)
        self.tls_mode = _combo([("explicit", _("Explicit (AUTH TLS, port 21)")),
                                ("implicit", _("Implicit (TLS from the start, port 990)"))], settings.tls_mode)
        self.tls_mode.connect("changed", lambda *_: self._on_default_port())
        self.tls_verify = _combo([("system", _("Verify with the system certificates")),
                                  ("custom_ca", _("Verify with a custom CA file")),
                                  ("pinned", _("Trust one certificate (pinned)")),
                                  ("insecure", _("Do not verify (insecure)"))], settings.tls_verify)
        self._last_verify = settings.tls_verify
        self.tls_verify.connect("changed", self._on_verify_changed)
        self.ca_file = Gtk.FileChooserButton.new(_("CA certificate"), Gtk.FileChooserAction.OPEN)
        if settings.ca_file:
            self.ca_file.set_filename(settings.ca_file)
        security = FormSection(_("Security"))
        self._add("ftps", security, _("_TLS"), self.tls_mode, labels)
        self._add("ftps", security, _("_Certificate"), self.tls_verify, labels,
                  help_text=_("A certificate you trust when connecting is remembered (pinned) for this host."))
        self._add("ftps", security, _("CA _file"), self.ca_file, labels)
        self.sections.append(security)

        # --------------------------------------------------------- connection
        self.passive = Gtk.CheckButton.new_with_mnemonic(_("_Passive mode (recommended)"))
        self.passive.set_active(settings.passive)
        self.encoding = Gtk.ComboBoxText.new_with_entry()
        self.encoding.append("auto", _("Automatic (UTF-8)"))
        for name in ENCODINGS:
            self.encoding.append(name, name)
        if not self.encoding.set_active_id(settings.encoding):
            self.encoding.get_child().set_text(settings.encoding)
        self.timeout_connect = _spin(1, 600, settings.timeout_connect)
        self.timeout_read = _spin(1, 3600, settings.timeout_read)
        self.keepalive = _spin(0, 3600, settings.keepalive_s)
        self.max_connections = _spin(1, 8, settings.max_connections)
        connection = FormSection(_("Connection"))
        self._add("ftp", connection, _("Data"), self.passive, labels,
                  help_text=_("Active mode needs the server to connect back to this computer."))
        self._add("ftp", connection, _("_Encoding"), self.encoding, labels,
                  help_text=_("For file names; change it only for servers that do not use UTF-8."))
        self._add("all", connection, _("Connect _timeout (s)"), self.timeout_connect, labels)
        self._add("all", connection, _("_Read timeout (s)"), self.timeout_read, labels)
        self._add("all", connection, _("_Keepalive (s)"), self.keepalive, labels, help_text=_("0 turns it off."))
        self._add("all", connection, _("Parallel _connections"), self.max_connections, labels,
                  help_text=_("Transfers at the same time (FTP uses one connection per transfer)."))
        self.sections.append(connection)

        # ------------------------------------------------------------- paths
        self.remote_dir = entry(settings.initial_remote_dir, _("/home/alice or empty for the login folder"))
        self.local_dir = Gtk.FileChooserButton.new(_("Local folder"), Gtk.FileChooserAction.SELECT_FOLDER)
        if settings.initial_local_dir:
            self.local_dir.set_filename(settings.initial_local_dir)
        paths = FormSection(_("Folders"))
        self._add("all", paths, _("Remote _folder"), self.remote_dir, labels)
        self._add("all", paths, _("_Local folder"), self.local_dir, labels)
        self.sections.append(paths)

        # --------------------------------------------------------- transfers
        self.transfer_mode = _combo([("binary", _("Binary (exact copy)")),
                                     ("auto", _("Text files in ASCII mode"))], settings.transfer_mode)
        self.ascii_extensions = entry(", ".join(settings.ascii_extensions), "txt, csv, log")
        self.upload_atomic = Gtk.CheckButton.new_with_mnemonic(
            _("Upload to a ._part file, then _rename (never a half-written file)"))
        self.upload_atomic.set_active(settings.upload_atomic)
        self.preserve_mtime = Gtk.CheckButton.new_with_mnemonic(_("Keep _modification times"))
        self.preserve_mtime.set_active(settings.preserve_mtime)
        self.bandwidth = _spin(0, 10_000_000, settings.bandwidth_limit_kbps)
        transfers = FormSection(_("Transfers"))
        self._add("ftp", transfers, _("_Mode"), self.transfer_mode, labels)
        self._add("ftp", transfers, _("_ASCII extensions"), self.ascii_extensions, labels)
        self._add("all", transfers, "", self.upload_atomic, labels)
        self._add("all", transfers, "", self.preserve_mtime, labels)
        self._add("all", transfers, _("_Speed limit (KB/s)"), self.bandwidth, labels, help_text=_("0 = unlimited."))
        self.sections.append(transfers)

        # ---------------------------------------------------------- advanced
        self.anonymous = Gtk.CheckButton.new_with_mnemonic(_("_Anonymous login (no credential)"))
        self.anonymous.set_active(settings.anonymous)
        self.anonymous.connect("toggled", lambda button: self._on_anonymous(button.get_active()))
        self.anonymous_password = entry(settings.anonymous_password, "anonymous@")
        self.trust_pasv = Gtk.CheckButton.new_with_mnemonic(_("Trust the server-provided _PASV address"))
        self.trust_pasv.set_active(settings.trust_pasv_address)
        self.list_command = _combo([("auto", _("Automatic (MLSD when offered)")), ("mlsd", "MLSD"),
                                    ("list", "LIST"), ("list-a", "LIST -a")], settings.list_command)
        self.time_offset = _spin(-24 * 60, 24 * 60, settings.server_time_offset_min)
        self.active_ports = entry(settings.active_port_range, "50000-50100")
        self.show_hidden = Gtk.CheckButton.new_with_mnemonic(_("Show _hidden files"))
        self.show_hidden.set_active(settings.show_hidden)
        self.follow_symlinks = Gtk.CheckButton.new_with_mnemonic(_("_Follow symbolic links when transferring"))
        self.follow_symlinks.set_active(settings.follow_symlinks)
        advanced = FormSection(_("Advanced"))
        self._add("ftp", advanced, "", self.anonymous, labels)
        self._add("ftp", advanced, _("Anonymous pass_word"), self.anonymous_password, labels,
                  help_text=_("By convention an e-mail address; not a secret."))
        self._add("ftp", advanced, "", self.trust_pasv, labels,
                  help_text=_("Only for servers behind NAT that need it; off protects against FTP bounce tricks."))
        self._add("ftp", advanced, _("_Listing"), self.list_command, labels)
        self._add("ftp", advanced, _("Server time offset (min)"), self.time_offset, labels)
        self._add("ftp", advanced, _("Active-mode _ports"), self.active_ports, labels,
                  help_text=_("A local port range for active mode; empty uses any free port."))
        self._add("all", advanced, "", self.show_hidden, labels)
        self._add("all", advanced, "", self.follow_symlinks, labels)
        self.sections.append(advanced)

        # --------------------------------------------------- test connection
        self.test_button = Gtk.Button.new_with_mnemonic(_("_Test Connection"))
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

    # ------------------------------------------------------------ layout
    def _add(self, scope: str, section: FormSection, label: str, control: Gtk.Widget, labels: Gtk.SizeGroup,
             help_text: str = "") -> FormRow:
        row = section.add(label or " ", control, label_group=labels, help_text=help_text)
        self._rows.setdefault(scope, []).append(row)
        return row

    def append_to(self, page) -> None:
        for section in self.sections:
            page.append(section)
        page.append(self.test_box)

    def set_protocol(self, protocol: str) -> None:
        """Show the rows that apply: ``ftp`` (plain), ``ftps`` (+ TLS) or ``sftp`` (no FTP specifics)."""

        self._protocol = protocol
        for scope, rows in self._rows.items():
            visible = scope == "all" or (scope == "ftp" and protocol in ("ftp", "ftps")) or \
                (scope == "ftps" and protocol == "ftps")
            for row in rows:
                for widget in (row.label, row.control, row.help):
                    widget.set_no_show_all(not visible)
                    widget.set_visible(visible and (widget is not row.help or bool(row.help.get_text())))
                    if visible and widget is row.control:
                        widget.show_all()
        self.ca_file.set_sensitive(self.tls_verify.get_active_id() == "custom_ca")

    def anonymous_active(self) -> bool:
        return self._protocol in ("ftp", "ftps") and self.anonymous.get_active()

    # ----------------------------------------------------------- values
    def _on_verify_changed(self, combo: Gtk.ComboBoxText) -> None:
        if self._suppress:
            return
        choice = combo.get_active_id() or "system"
        if choice == "insecure" and not confirm(
            self._parent,
            _("Turn off certificate verification?"),
            _("Anyone between you and the server could read and change the connection, including the password. "
              "The tab will show a warning while it is connected."),
            confirm_label=_("_Turn Off Verification"),
            destructive=True,
        ):
            self._suppress = True
            combo.set_active_id(self._last_verify)
            self._suppress = False
            return
        self._last_verify = choice
        self.ca_file.set_sensitive(choice == "custom_ca")

    def tls_mode_id(self) -> str:
        return self.tls_mode.get_active_id() or "explicit"

    def settings(self) -> TransferSettings:
        base = self._base
        encoding = self.encoding.get_active_id() or self.encoding.get_child().get_text().strip() or "auto"
        return TransferSettings(
            passive=self.passive.get_active(),
            encoding=encoding,
            tls_mode=self.tls_mode_id(),
            tls_verify=self.tls_verify.get_active_id() or "system",
            ca_file=self.ca_file.get_filename() or "",
            pinned_sha256=base.pinned_sha256,
            trust_pasv_address=self.trust_pasv.get_active(),
            initial_remote_dir=self.remote_dir.get_text().strip(),
            initial_local_dir=self.local_dir.get_filename() or "",
            ascii_extensions=[e.strip().lstrip(".") for e in self.ascii_extensions.get_text().split(",") if e.strip()],
            transfer_mode=self.transfer_mode.get_active_id() or "binary",
            timeout_connect=int(self.timeout_connect.get_value()),
            timeout_read=int(self.timeout_read.get_value()),
            keepalive_s=int(self.keepalive.get_value()),
            max_connections=int(self.max_connections.get_value()),
            show_hidden=self.show_hidden.get_active(),
            preserve_mtime=self.preserve_mtime.get_active(),
            upload_atomic=self.upload_atomic.get_active(),
            server_time_offset_min=int(self.time_offset.get_value()),
            active_port_range=self.active_ports.get_text().strip(),
            bandwidth_limit_kbps=int(self.bandwidth.get_value()),
            follow_symlinks=self.follow_symlinks.get_active(),
            list_command=self.list_command.get_active_id() or "auto",
            anonymous=self.anonymous_active(),
            anonymous_password=self.anonymous_password.get_text().strip() or "anonymous@",
            extra=dict(base.extra),
        )

    def problems(self) -> List[str]:
        if self._protocol not in ("ftp", "ftps", "sftp"):
            return []
        problems = self.settings().problems()
        if self._protocol != "ftps":
            problems = [p for p in problems if "CA" not in p]
        if self._protocol == "sftp":
            problems = [p for p in problems if "encoding" not in p and "port range" not in p]
        return problems

    # ------------------------------------------------- test connection
    def test_connection(self, factory: Callable[[], Optional[RemoteFileBackend]]) -> None:
        """Connect in a worker with the values as entered; report the result. Nothing is saved."""

        self.test_button.set_sensitive(False)
        self.test_spinner.start()
        self.test_result.set_text(_("Connecting…"))
        try:
            backend = factory()
        except Exception as exc:  # invalid values: say so instead of crashing
            self._show_test(False, _("Cannot test: {error}").format(error=exc))
            return
        if backend is None:
            self._show_test(False, _("Enter the host name first."))
            return

        def work() -> None:
            try:
                info = backend.connect()
                text = "\n".join([_("Connected.")] + info.lines())
                ok = True
            except UntrustedCertificate as exc:
                cert = exc.info
                text = _("The certificate is not trusted ({reason}).\nSubject: {subject}\nIssuer: {issuer}\n"
                         "Valid until: {until}\nSHA-256: {sha}\nYou can trust it when you connect.").format(
                    reason=cert.reason or exc.message, subject=cert.subject, issuer=cert.issuer,
                    until=cert.not_after, sha=cert.sha256)
                ok = False
            except UnknownHostKey as exc:
                text = _("The host key is not known yet: {type} {fingerprint}\nYou can accept it when you "
                         "connect.").format(type=exc.key_type, fingerprint=exc.fingerprint)
                ok = False
            except HostKeyChanged as exc:
                text = exc.message
                ok = False
            except BackendError as exc:
                text = exc.message + (f"\n{exc.technical}" if exc.technical else "")
                ok = False
            except Exception as exc:  # never let a worker die silently
                text = _("The test failed: {error}").format(error=type(exc).__name__)
                ok = False
            finally:
                try:
                    backend.close()
                except Exception:
                    pass
            GLib.idle_add(self._show_test, ok, text)

        threading.Thread(target=work, daemon=True, name="sshm-test-connection").start()

    def _show_test(self, ok: bool, text: str) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        self.test_spinner.stop()
        self.test_button.set_sensitive(True)
        self.test_result.set_text(text)  # plain text: server data is never markup
        context = self.test_result.get_style_context()
        context.remove_class("form-error" if ok else "form-help")
        context.add_class("form-help" if ok else "form-error")
        self.last_test = (ok, text)
        return GLib.SOURCE_REMOVE

    def close(self) -> None:
        self._closed = True
