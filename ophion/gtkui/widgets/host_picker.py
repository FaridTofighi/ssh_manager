"""A compact host chooser for long lists: a button that opens a searchable popover list.

Used by the RDP "SSH tunnel" row when the inventory has more SSH hosts than a
combo box can hold comfortably (:data:`COMBO_LIMIT`): ``Gtk.ComboBoxText``
measures every item (3.6 s for 4,000 hosts). The list is one
``Gtk.ListStore`` behind a filter (:class:`~.searchable_list.SearchableList`),
built once. ``get_active_id`` / ``set_active_id`` work as on the combo.
"""

from __future__ import annotations

from typing import Optional, Sequence, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # type: ignore  # noqa: E402

from ..i18n import _, ngettext  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from .icon_button import set_accessible_name  # noqa: E402
from .searchable_list import SearchableList  # noqa: E402

COMBO_LIMIT = 30  # up to this many hosts the combo box stays


class HostPicker(Gtk.MenuButton):
    """``[selected label ▾]`` → ``[search] / [hosts]``; the first row (``first``) is always shown."""

    def __init__(self, hosts: Sequence[Tuple[str, str]], *, first: Tuple[str, str], active: str = "",
                 accessible_name: str = "") -> None:
        Gtk.MenuButton.__init__(self)
        self._labels = {first[0]: first[1]}
        self._labels.update(hosts)
        self._active = first[0]
        self.label = Gtk.Label(xalign=0.0)
        self.label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.label.set_width_chars(24)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.XS)
        box.pack_start(self.label, True, True, 0)
        box.pack_end(Gtk.Image.new_from_icon_name("pan-down-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        self.add(box)
        set_accessible_name(self, accessible_name or _("Host"))

        self.list = SearchableList(
            [first, *hosts], pinned=(first[0],), placeholder=_("Search hosts"),
            search_label=_("Search hosts"), list_label=accessible_name or _("Hosts"),
            on_activate=self._choose,
            count_text=self._count_text)
        self.list.view.set_activate_on_single_click(True)
        self.list.set_size_request(360, -1)
        self.list.set_margin_start(Space.S)
        self.list.set_margin_end(Space.S)
        self.list.set_margin_top(Space.S)
        self.list.set_margin_bottom(Space.S)
        self.popover = Gtk.Popover()
        self.popover.add(self.list)
        self.list.show_all()
        self.set_popover(self.popover)
        self.popover.connect("show", lambda *_a: self._on_show())
        self.set_active_id(active)

    @staticmethod
    def _count_text(shown: int, total: int, filtered: bool) -> str:
        shown, total = shown - 1, total - 1  # the first row ("Connect directly") is not a host
        if filtered:
            return ngettext("{n} of {total} host", "{n} of {total} hosts", total).format(n=shown, total=total)
        return ngettext("{n} host", "{n} hosts", shown).format(n=shown)

    def _on_show(self) -> None:
        self.list.entry.set_text("")
        self.list.refilter(select=self._active)
        self.list.entry.grab_focus()

    def _choose(self) -> None:
        chosen = self.list.selected()
        if chosen is not None:
            self.set_active_id(chosen[0])
        self.popover.popdown()

    def add_host(self, host_id: str, label: str) -> None:
        """One more entry (e.g. a stale id), at the end of the list."""

        self._labels[host_id] = label
        self.list.store.append([label, host_id, False])

    def get_active_id(self) -> Optional[str]:
        return self._active

    def set_active_id(self, host_id: str) -> bool:
        """Select ``host_id`` (as ``Gtk.ComboBox.set_active_id``: ``False`` when it is not in the list)."""

        if host_id not in self._labels:
            return False
        self._active = host_id
        self.label.set_text(self._labels[host_id])
        self.set_tooltip_text(self._labels[host_id])
        return True


__all__ = ["HostPicker", "COMBO_LIMIT"]
