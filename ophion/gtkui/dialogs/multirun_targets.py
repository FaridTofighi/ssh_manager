"""Run Command → Change targets: groups and hosts with check boxes, search, tag and protocol filters.

One ``Gtk.TreeStore`` (parent groups → child groups → hosts) behind a
``Gtk.TreeModelFilter``; visibility is computed once per keystroke into a set,
so 1,200 hosts in 1,000 groups stay quick. No widget per host.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # type: ignore  # noqa: E402

from ..i18n import _, ngettext  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402

COL_CHECKED, COL_LABEL, COL_KIND, COL_KEY, COL_SEARCH, COL_PROTOCOL, COL_TAGS, COL_MIXED, COL_DETAIL = range(9)
GROUP, HOST = "group", "host"


class TargetChooserDialog(BaseDialog):
    def __init__(self, parent: Optional[Gtk.Window], tree: Dict[str, Dict[str, object]],
                 selected: Iterable[str] = (), key=lambda host: host.alias) -> None:
        BaseDialog.__init__(self, parent, _("Choose Hosts"), size=DialogSize.MEDIUM, height=560)
        self.selected: Set[str] = set(selected)
        self._visible: Optional[Set[str]] = None  # row ids that pass the filters (None: everything)

        filters = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text(_("Search hosts"))
        set_accessible_name(self.search, _("Search hosts"))
        self.search.connect("search-changed", lambda *_a: self.refilter())
        filters.pack_start(self.search, True, True, 0)
        self.search.set_width_chars(10)  # the entries expand; their default 150 px minimum widened the dialog
        self.tag = Gtk.Entry()
        self.tag.set_placeholder_text(_("Tag"))
        self.tag.set_width_chars(8)
        set_accessible_name(self.tag, _("Only hosts with this tag"))
        self.tag.connect("changed", lambda *_a: self.refilter())
        filters.pack_start(self.tag, False, False, 0)
        self.ssh_only = Gtk.CheckButton.new_with_mnemonic(_("_SSH hosts only"))
        self.ssh_only.set_active(True)
        self.ssh_only.connect("toggled", lambda *_a: self.refilter())
        filters.pack_start(self.ssh_only, False, False, 0)
        self.add_body(filters)

        self.store = Gtk.TreeStore(bool, str, str, str, str, str, str, bool, str)
        self._build(tree, key)
        self.filtered = self.store.filter_new()
        self.filtered.set_visible_func(self._is_visible)
        self.view = Gtk.TreeView(model=self.filtered)
        self.view.set_headers_visible(False)
        self.view.set_enable_search(False)
        set_accessible_name(self.view, _("Hosts and groups"))
        toggle = Gtk.CellRendererToggle()
        toggle.connect("toggled", self._on_toggled)
        column = Gtk.TreeViewColumn("", toggle, active=COL_CHECKED, inconsistent=COL_MIXED)
        self.view.append_column(column)
        text = Gtk.CellRendererText()
        text.set_property("ellipsize", Pango.EllipsizeMode.END)
        name_column = Gtk.TreeViewColumn("", text, text=COL_LABEL)
        name_column.set_expand(True)
        self.view.append_column(name_column)
        detail = Gtk.CellRendererText()
        detail.set_property("style", Pango.Style.ITALIC)
        self.view.append_column(Gtk.TreeViewColumn("", detail, text=COL_DETAIL))
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.set_min_content_height(320)
        scroller.add(self.view)
        self.add_body(scroller, expand=True)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        select = Gtk.Button.new_with_mnemonic(_("Select _All Matches"))
        select.connect("clicked", lambda *_a: self.select_matches(True))
        clear = Gtk.Button.new_with_mnemonic(_("C_lear Matches"))
        clear.connect("clicked", lambda *_a: self.select_matches(False))
        actions.pack_start(select, False, False, 0)
        actions.pack_start(clear, False, False, 0)
        self.count = Gtk.Label()
        self.count.set_xalign(1.0)
        actions.pack_end(self.count, False, False, 0)
        self.add_body(actions)
        self.add_cancel()
        self.add_primary(_("_Use These Hosts"))
        self.refilter()

    # ------------------------------------------------------------- model
    def _build(self, tree: Dict[str, Dict[str, object]], key) -> None:
        self._hosts: Dict[str, Gtk.TreeIter] = {}
        for parent_name in sorted(tree, key=str.casefold):
            parent = self.store.append(None, [False, parent_name, GROUP, f"g:{parent_name}", parent_name.lower(),
                                              "", "", False, ""])
            for child_name in sorted(tree[parent_name], key=str.casefold):
                group = tree[parent_name][child_name]
                holder = parent
                if child_name:
                    holder = self.store.append(parent, [False, child_name, GROUP, f"g:{parent_name}/{child_name}",
                                                        child_name.lower(), "", "", False, ""])
                for host in sorted(getattr(group, "hosts", []), key=lambda h: h.alias.casefold()):
                    host_key = key(host)
                    hostname = host.option("hostname") or ""
                    protocol = getattr(host, "protocol", "ssh") or "ssh"
                    tags = (host.option("tags") or "").lower()
                    detail = hostname if protocol == "ssh" else f"{hostname} · {protocol}"
                    self._hosts[host_key] = self.store.append(holder, [
                        host_key in self.selected, host.alias, HOST, host_key,
                        f"{host.alias} {hostname}".lower(), protocol, f",{tags.replace(' ', '')},", False, detail])
        self._update_groups()

    def _update_groups(self) -> None:
        def walk(tree_iter) -> tuple:
            total = checked = 0
            child = self.store.iter_children(tree_iter)
            while child is not None:
                if self.store[child][COL_KIND] == HOST:
                    total += 1
                    checked += bool(self.store[child][COL_CHECKED])
                else:
                    sub_total, sub_checked = walk(child)
                    total += sub_total
                    checked += sub_checked
                child = self.store.iter_next(child)
            if tree_iter is not None:
                self.store[tree_iter][COL_CHECKED] = total > 0 and checked == total
                self.store[tree_iter][COL_MIXED] = 0 < checked < total
            return total, checked

        walk(None)

    # ----------------------------------------------------------- filters
    def _host_passes(self, row) -> bool:
        needle = self.search.get_text().strip().lower()
        tag = self.tag.get_text().strip().lower()
        if needle and needle not in row[COL_SEARCH]:
            return False
        if tag and f",{tag}," not in row[COL_TAGS]:
            return False
        return not (self.ssh_only.get_active() and row[COL_PROTOCOL] != "ssh")

    def refilter(self) -> None:
        filtering = bool(self.search.get_text().strip() or self.tag.get_text().strip() or self.ssh_only.get_active())
        if not filtering:
            self._visible = None
        else:
            visible: Set[str] = set()
            for host_iter in self._hosts.values():
                row = self.store[host_iter]
                if self._host_passes(row):
                    visible.add(row[COL_KEY])
                    parent = self.store.iter_parent(host_iter)
                    while parent is not None:
                        visible.add(self.store[parent][COL_KEY])
                        parent = self.store.iter_parent(parent)
            self._visible = visible
        self.filtered.refilter()
        if self.search.get_text().strip() or self.tag.get_text().strip():
            self.view.expand_all()
        self._update_count()

    def _is_visible(self, model, tree_iter, _data=None) -> bool:
        return self._visible is None or model[tree_iter][COL_KEY] in self._visible

    def matches(self) -> List[str]:
        return [key for key in self._hosts if self._visible is None or key in self._visible]

    def select_matches(self, checked: bool) -> None:
        for key in self.matches():
            self._set(key, checked)
        self._update_groups()
        self._update_count()

    # ------------------------------------------------------------ toggles
    def _set(self, key: str, checked: bool) -> None:
        self.store[self._hosts[key]][COL_CHECKED] = checked
        if checked:
            self.selected.add(key)
        else:
            self.selected.discard(key)

    def _on_toggled(self, _cell, path: str) -> None:
        row = self.filtered[Gtk.TreePath.new_from_string(path)]
        store_iter = self.filtered.convert_iter_to_child_iter(row.iter)
        target = not (self.store[store_iter][COL_CHECKED] and not self.store[store_iter][COL_MIXED])
        if self.store[store_iter][COL_KIND] == HOST:
            self._set(self.store[store_iter][COL_KEY], target)
        else:  # a group: every visible host below it
            def walk(tree_iter) -> None:
                child = self.store.iter_children(tree_iter)
                while child is not None:
                    if self.store[child][COL_KIND] == HOST:
                        if self._visible is None or self.store[child][COL_KEY] in self._visible:
                            self._set(self.store[child][COL_KEY], target)
                    else:
                        walk(child)
                    child = self.store.iter_next(child)

            walk(store_iter)
        self._update_groups()
        self._update_count()

    def toggle_key(self, key: str) -> None:
        self._set(key, key not in self.selected)
        self._update_groups()
        self._update_count()

    def _update_count(self) -> None:
        self.count.set_text(ngettext("{n} host selected", "{n} hosts selected", len(self.selected)).format(
            n=len(self.selected)))

    def ask(self) -> Optional[Set[str]]:
        response = self.run_and_destroy()
        return set(self.selected) if is_accept(response) else None


__all__ = ["TargetChooserDialog"]
