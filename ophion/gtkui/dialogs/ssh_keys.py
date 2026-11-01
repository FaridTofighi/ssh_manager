"""SSH Keys: the keys in ``~/.ssh`` (and added by path), generating, importing, public keys, passphrases.

One ``Gtk.TreeView`` over a ``Gtk.ListStore`` (no widget per key): name, type,
SHA256 fingerprint, comment, passphrase (yes / no / unknown), public key file,
and how many hosts use it. Identity files hosts point to that no longer exist
are listed too, with a warning. Nothing here asks for a passphrase except the
actions that need one (Derive Public Key, Change Passphrase).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable, List, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # type: ignore  # noqa: E402

from ... import ssh_keys as K  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..theme.tokens import Level, Space  # noqa: E402
from ..widgets.forms import FormSection  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from ..widgets.secret_entry import SecretEntry  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402
from .confirm import confirm, show_message  # noqa: E402

(COL_ICON, COL_NAME, COL_TYPE, COL_FINGERPRINT, COL_COMMENT, COL_PASSPHRASE, COL_PUBLIC, COL_USED, COL_PATH,
 COL_TOOLTIP) = range(10)
KINDS = (("ed25519", 0, _("Ed25519 (recommended)")), ("rsa", 3072, _("RSA 3072")), ("rsa", 4096, _("RSA 4096")),
         ("ecdsa", 256, _("ECDSA P-256")), ("ecdsa", 384, _("ECDSA P-384")))


def _mono_view(text: str) -> Gtk.TextView:
    view = Gtk.TextView()
    view.set_editable(False)
    view.set_monospace(True)
    view.set_wrap_mode(Gtk.WrapMode.CHAR)
    view.get_buffer().set_text(text)
    return view


def show_public_key(parent: Optional[Gtk.Window], name: str, line: str) -> None:
    dialog = BaseDialog(parent, _("Public Key: {name}").format(name=name), size=DialogSize.MEDIUM)
    help_text = Gtk.Label(label=_("Add this line to ~/.ssh/authorized_keys on a server to log in with the key."))
    help_text.set_xalign(0.0)
    help_text.set_line_wrap(True)
    dialog.add_body(help_text)
    view = _mono_view(line)
    set_accessible_name(view, _("Public key line"))
    scroller = Gtk.ScrolledWindow()
    scroller.set_min_content_height(90)
    scroller.add(view)
    dialog.add_body(scroller, expand=True)
    copy = dialog.add_secondary(_("_Copy"), 1)
    copy.connect("clicked", lambda *_a: Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(line, -1))
    dialog.add_primary(_("_Close"), Gtk.ResponseType.CLOSE)
    dialog.show_all()
    try:
        while dialog.run() == 1:  # Copy keeps the dialog open
            pass
    finally:
        dialog.destroy()


class GenerateKeyDialog(BaseDialog):
    """Name, type, comment, passphrase (twice), remember it; generation runs off the GTK thread."""

    def __init__(self, parent: Optional[Gtk.Window], controller) -> None:
        BaseDialog.__init__(self, parent, _("Generate SSH Key"), size=DialogSize.MEDIUM, resizable=False)
        self.controller = controller
        self.result: Optional[K.KeyInfo] = None
        self._worker_id = 0
        section = FormSection()
        self.name = Gtk.Entry(text=controller.suggest("id_ed25519"))
        self.name_row = section.add(_("File _name"), self.name)
        self.kind = Gtk.ComboBoxText()
        for index, (_kind, _bits, label) in enumerate(KINDS):
            self.kind.append(str(index), label)
        self.kind.set_active(0)
        section.add(_("_Type"), self.kind)
        self.comment = Gtk.Entry(text=controller.default_comment())
        section.add(_("_Comment"), self.comment)
        self.passphrase = SecretEntry(_("Passphrase"), show_strength=True)
        section.add(_("_Passphrase"), self.passphrase)
        self.confirm = SecretEntry(_("Repeat the passphrase"))
        self.confirm_row = section.add(_("Con_firm"), self.confirm)
        self.add_body(section)
        self.remember = Gtk.CheckButton.new_with_mnemonic(_("_Remember the passphrase in Ophion (encrypted)"))
        self.add_body(self.remember)
        self.warning = Gtk.Label()
        self.warning.set_xalign(0.0)
        self.warning.set_line_wrap(True)
        self.add_body(self.warning)
        detail = Gtk.Label(label=_("Saved in {folder} (the private key 0600). With a passphrase the key is encrypted "
                                   "in OpenSSH format.").format(folder=controller.ssh_dir)
                           if K.bcrypt_available() else
                           _("bcrypt is not installed: RSA and ECDSA keys with a passphrase use the encrypted PEM "
                             "format; Ed25519 keys need bcrypt for a passphrase."))
        detail.set_xalign(0.0)
        detail.set_line_wrap(True)
        detail.get_style_context().add_class("form-help")
        self.add_body(detail)
        self.spinner = Gtk.Spinner()
        self.add_body(self.spinner)
        self.add_cancel()
        self.add_primary(_("_Generate"))
        self._remember_touched = False
        self.remember.connect("toggled", lambda *_a: setattr(self, "_remember_touched", True)
                              if not getattr(self, "_syncing", False) else None)
        for widget in (self.name, self.passphrase.entry, self.confirm.entry):
            widget.connect("changed", lambda *_a: self._update())
        self.kind.connect("changed", lambda *_a: self._kind_changed())
        self.set_response_validator(self._validate)
        self.connect("response", self._on_response)
        self._update()

    @property
    def choice(self):
        return KINDS[int(self.kind.get_active_id() or 0)]

    def _kind_changed(self) -> None:
        kind = self.choice[0]
        current = self.name.get_text().strip()
        if current.startswith("id_") and not current.startswith(f"id_{kind}"):
            self.name.set_text(self.controller.suggest(f"id_{kind}"))
        self._update()

    def errors(self) -> List[str]:
        problems = []
        name = self.name.get_text().strip()
        if not name or "/" in name or name.startswith(".") or name.endswith(".pub"):
            problems.append(_("Choose a plain file name."))
        elif (self.controller.ssh_dir / name).exists() or (self.controller.ssh_dir / f"{name}.pub").exists():
            problems.append(_("{name} already exists.").format(name=name))
        if self.passphrase.get_text() != self.confirm.get_text():
            problems.append(_("The passphrases do not match."))
        return problems

    def _update(self) -> None:
        passphrase = self.passphrase.get_text()
        if not self._remember_touched:
            self._syncing = True
            self.remember.set_active(bool(passphrase))
            self._syncing = False
        self.remember.set_sensitive(bool(passphrase))
        problems = self.errors()
        lines = [f"✖ {p}" for p in problems]
        if not passphrase:
            lines.append("⚠ " + _("No passphrase: anyone with the file can use the key."))
        self.warning.set_text("\n".join(lines))
        self.primary_button.set_sensitive(not problems and not self._worker_id)

    def _validate(self, response: int) -> bool:
        if not is_accept(response) or self.result is not None:
            return True
        if self.errors() or self._worker_id:
            return False
        self.start()
        return False  # closes when the key is written

    def start(self) -> None:
        kind, bits, _label = self.choice
        self._worker_id += 1
        worker = self._worker_id
        self.spinner.start()
        self.primary_button.set_sensitive(False)

        def work() -> None:
            try:
                private = K.make_private_key(kind, bits)  # the slow part (RSA 4096)
                result: object = private
            except K.KeyError_ as exc:
                result = exc
            GLib.idle_add(self._generated, worker, result)

        threading.Thread(target=work, daemon=True, name="sshm-keys-generate").start()

    def _generated(self, worker: int, result: object) -> bool:
        if worker != self._worker_id:  # cancelled: the key is dropped, nothing was written
            return False
        self._worker_id = 0
        self.spinner.stop()
        if isinstance(result, Exception):
            self.warning.set_text(f"✖ {result}")
            self._update()
            return False
        try:
            self.result = self.controller.write_generated(
                self.name.get_text().strip(), result, comment=self.comment.get_text().strip(),
                passphrase=self.passphrase.get_text(), remember=self.remember.get_active())
        except (K.KeyError_, OSError) as exc:
            self.warning.set_text("✖ " + str(exc))
            self._update()
            return False
        self.response(Gtk.ResponseType.OK)
        return False

    def _on_response(self, _dialog, response: int) -> None:
        if not is_accept(response):
            self._worker_id += 1  # a running generation is dropped (Cancel)

    def ask(self) -> Optional[K.KeyInfo]:
        self.run_and_destroy()
        return self.result


def ask_passphrase(parent: Optional[Gtk.Window], title: str, check: Callable[[str], bool]) -> Optional[str]:
    from .secrets import UnlockDialog

    return UnlockDialog(parent, title, title, _("The passphrase of the key."), check,
                        error_text=_("The passphrase is not correct.")).ask()


class SshKeysDialog(BaseDialog):
    def __init__(self, parent: Optional[Gtk.Window], controller, *, select: str = "") -> None:
        BaseDialog.__init__(self, parent, _("SSH Keys"), size=DialogSize.LARGE, height=480)
        self.controller = controller
        self.infos: dict = {}
        # Test hooks.
        self.confirm: Callable[[str, str], bool] = lambda title, text: confirm(
            self, title, text, confirm_label=_("_Delete"), destructive=True)
        self.message: Callable[[str, str], None] = lambda title, text: show_message(self, title, text)
        self.generate_dialog: Callable[[], Optional[K.KeyInfo]] = lambda: GenerateKeyDialog(self, controller).ask()
        self.ask_rename: Callable[[str], Optional[str]] = self._ask_rename
        self.ask_passphrase: Callable[[K.KeyInfo], Optional[str]] = self._ask_passphrase
        self.ask_new_passphrase: Callable[[K.KeyInfo], Optional[tuple]] = self._ask_new_passphrase
        self.choose_import: Callable[[], Optional[tuple]] = self._choose_import
        self.show_public: Callable[[str, str], None] = lambda name, line: show_public_key(self, name, line)
        self.deploy: Callable[[K.KeyInfo], None] = lambda info: None  # set by the deploy feature

        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.buttons = {}
        for name, label, callback in (("generate", _("_Generate…"), self.generate),
                                      ("import", _("_Import…"), self.import_key),
                                      ("copy", _("_Copy Public Key"), self.copy_public),
                                      ("show", _("Show _Public Key"), self.show_public_key),
                                      ("deploy", _("_Deploy to Hosts…"), lambda: self.deploy(self.selected())),
                                      ("derive", _("Derive Public _Key…"), self.derive),
                                      ("passphrase", _("Change _Passphrase…"), self.change_passphrase),
                                      ("rename", _("_Rename…"), self.rename),
                                      ("delete", _("De_lete…"), self.delete),
                                      ("reveal", _("Show in _Folder"), self.reveal)):
            button = Gtk.Button.new_with_mnemonic(label)
            button.connect("clicked", lambda _b, cb=callback: cb())
            self.buttons[name] = button
        rows = (("generate", "import", "copy", "show", "deploy"), ("derive", "passphrase", "rename", "delete",
                                                                     "reveal"))
        for names in rows:
            line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
            for name in names:
                line.pack_start(self.buttons[name], False, False, 0)
            toolbar.pack_start(line, False, False, 0)
        toolbar.set_orientation(Gtk.Orientation.VERTICAL)
        self.add_body(toolbar)

        self.store = Gtk.ListStore(str, str, str, str, str, str, str, str, str, str)
        self.view = Gtk.TreeView(model=self.store)
        self.view.set_tooltip_column(COL_TOOLTIP)
        set_accessible_name(self.view, _("SSH keys"))
        name_column = Gtk.TreeViewColumn(_("Name"))
        icon = Gtk.CellRendererPixbuf()
        name_column.pack_start(icon, False)
        name_column.add_attribute(icon, "icon-name", COL_ICON)
        text = Gtk.CellRendererText()
        name_column.pack_start(text, True)
        name_column.add_attribute(text, "text", COL_NAME)
        self.view.append_column(name_column)
        for title, column, mono in ((_("Type"), COL_TYPE, False), (_("SHA256 fingerprint"), COL_FINGERPRINT, True),
                                    (_("Comment"), COL_COMMENT, False), (_("Passphrase"), COL_PASSPHRASE, False),
                                    (_("Public key file"), COL_PUBLIC, False), (_("Used by"), COL_USED, False)):
            cell = Gtk.CellRendererText()
            cell.set_property("ellipsize", Pango.EllipsizeMode.END)
            if mono:
                cell.set_property("family", "monospace")
            col = Gtk.TreeViewColumn(title, cell, text=column)
            col.set_resizable(True)
            col.set_expand(column in (COL_FINGERPRINT, COL_COMMENT))
            self.view.append_column(col)
        self.view.get_selection().connect("changed", lambda *_a: self._sync())
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.add(self.view)
        self.add_body(scroller, expand=True)
        self.status = Gtk.Label()
        self.status.set_xalign(0.0)
        self.status.get_style_context().add_class("form-help")
        self.add_body(self.status)
        self.add_primary(_("_Close"), Gtk.ResponseType.CLOSE)
        self.refresh(select=select)

    # -------------------------------------------------------------- rows
    def refresh(self, *, select: str = "") -> None:
        import os

        self.store.clear()
        self.infos.clear()
        usage = self.controller.usage()
        for info in self.controller.keys():
            real = os.path.realpath(str(info.path))
            self.infos[str(info.path)] = info
            used = usage.get(real, 0)
            passphrase = _("unknown") if info.encrypted is None else (_("yes") if info.encrypted else _("no"))
            if info.encrypted and self.controller.has_saved_passphrase(info.path):
                passphrase = _("yes (saved)")
            self.store.append(["dialog-password-symbolic", info.name, info.type_label, info.fingerprint or "—",
                               info.comment, passphrase, _("yes") if info.has_public_file else _("missing"),
                               ngettext("{n} host", "{n} hosts", used).format(n=used) if used else "",
                               str(info.path), str(info.path)])
        for path, count in sorted(self.controller.missing_references().items()):
            self.store.append(["dialog-warning-symbolic", Path(path).name, _("missing"), "", "", "", "",
                               ngettext("{n} host", "{n} hosts", count).format(n=count), "",
                               _("{path} does not exist, but hosts still use it as their identity file.").format(
                                   path=path)])
        self.status.set_text(_("Keys in {folder}").format(folder=self.controller.ssh_dir))
        if select:
            self.select(select)
        self._sync()

    def select(self, path: str) -> bool:
        for row in self.store:
            if row[COL_PATH] == str(path):
                self.view.get_selection().select_iter(row.iter)
                return True
        return False

    def selected(self) -> Optional[K.KeyInfo]:
        model, tree_iter = self.view.get_selection().get_selected()
        return self.infos.get(model[tree_iter][COL_PATH]) if tree_iter is not None else None

    def _sync(self) -> None:
        info = self.selected()
        for name in ("copy", "show", "deploy"):
            self.buttons[name].set_sensitive(bool(info and info.public_line))
        for name in ("passphrase", "rename", "delete", "reveal"):
            self.buttons[name].set_sensitive(info is not None)
        self.buttons["derive"].set_sensitive(bool(info and not info.public_line and info.encrypted))

    # ----------------------------------------------------------- actions
    def generate(self) -> Optional[K.KeyInfo]:
        info = self.generate_dialog()
        if info is not None:
            self.refresh(select=str(info.path))
        return info

    def _choose_import(self) -> Optional[tuple]:
        from .files import choose_open_file

        name = choose_open_file(self, _("Import a private key"))
        if not name:
            return None
        copy = confirm(self, _("Copy the key into {folder}?").format(folder=self.controller.ssh_dir),
                       _("Copy: a private copy (0600) in your SSH folder. Cancel: use the key where it is."),
                       confirm_label=_("_Copy"), default_confirm=True)
        return name, copy

    def import_key(self) -> Optional[K.KeyInfo]:
        chosen = self.choose_import()
        if not chosen:
            return None
        name, copy = chosen
        try:
            info = (self.controller.import_copy if copy else self.controller.import_reference)(Path(name))
        except (K.KeyError_, OSError) as exc:
            self.message(_("The key was not imported"), str(exc))
            return None
        self.refresh(select=str(info.path))
        return info

    def copy_public(self) -> str:
        info = self.selected()
        if info is None or not info.public_line:
            return ""
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(info.public_line, -1)
        self.controller.win._notify(_("Public key copied."), Level.SUCCESS)
        return info.public_line

    def show_public_key(self) -> None:
        info = self.selected()
        if info is not None and info.public_line:
            self.show_public(info.name, info.public_line)

    def _ask_passphrase(self, info: K.KeyInfo) -> Optional[str]:
        def check(text: str) -> bool:
            try:
                K.derive_public_line(info.path, text)
                return True
            except K.KeyError_:
                return False

        return ask_passphrase(self, _("Passphrase of {name}").format(name=info.name), check)

    def derive(self) -> str:
        info = self.selected()
        if info is None:
            return ""
        passphrase = self.ask_passphrase(info)
        if passphrase is None:
            return ""
        try:
            line = self.controller.derive_public(info, passphrase)
        except (K.KeyError_, OSError) as exc:
            self.message(_("The public key could not be derived"), str(exc))
            return ""
        self.refresh(select=str(info.path))
        return line

    def _ask_new_passphrase(self, info: K.KeyInfo) -> Optional[tuple]:
        from .secrets import NewSecretDialog

        def check(text: str) -> bool:
            try:
                K.derive_public_line(info.path, text)
                return True
            except K.KeyError_:
                return False

        dialog = NewSecretDialog(self, _("Change Passphrase"), _("New passphrase for {name}").format(name=info.name),
                                 accept_label=_("_Change"), current_label=_("C_urrent"),
                                 verify_current=check if info.encrypted else None)
        remember = Gtk.CheckButton.new_with_mnemonic(_("_Remember the passphrase in Ophion (encrypted)"))
        remember.set_active(True)
        dialog.add_body(remember)
        new = dialog.ask()
        if new is None:
            return None
        return (dialog.current_password or "", new, remember.get_active())

    def change_passphrase(self) -> bool:
        info = self.selected()
        if info is None:
            return False
        answer = self.ask_new_passphrase(info)
        if answer is None:
            return False
        old, new, remember = answer
        try:
            self.controller.change_passphrase(info, old, new, remember=remember)
        except (K.KeyError_, OSError) as exc:
            self.message(_("The passphrase was not changed"), str(exc))
            return False
        self.refresh(select=str(info.path))
        return True

    def _ask_rename(self, current: str) -> Optional[str]:
        return self.controller.win._prompt_text(_("Rename Key"), _("New file name:"), current,
                                                accept_label=_("_Rename"))

    def rename(self) -> Optional[K.KeyInfo]:
        info = self.selected()
        if info is None:
            return None
        name = self.ask_rename(info.name)
        if not name or name == info.name:
            return None
        try:
            renamed = self.controller.rename(info, name)
        except (K.KeyError_, OSError) as exc:
            self.message(_("The key was not renamed"), str(exc))
            return None
        used = self.controller.usage().get(str(info.path), 0)
        if used:
            self.message(_("Hosts still use the old name"),
                         ngettext("{n} host uses {old} as its identity file; update it in the host.",
                                  "{n} hosts use {old} as their identity file; update them.", used).format(
                             n=used, old=info.path))
        self.refresh(select=str(renamed.path))
        return renamed

    def delete(self) -> bool:
        import os

        info = self.selected()
        if info is None:
            return False
        used = self.controller.usage().get(os.path.realpath(str(info.path)), 0)
        text = _("The private key and its .pub file are deleted. Servers that trust it keep its public key.")
        if used:
            text += "\n\n" + ngettext("{n} host uses this key; it keeps the setting and shows a warning.",
                                      "{n} hosts use this key; they keep the setting and show a warning.",
                                      used).format(n=used)
        if not self.confirm(_("Delete the key {name}?").format(name=info.name), text):
            return False
        self.controller.delete(info)
        self.refresh()
        return True

    def reveal(self) -> None:
        info = self.selected()
        if info is not None and not self.controller.reveal(info):
            self.message(_("The key is in"), str(info.path))

    def ask(self) -> None:
        self.run_and_destroy()


__all__ = ["SshKeysDialog", "GenerateKeyDialog", "show_public_key", "ask_passphrase", "KINDS"]
