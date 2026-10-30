"""Host dialog → Port Forwarding: the host's managed forwards, and its own ``*Forward`` ssh options.

Built the first time the page is shown (it is not part of opening the
dialog). Managed forwards are edited with :class:`ForwardDialog`; the host's
``LocalForward`` / ``RemoteForward`` / ``DynamicForward`` ssh options are
shown read-only ("From SSH options (not managed)") with **Import as Managed
Forward**: the option itself is never removed here (the SSH Options page
owns it).
"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # type: ignore  # noqa: E402

from ...tunnels.model import Forward, kind_label, raw_forward_options  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402

COL_NAME, COL_TYPE, COL_LISTEN, COL_DEST, COL_AUTO, COL_INDEX = range(6)


class ForwardsPage(Gtk.Box):
    def __init__(self, parent: Optional[Gtk.Window], forwards: Sequence[Forward], *, host_id: str = "",
                 others: Sequence[Tuple[str, Forward]] = (), host_label: str = "",
                 ssh_options: Callable[[], dict] = dict) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        self.parent_window = parent
        self.items: List[Forward] = list(forwards)
        self.host_id = host_id
        self.others = [(h, f) for h, f in others if h != host_id]  # this host's forwards come from the page
        self.host_label = host_label
        self.ssh_options = ssh_options
        self.edit_forward: Callable[[Optional[Forward]], Optional[Forward]] = self._edit_forward  # test hook

        self.store = Gtk.ListStore(str, str, str, str, str, int)
        self.view = Gtk.TreeView(model=self.store)
        set_accessible_name(self.view, _("Port forwards of this host"))
        for title, column in ((_("Name"), COL_NAME), (_("Type"), COL_TYPE), (_("Listen"), COL_LISTEN),
                              (_("Destination"), COL_DEST), (_("Starts"), COL_AUTO)):
            cell = Gtk.CellRendererText()
            cell.set_property("ellipsize", Pango.EllipsizeMode.END)
            column_widget = Gtk.TreeViewColumn(title, cell, text=column)
            column_widget.set_expand(column in (COL_NAME, COL_DEST))
            self.view.append_column(column_widget)
        self.view.get_selection().connect("changed", lambda *_a: self._sync())
        self.view.connect("row-activated", lambda *_a: self.edit())
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.set_min_content_height(140)
        scroller.add(self.view)
        self.pack_start(scroller, True, True, 0)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.add_button = Gtk.Button.new_with_mnemonic(_("_Add Forward…"))
        self.edit_button = Gtk.Button.new_with_mnemonic(_("_Edit…"))
        self.remove_button = Gtk.Button.new_with_mnemonic(_("_Remove"))
        for widget, callback in ((self.add_button, self.add), (self.edit_button, self.edit),
                                 (self.remove_button, self.remove)):
            widget.connect("clicked", lambda _b, cb=callback: cb())
            buttons.pack_start(widget, False, False, 0)
        self.pack_start(buttons, False, False, 0)

        self.raw_title = Gtk.Label(label=_("From SSH options (not managed)"))
        self.raw_title.set_xalign(0.0)
        self.raw_title.get_style_context().add_class("section-title")
        self.pack_start(self.raw_title, False, False, 0)
        self.raw_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        self.pack_start(self.raw_box, False, False, 0)
        self.refresh()

    # --------------------------------------------------------------- rows
    def refresh(self) -> None:
        self.store.clear()
        for index, forward in enumerate(self.items):
            starts = ", ".join(word for flag, word in ((forward.auto_start_with_session, _("with a session")),
                                                       (forward.auto_start_with_app, _("with the app"))) if flag)
            self.store.append([forward.title, kind_label(forward.kind), forward.listen_address, forward.destination,
                               starts or _("manually"), index])
        self._refresh_raw()
        self._sync()

    def _refresh_raw(self) -> None:
        for child in self.raw_box.get_children():
            self.raw_box.remove(child)
            child.destroy()
        raw = raw_forward_options(self.ssh_options())
        self.raw_title.set_visible(bool(raw))
        self.raw_title.set_no_show_all(not raw)
        self.raw_buttons = []
        for key, value, parsed in raw:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
            label = Gtk.Label(label=f"{key} {value}")
            label.set_xalign(0.0)
            label.get_style_context().add_class("mono")
            row.pack_start(label, True, True, 0)
            if parsed is None:
                note = Gtk.Label(label=_("not understood; left as it is"))
                note.get_style_context().add_class("dim-label")
                row.pack_start(note, False, False, 0)
            else:
                button = Gtk.Button.new_with_label(_("Import as Managed Forward"))
                button.connect("clicked", lambda _b, f=parsed: self.import_option(f))
                row.pack_start(button, False, False, 0)
                self.raw_buttons.append(button)
            self.raw_box.pack_start(row, False, False, 0)
        self.raw_box.show_all()

    def _selected_index(self) -> int:
        model, tree_iter = self.view.get_selection().get_selected()
        return model[tree_iter][COL_INDEX] if tree_iter is not None else -1

    def _sync(self) -> None:
        selected = self._selected_index() >= 0
        self.edit_button.set_sensitive(selected)
        self.remove_button.set_sensitive(selected)

    # ------------------------------------------------------------ actions
    def _edit_forward(self, forward: Optional[Forward]) -> Optional[Forward]:
        from .tunnels import ForwardDialog

        others = self.others + [(self.host_id, f) for f in self.items]
        return ForwardDialog(self.parent_window, forward, host_id=self.host_id, others=others,
                             host_label=self.host_label).ask()

    def add(self) -> Optional[Forward]:
        forward = self.edit_forward(None)
        if forward is not None:
            self.items.append(forward)
            self.refresh()
        return forward

    def edit(self) -> Optional[Forward]:
        index = self._selected_index()
        if index < 0:
            return None
        forward = self.edit_forward(self.items[index])
        if forward is not None:
            self.items[index] = forward
            self.refresh()
        return forward

    def remove(self) -> None:
        index = self._selected_index()
        if index >= 0:
            del self.items[index]
            self.refresh()

    def import_option(self, parsed: Forward) -> Optional[Forward]:
        """Prefilled from the option; saved only after the editor's OK (the option itself stays)."""

        forward = self.edit_forward(parsed)
        if forward is not None:
            self.items.append(forward)
            self.refresh()
        return forward

    def forwards(self) -> List[Forward]:
        return list(self.items)


__all__ = ["ForwardsPage"]
