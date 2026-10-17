"""Reusable GTK password dialog for SSH authentication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib  # type: ignore  # noqa: E402


class HostDetails(Protocol):
    host: str
    port: int
    username: str
    remember_password_default: bool
    parent_window: Optional[Gtk.Window]


@dataclass
class PasswordDialog(Gtk.Dialog):
    """Modal password dialog reused across authentication attempts."""

    def __init__(self) -> None:
        super().__init__(modal=True)
        self.set_destroy_with_parent(False)
        self.set_resizable(False)
        self.set_border_width(12)

        content = self.get_content_area()
        content.set_spacing(10)

        grid = Gtk.Grid(column_spacing=8, row_spacing=6)
        content.add(grid)

        self._username_entry = Gtk.Entry()
        self._username_entry.set_editable(False)
        self._username_entry.set_can_focus(False)

        self._password_entry = Gtk.Entry()
        self._password_entry.set_visibility(False)
        self._password_entry.set_invisible_char("•")
        self._password_entry.connect("changed", self._on_password_changed)
        self._password_entry.connect("activate", self._on_activate_connect)

        self._remember_check = Gtk.CheckButton(label="Remember password for this host")

        self._error_label = Gtk.Label()
        self._error_label.set_halign(Gtk.Align.START)
        self._error_label.set_no_show_all(True)
        self._error_label.set_line_wrap(True)
        self._error_label.get_style_context().add_class("error")

        self._connect_button = Gtk.Button.new_with_label("Connect")
        self._connect_button.set_can_default(True)
        self._connect_button.connect("clicked", self._on_connect_clicked)

        cancel_button = Gtk.Button.new_with_label("Cancel")
        cancel_button.connect("clicked", self._on_cancel_clicked)

        row = 0
        grid.attach(Gtk.Label(label="Username:", halign=Gtk.Align.END), 0, row, 1, 1)
        grid.attach(self._username_entry, 1, row, 1, 1)
        row += 1

        grid.attach(Gtk.Label(label="Password:", halign=Gtk.Align.END), 0, row, 1, 1)
        grid.attach(self._password_entry, 1, row, 1, 1)
        row += 1

        grid.attach(self._remember_check, 1, row, 1, 1)
        row += 1

        grid.attach(self._error_label, 0, row, 2, 1)
        row += 1

        button_box = Gtk.Box(spacing=6)
        button_box.set_halign(Gtk.Align.END)
        button_box.pack_start(cancel_button, False, False, 0)
        button_box.pack_start(self._connect_button, False, False, 0)
        grid.attach(button_box, 0, row, 2, 1)

        self._response_password: Optional[str] = None
        self._response_remember: bool = False
        self._connect_button.set_sensitive(False)
        self._connect_button.grab_default()

    def show_for(
        self,
        host_cfg: HostDetails,
        *,
        error_message: Optional[str] = None,
    ) -> Tuple[Optional[str], bool]:
        parent = getattr(host_cfg, "parent_window", None)
        if parent and isinstance(parent, Gtk.Window):
            self.set_transient_for(parent)
            self.set_position(Gtk.WindowPosition.CENTER_ON_PARENT)
        else:
            self.set_position(Gtk.WindowPosition.CENTER_ALWAYS)

        title = f"Authenticate to {host_cfg.username}@{host_cfg.host}:{host_cfg.port}"
        self.set_title(title)
        self._username_entry.set_text(host_cfg.username)
        self._password_entry.set_text("")
        self._password_entry.grab_focus()

        remember_default = bool(getattr(host_cfg, "remember_password_default", True))
        self._remember_check.set_active(remember_default)

        if error_message:
            self._error_label.set_text(error_message)
            self._error_label.show()
        else:
            self._error_label.hide()

        self._connect_button.set_sensitive(False)
        self._response_password = None
        self._response_remember = remember_default

        self.show_all()
        response = self.run()
        self.hide()

        password = self._response_password if response == Gtk.ResponseType.OK else None
        remember = self._response_remember if password is not None else False

        # Clear sensitive data promptly
        if password is not None:
            GLib.idle_add(self._password_entry.set_text, "")
        else:
            self._password_entry.set_text("")

        return password, remember

    # ------------------------------------------------------------------ signals
    def _on_password_changed(self, entry: Gtk.Entry) -> None:
        password = entry.get_text()
        self._connect_button.set_sensitive(bool(password))

    def _on_activate_connect(self, _entry: Gtk.Entry) -> None:
        if self._connect_button.get_sensitive():
            self._connect_button.emit("clicked")

    def _on_connect_clicked(self, _button: Gtk.Button) -> None:
        password = self._password_entry.get_text()
        if not password:
            return
        self._response_password = password
        self._response_remember = self._remember_check.get_active()
        self.response(Gtk.ResponseType.OK)

    def _on_cancel_clicked(self, _button: Gtk.Button) -> None:
        self._response_password = None
        self.response(Gtk.ResponseType.CANCEL)
