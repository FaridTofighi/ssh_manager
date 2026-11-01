"""Preferences → Security → SSH agent: whether an agent answers, and adding a key to it.

``ssh-add`` runs in its own pty (see :func:`ophion.ssh_keys.agent_add`): a
passphrase — the saved one, or one typed here — is typed at its prompt, never
put in its arguments or environment.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

from ... import ssh_keys as K  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.forms import FormSection  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402


class AgentSection(FormSection):
    def __init__(self, controller, parent: Optional[Gtk.Window] = None) -> None:
        FormSection.__init__(self, _("SSH agent"),
                             _("Keys added to the agent are used by ssh without asking for their passphrase again."))
        self.controller = controller
        self.parent_window = parent
        self.busy = False
        # Test hook: the passphrase of an encrypted key without a saved one (None: cancelled).
        self.ask_passphrase: Callable[[K.KeyInfo], Optional[str]] = self._ask_passphrase

        self.status_label = Gtk.Label()
        self.status_label.set_xalign(0.0)
        self.status_label.set_line_wrap(True)
        self.add_widget(self.status_label)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.key_combo = Gtk.ComboBoxText()
        set_accessible_name(self.key_combo, _("Key to add"))
        row.pack_start(self.key_combo, True, True, 0)
        self.add_button = Gtk.Button.new_with_mnemonic(_("Add Key to _Agent…"))
        self.add_button.connect("clicked", lambda *_a: self.add_key())
        row.pack_start(self.add_button, False, False, 0)
        refresh = Gtk.Button.new_with_mnemonic(_("_Refresh"))
        refresh.connect("clicked", lambda *_a: self.refresh())
        row.pack_start(refresh, False, False, 0)
        self.add_widget(row)

        self.result_label = Gtk.Label()
        self.result_label.set_xalign(0.0)
        self.result_label.set_line_wrap(True)
        self.result_label.get_style_context().add_class("form-help")
        self.add_widget(self.result_label)
        self.refresh()

    def refresh(self) -> None:
        status = K.agent_status()
        if status.reachable:
            self.status_label.set_text(ngettext("An ssh-agent is running with {n} key.",
                                                "An ssh-agent is running with {n} keys.", status.keys).format(
                n=status.keys))
        else:
            self.status_label.set_text(status.detail)
        self.infos = {str(info.path): info for info in self.controller.keys()}
        current = self.key_combo.get_active_id()
        self.key_combo.remove_all()
        for path, info in self.infos.items():
            self.key_combo.append(path, f"{info.name} ({info.type_label})")
        if not (current and self.key_combo.set_active_id(current)):
            self.key_combo.set_active(0)
        self.add_button.set_sensitive(status.reachable and bool(self.infos) and not self.busy)

    def selected(self) -> Optional[K.KeyInfo]:
        return self.infos.get(self.key_combo.get_active_id() or "")

    def _ask_passphrase(self, info: K.KeyInfo) -> Optional[str]:
        from .ssh_keys import ask_passphrase

        def check(text: str) -> bool:
            try:
                K.derive_public_line(info.path, text)
                return True
            except K.KeyError_:
                return False

        return ask_passphrase(self.parent_window, _("Passphrase of {name}").format(name=info.name), check)

    def add_key(self) -> bool:
        """Start ``ssh-add`` for the chosen key in a worker; ``False`` when nothing was started."""

        info = self.selected()
        if info is None or self.busy:
            return False
        passphrase = ""
        if info.encrypted:
            passphrase = self.controller.saved_passphrase(info.path) or self.ask_passphrase(info) or ""
            if not passphrase:
                return False
        self.busy = True
        self.add_button.set_sensitive(False)
        self.result_label.set_text(_("Adding {name}…").format(name=info.name))

        def work() -> None:
            ok, message = K.agent_add(info.path, passphrase)
            GLib.idle_add(self._added, ok, message)

        threading.Thread(target=work, name="ssh-add", daemon=True).start()
        return True

    def _added(self, ok: bool, message: str) -> bool:
        self.busy = False
        self.result_label.set_text(message)
        self.last_result = (ok, message)
        self.refresh()
        return False


__all__ = ["AgentSection"]
