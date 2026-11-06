"""Port forwarding: the Tunnels manager and the add / edit dialog.

The manager is one ``Gtk.TreeView`` over a ``Gtk.TreeStore`` (hosts with
forwards, their forwards below; no widget per row): name, type, listening
address, destination, status (an icon **and** a word), connections, traffic,
and the last error in the tooltip. The toolbar acts on the selection:
Start / Stop, Edit, Delete, Copy Address, Open in Browser; Add Forward picks
the host in a searchable list; Stop All; a filter.
"""

from __future__ import annotations

import webbrowser
from typing import Callable, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk, Pango  # type: ignore  # noqa: E402

from ...tunnels import engine as E  # noqa: E402
from ...tunnels.model import DYNAMIC, KINDS, LOCAL, LOOPBACK, REMOTE, Forward, kind_label, validate  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.forms import FormSection  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402
from .confirm import confirm  # noqa: E402

(COL_NAME, COL_TYPE, COL_LISTEN, COL_DEST, COL_STATUS_ICON, COL_STATUS, COL_CONNECTIONS, COL_TRAFFIC, COL_TOOLTIP,
 COL_HOST_KEY, COL_FORWARD_ID) = range(11)
WEB_PORTS = {80, 443, 3000, 5000, 8000, 8008, 8080, 8443, 8888, 9000}
STATUS_ICONS = {E.ACTIVE: "emblem-ok-symbolic", E.STARTING: "content-loading-symbolic",
                E.RECONNECTING: "view-refresh-symbolic", E.FAILED: "dialog-error-symbolic",
                E.STOPPED: "media-playback-stop-symbolic"}


def explanation(kind: str) -> Tuple[str, str]:
    """One line and a small picture of where the connection goes."""

    return {
        LOCAL: (_("A port on this computer reaches a service the server can reach (ssh -L)."),
                _("your PC :port  →  server  →  destination :port")),
        REMOTE: (_("A port on the server reaches a service this computer can reach (ssh -R)."),
                 _("server :port  →  your PC  →  destination :port")),
        DYNAMIC: (_("A SOCKS5 proxy on this computer: programs set to use it connect from the server (ssh -D)."),
                  _("your PC :port (SOCKS5)  →  server  →  any address")),
    }[kind]


def traffic(status: E.TunnelStatus) -> str:
    def size(value: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
            value /= 1024
        return str(value)

    if not (status.bytes_in or status.bytes_out):
        return ""
    return f"↓ {size(status.bytes_in)}  ↑ {size(status.bytes_out)}"


# ------------------------------------------------------------- the editor
class ForwardDialog(BaseDialog):
    """Add or edit one forward; OK stays disabled while the validation finds errors."""

    def __init__(self, parent: Optional[Gtk.Window], forward: Optional[Forward] = None, *, host_id: str = "",
                 others: Sequence[Tuple[str, Forward]] = (), host_label: str = "") -> None:
        title = _("Edit Forward") if forward else _("Add Forward")
        BaseDialog.__init__(self, parent, title, size=DialogSize.MEDIUM, resizable=False)
        self.original = forward
        self.host_id = host_id
        self.others = list(others)
        forward = forward or Forward()
        if host_label:
            label = Gtk.Label(label=_("Host: {host}").format(host=host_label))
            label.set_xalign(0.0)
            label.get_style_context().add_class("section-title")
            self.add_body(label)
        kinds = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.M)
        self.kind_buttons = {}
        group = None
        for kind in KINDS:
            button = Gtk.RadioButton.new_with_label_from_widget(group, kind_label(kind))
            button.connect("toggled", lambda *_a: self._update())
            kinds.pack_start(button, False, False, 0)
            self.kind_buttons[kind] = button
            group = group or button
        self.add_body(kinds)
        self.explain = Gtk.Label()
        self.explain.set_xalign(0.0)
        self.explain.set_line_wrap(True)
        self.add_body(self.explain)
        self.picture = Gtk.Label()
        self.picture.set_xalign(0.0)
        self.picture.get_style_context().add_class("mono")
        self.add_body(self.picture)

        section = FormSection()
        self.name = Gtk.Entry(text=forward.name)
        section.add(_("_Name"), self.name)
        self.bind_host = Gtk.Entry(text=forward.bind_host or LOOPBACK)
        self.bind_port = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.bind_port.set_value(forward.bind_port or 8080)
        self.bind_host_row = section.add(_("_Listen on"), self.bind_host)
        section.add(_("Listening _port"), self.bind_port)
        self.dest_host = Gtk.Entry(text=forward.dest_host)
        self.dest_host.set_placeholder_text(_("resolved by the server (local) or this computer (remote)"))
        self.dest_port = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.dest_port.set_value(forward.dest_port or 80)
        self.dest_host_row = section.add(_("_Destination"), self.dest_host)
        self.dest_port_row = section.add(_("Destination p_ort"), self.dest_port)
        self.add_body(section)
        self.allow_remote = Gtk.CheckButton.new_with_mnemonic(_("_Bind to all interfaces (other computers can use it)"))
        self.allow_remote.set_active(forward.allow_remote_bind)
        self.add_body(self.allow_remote)
        self.with_session = Gtk.CheckButton.new_with_mnemonic(_("Start with a _session of this host"))
        self.with_session.set_active(forward.auto_start_with_session)
        self.with_app = Gtk.CheckButton.new_with_mnemonic(_("Start with the _app"))
        self.with_app.set_active(forward.auto_start_with_app)
        self.stop_last = Gtk.CheckButton.new_with_mnemonic(_("Stop when the _last session of this host closes"))
        self.stop_last.set_active(bool(forward.stop_with_last_session))
        for widget in (self.with_session, self.with_app, self.stop_last):
            self.add_body(widget)
        self.note = Gtk.Entry(text=forward.note)
        self.note.set_placeholder_text(_("Note"))
        set_accessible_name(self.note, _("Note"))
        self.add_body(self.note)
        self.messages = Gtk.Label()
        self.messages.set_xalign(0.0)
        self.messages.set_line_wrap(True)
        self.add_body(self.messages)
        self.add_cancel()
        self.add_primary(_("_Save"))
        self.kind_buttons[forward.kind if forward.kind in KINDS else LOCAL].set_active(True)
        for widget in (self.name, self.bind_host, self.dest_host):
            widget.connect("changed", lambda *_a: self._update())
        for widget in (self.bind_port, self.dest_port):
            widget.connect("value-changed", lambda *_a: self._update())
        self.allow_remote.connect("toggled", lambda *_a: self._update())
        self._stop_touched = False  # the user's own choice wins; until then it follows "with a session"
        self._syncing = False
        self.stop_last.connect("toggled", self._on_stop_toggled)
        self.with_session.connect("toggled", self._on_with_session_toggled)
        self._update()

    def _on_stop_toggled(self, _button) -> None:
        if not self._syncing:
            self._stop_touched = True

    def _on_with_session_toggled(self, button) -> None:
        if not self._stop_touched:
            self._syncing = True
            self.stop_last.set_active(button.get_active())
            self._syncing = False
        self._update()

    @property
    def kind(self) -> str:
        return next(kind for kind, button in self.kind_buttons.items() if button.get_active())

    def forward(self) -> Forward:
        base = self.original or Forward()
        return Forward(id=base.id, extra=dict(base.extra), name=self.name.get_text().strip(), kind=self.kind,
                       bind_host=self.bind_host.get_text().strip() or LOOPBACK,
                       bind_port=self.bind_port.get_value_as_int(),
                       dest_host="" if self.kind == DYNAMIC else self.dest_host.get_text().strip(),
                       dest_port=0 if self.kind == DYNAMIC else self.dest_port.get_value_as_int(),
                       auto_start_with_session=self.with_session.get_active(),
                       auto_start_with_app=self.with_app.get_active(),
                       stop_with_last_session=self.stop_last.get_active(), note=self.note.get_text().strip(),
                       allow_remote_bind=self.allow_remote.get_active())

    def validation(self):
        return validate(self.forward(), host_id=self.host_id, others=self.others)

    def _update(self) -> None:
        kind = self.kind
        line, picture = explanation(kind)
        self.explain.set_text(line)
        self.picture.set_text(picture)
        dynamic = kind == DYNAMIC
        for widget in (self.dest_host, self.dest_port):
            widget.set_sensitive(not dynamic)
        result = self.validation()
        lines = [f"✖ {e}" for e in result.errors] + [f"⚠ {w}" for w in result.warnings]
        self.messages.set_text("\n".join(lines))
        context = self.messages.get_style_context()
        for css in ("form-error", "form-help"):
            context.remove_class(css)
        context.add_class("form-error" if result.errors else "form-help")
        self.primary_button.set_sensitive(result.ok)

    def ask(self) -> Optional[Forward]:
        self.show_all()
        try:  # read the fields before the dialog is destroyed
            response = self.run()
            return self.forward() if is_accept(response) and self.validation().ok else None
        finally:
            self.destroy()


class HostPickerDialog(BaseDialog):
    """Choose the SSH host of a new forward (searchable; never a giant combo box)."""

    def __init__(self, parent: Optional[Gtk.Window], hosts: Sequence[Tuple[str, str]]) -> None:
        from ..widgets.searchable_list import SearchableList

        BaseDialog.__init__(self, parent, _("Add Forward"), size=DialogSize.SMALL)
        # ``hosts`` are (alias, key) pairs; the list takes (value, label): the key is returned, the alias shown.
        self.list = SearchableList([(key, alias) for alias, key in hosts], search_label=_("Search hosts"), list_label=_("Hosts"),
                                   placeholder=_("Search hosts"), on_activate=lambda: self.response(Gtk.ResponseType.OK))
        self.add_body(self.list, expand=True)
        self.add_cancel()
        self.add_primary(_("_Next"))

    def ask(self) -> Optional[str]:
        self.show_all()
        try:  # the selection is gone once the dialog is destroyed
            chosen = self.list.selected() if is_accept(self.run()) else None
        finally:
            self.destroy()
        return chosen[0] if chosen and chosen[0] else None


# ------------------------------------------------------------ the manager
class TunnelsDialog(BaseDialog):
    def __init__(self, parent: Optional[Gtk.Window], controller) -> None:
        BaseDialog.__init__(self, parent, _("Tunnels"), size=DialogSize.LARGE, height=520)
        self.controller = controller
        # Test hooks.
        self.edit_forward: Callable[..., Optional[Forward]] = \
            lambda forward, host: ForwardDialog(self, forward, host_id=controller.host_key(host),
                                                others=controller.all_forwards(), host_label=host.alias).ask()
        self.pick_host: Callable[[], Optional[str]] = \
            lambda: HostPickerDialog(self, [(h.alias, controller.host_key(h)) for h in controller.hosts()]).ask()
        self.confirm: Callable[[str, str], bool] = \
            lambda title, text: confirm(self, title, text, confirm_label=_("_Delete"), destructive=True)

        # Two lines (the forward, then the address and all): on one line the toolbar was 1017 px wide.
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        second = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.buttons = {}

        def button(name: str, label: str, callback: Callable[[], object]) -> Gtk.Button:
            widget = Gtk.Button.new_with_mnemonic(label)
            widget.connect("clicked", lambda *_a: callback())
            (second if name in ("copy", "browser", "stop_all") else toolbar).pack_start(widget, False, False, 0)
            self.buttons[name] = widget
            return widget

        button("add", _("_Add Forward…"), self.add)
        button("toggle", _("_Start"), self.toggle)
        button("edit", _("_Edit…"), self.edit)
        button("delete", _("_Delete…"), self.delete)
        button("copy", _("_Copy Address"), self.copy_address)
        button("browser", _("Open in _Browser"), self.open_browser)
        button("stop_all", _("Stop A_ll"), self.stop_all)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text(_("Filter"))
        set_accessible_name(self.search, _("Filter tunnels"))
        self.search.connect("search-changed", lambda *_a: self.refresh())
        toolbar.pack_end(self.search, False, False, 0)
        self.add_body(toolbar)
        self.add_body(second)

        self.store = Gtk.TreeStore(str, str, str, str, str, str, str, str, str, str, str)
        self.tree = Gtk.TreeView(model=self.store)
        self.tree.set_tooltip_column(COL_TOOLTIP)
        set_accessible_name(self.tree, _("Port forwards"))
        name_column = Gtk.TreeViewColumn(_("Name"))
        cell = Gtk.CellRendererText()
        cell.set_property("ellipsize", Pango.EllipsizeMode.END)
        name_column.pack_start(cell, True)
        name_column.add_attribute(cell, "text", COL_NAME)
        name_column.set_expand(True)
        self.tree.append_column(name_column)
        for title, column in ((_("Type"), COL_TYPE), (_("Listen"), COL_LISTEN), (_("Destination"), COL_DEST)):
            renderer = Gtk.CellRendererText()
            self.tree.append_column(Gtk.TreeViewColumn(title, renderer, text=column))
        status = Gtk.TreeViewColumn(_("Status"))
        icon = Gtk.CellRendererPixbuf()
        status.pack_start(icon, False)
        status.add_attribute(icon, "icon-name", COL_STATUS_ICON)
        text = Gtk.CellRendererText()
        status.pack_start(text, True)
        status.add_attribute(text, "text", COL_STATUS)  # a word, never colour alone
        self.tree.append_column(status)
        for title, column in ((_("Connections"), COL_CONNECTIONS), (_("Traffic"), COL_TRAFFIC)):
            self.tree.append_column(Gtk.TreeViewColumn(title, Gtk.CellRendererText(), text=column))
        self.tree.get_selection().connect("changed", lambda *_a: self._sync_buttons())
        self.tree.connect("row-activated", lambda *_a: self.toggle())
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.tree)
        self.add_body(scroller, expand=True)
        self.empty = Gtk.Label(label=_("No forwards yet. Add one, or open a host's Port Forwarding page."))
        self.empty.get_style_context().add_class("dim-label")
        self.empty.set_no_show_all(True)
        self.add_body(self.empty)
        self.add_primary(_("_Close"), Gtk.ResponseType.CLOSE)
        controller.listeners.append(self._on_changed)
        self.connect("destroy", lambda *_a: controller.listeners.remove(self._on_changed)
                     if self._on_changed in controller.listeners else None)
        self.refresh()

    # ------------------------------------------------------------- rows
    def _on_changed(self) -> None:
        self.update_status()

    def refresh(self) -> None:
        """Rebuild the rows (hosts with forwards only), keeping the selection."""

        selected = self.selected()
        keep = (self.controller.host_key(selected[0]), selected[1].id) if selected and selected[1] else None
        needle = self.search.get_text().strip().lower()
        self.store.clear()
        count = 0
        for host, forwards in self.controller.hosts_with_forwards():
            key = self.controller.host_key(host)
            rows = [f for f in forwards if not needle or needle in " ".join(
                (host.alias, f.title, f.listen_address, f.destination, kind_label(f.kind))).lower()]
            if not rows:
                continue
            parent = self.store.append(None, [host.alias, "", "", "", "", "", "", "", "", key, ""])
            for forward in rows:
                child = self.store.append(parent, self._row(key, forward))
                count += 1
                if keep == (key, forward.id):
                    self.tree.expand_all()
                    self.tree.get_selection().select_iter(child)
        self.tree.expand_all()
        self.empty.set_visible(count == 0)
        self._sync_buttons()

    def _row(self, key: str, forward: Forward) -> list:
        status = self.controller.manager.status(key, forward.id)
        tooltip = status.error or forward.note or ""
        connections = str(status.connections) if status.state == E.ACTIVE else ""
        return [forward.title, kind_label(forward.kind), forward.listen_address, forward.destination,
                STATUS_ICONS.get(status.state, ""), E.state_label(status), connections, traffic(status),
                tooltip, key, forward.id]

    def update_status(self) -> None:
        for parent in self.store:
            for row in parent.iterchildren():
                host = self.controller.host(row[COL_HOST_KEY])
                forward = self._forward(host, row[COL_FORWARD_ID]) if host is not None else None
                if forward is not None:
                    values = self._row(row[COL_HOST_KEY], forward)
                    for column in (COL_STATUS_ICON, COL_STATUS, COL_CONNECTIONS, COL_TRAFFIC, COL_TOOLTIP):
                        if row[column] != values[column]:
                            row[column] = values[column]
        self._sync_buttons()

    @staticmethod
    def _forward(host, forward_id: str) -> Optional[Forward]:
        from ...tunnels.model import forwards_from_metadata

        return next((f for f in forwards_from_metadata(host.metadata) if f.id == forward_id), None)

    def selected(self) -> Optional[Tuple[object, Optional[Forward]]]:
        model, tree_iter = self.tree.get_selection().get_selected()
        if tree_iter is None:
            return None
        host = self.controller.host(model[tree_iter][COL_HOST_KEY])
        if host is None:
            return None
        forward_id = model[tree_iter][COL_FORWARD_ID]
        return host, (self._forward(host, forward_id) if forward_id else None)

    def select(self, key: str, forward_id: str) -> bool:
        for parent in self.store:
            for row in parent.iterchildren():
                if (row[COL_HOST_KEY], row[COL_FORWARD_ID]) == (key, forward_id):
                    self.tree.get_selection().select_iter(row.iter)
                    return True
        return False

    def _sync_buttons(self) -> None:
        selected = self.selected()
        forward = selected[1] if selected else None
        running = bool(forward and self.controller.status(selected[0], forward).running)
        self.buttons["toggle"].set_label(_("_Stop") if running else _("_Start"))
        for name in ("toggle", "edit", "delete", "copy"):
            self.buttons[name].set_sensitive(forward is not None)
        self.buttons["browser"].set_sensitive(bool(forward and forward.kind == LOCAL and running
                                                   and forward.bind_port in WEB_PORTS))
        self.buttons["stop_all"].set_sensitive(self.controller.running_count() > 0)

    # ---------------------------------------------------------- actions
    def toggle(self) -> None:
        selected = self.selected()
        if not selected or selected[1] is None:
            return
        host, forward = selected
        if self.controller.status(host, forward).running:
            self.controller.stop(host, forward)
        else:
            self.controller.start(host, forward)
        self.update_status()

    def add(self) -> Optional[Forward]:
        key = self.pick_host()
        host = self.controller.host(key) if key else None
        if host is None:
            return None
        forward = self.edit_forward(None, host)
        if forward is None:
            return None
        from ...tunnels.model import forwards_from_metadata

        self.controller.save(host, forwards_from_metadata(host.metadata) + [forward])
        self.refresh()
        self.select(key, forward.id)
        return forward

    def edit(self) -> Optional[Forward]:
        selected = self.selected()
        if not selected or selected[1] is None:
            return None
        host, forward = selected
        edited = self.edit_forward(forward, host)
        if edited is None:
            return None
        from ...tunnels.model import forwards_from_metadata

        forwards = [edited if f.id == forward.id else f for f in forwards_from_metadata(host.metadata)]
        running = self.controller.status(host, forward).running
        self.controller.save(host, forwards)
        if running:  # the new settings apply now
            self.controller.start(host, edited)
        self.refresh()
        return edited

    def delete(self) -> bool:
        selected = self.selected()
        if not selected or selected[1] is None:
            return False
        host, forward = selected
        if not self.confirm(_("Delete the forward ‘{name}’?").format(name=forward.title),
                            _("It is stopped and removed from {host}.").format(host=host.alias)):
            return False
        from ...tunnels.model import forwards_from_metadata

        self.controller.stop(host, forward)
        self.controller.save(host, [f for f in forwards_from_metadata(host.metadata) if f.id != forward.id])
        self.refresh()
        return True

    def copy_address(self) -> str:
        selected = self.selected()
        if not selected or selected[1] is None:
            return ""
        text = selected[1].copy_address
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        return text

    def open_browser(self) -> None:
        selected = self.selected()
        if selected and selected[1] is not None:
            scheme = "https" if selected[1].bind_port in (443, 8443) else "http"
            host = "localhost" if selected[1].bind_host in (LOOPBACK, "0.0.0.0") else selected[1].bind_host
            webbrowser.open(f"{scheme}://{host}:{selected[1].bind_port}/")

    def stop_all(self) -> None:
        self.controller.stop_all()
        self.update_status()

    def ask(self) -> None:
        self.run_and_destroy()


__all__ = ["TunnelsDialog", "ForwardDialog", "HostPickerDialog", "explanation", "traffic"]
