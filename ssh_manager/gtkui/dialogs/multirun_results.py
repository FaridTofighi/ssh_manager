"""Run Command → the results: a table per host, the selected host's output, grouping by output.

* **Table**: one row per host in a ``Gtk.ListStore`` (no widget per host):
  status (an icon and a word), host, group (lighter), exit code, duration,
  the first line of output. Rows are created once per run and updated in
  place from the engine's events. Filter chips: All / Failed / Succeeded.
* **Output**: the selected host's stdout and stderr (read-only, monospace,
  with a notice when truncated) and a search that highlights matches.
* **Group by output**: hosts with the same output collapse into "Same
  output on 31 hosts" rows (expand to see them); **Show Differences** gives
  a unified diff between a host's output and the majority's.
"""

from __future__ import annotations

from typing import Callable, Dict, Iterable, List, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # type: ignore  # noqa: E402

from ...multirun import engine as R  # noqa: E402
from ...multirun import report as P  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..theme.loader import current_palette  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402

(COL_KEY, COL_ICON, COL_STATUS, COL_ALIAS, COL_GROUP, COL_EXIT, COL_DURATION, COL_FIRST, COL_STATE,
 COL_SORT) = range(10)
G_LABEL, G_KEY, G_ICON = range(3)
ICONS = {R.QUEUED: "content-loading-symbolic", R.RUNNING: "media-playback-start-symbolic",
         R.SUCCESS: "emblem-ok-symbolic", R.FAILED: "dialog-error-symbolic", R.TIMEOUT: "alarm-symbolic",
         R.AUTH_FAILED: "dialog-password-symbolic", R.HOST_KEY_CHANGED: "dialog-warning-symbolic",
         R.HOST_KEY_UNKNOWN: "dialog-question-symbolic", R.UNREACHABLE: "network-offline-symbolic",
         R.CANCELLED: "process-stop-symbolic", R.SKIPPED: "action-unavailable-symbolic"}
FILTERS = (("all", _("All")), ("failed", _("Failed")), ("succeeded", _("Succeeded")))


def _text_view() -> Gtk.TextView:
    view = Gtk.TextView()
    view.set_editable(False)
    view.set_cursor_visible(False)
    view.set_monospace(True)
    view.set_wrap_mode(Gtk.WrapMode.NONE)
    palette = current_palette()  # theme colours, never literals
    view.get_buffer().create_tag("match", background=palette.warning, foreground=palette.bg_canvas)
    return view


def _scrolled(child: Gtk.Widget, height: int = 0) -> Gtk.ScrolledWindow:
    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    scroller.set_shadow_type(Gtk.ShadowType.IN)
    if height:
        scroller.set_min_content_height(height)
    scroller.add(child)
    return scroller


class ResultsView(Gtk.Box):
    def __init__(self) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        self.results: Dict[str, R.HostResult] = {}
        self._iters: Dict[str, Gtk.TreeIter] = {}
        self.show_filter = "all"
        self.show_diff: Callable[[str, str], None] = self._show_diff_dialog  # test hook

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        chips.get_style_context().add_class("linked")
        self.filter_buttons: Dict[str, Gtk.RadioButton] = {}
        group = None
        for name, label in FILTERS:
            button = Gtk.RadioButton.new_with_label_from_widget(group, label)
            button.set_mode(False)
            button.connect("toggled", lambda b, n=name: b.get_active() and self.set_filter(n))
            chips.pack_start(button, False, False, 0)
            self.filter_buttons[name] = button
            group = group or button
        bar.pack_start(chips, False, False, 0)
        self.group_toggle = Gtk.ToggleButton.new_with_mnemonic(_("_Group by Output"))
        self.group_toggle.connect("toggled", lambda *_a: self.set_grouped(self.group_toggle.get_active()))
        bar.pack_start(self.group_toggle, False, False, 0)
        self.diff_button = Gtk.Button.new_with_mnemonic(_("Show _Differences"))
        self.diff_button.set_tooltip_text(_("The selected host's output against the output most hosts have"))
        self.diff_button.connect("clicked", lambda *_a: self.differences())
        bar.pack_start(self.diff_button, False, False, 0)
        self.pack_start(bar, False, False, 0)

        # the flat table
        self.store = Gtk.ListStore(str, str, str, str, str, str, str, str, str, int)
        self.filtered = self.store.filter_new()
        self.filtered.set_visible_func(self._visible)
        self.view = Gtk.TreeView(model=self.filtered)
        self.view.set_fixed_height_mode(True)
        set_accessible_name(self.view, _("Results per host"))
        status = Gtk.TreeViewColumn(_("Status"))
        status.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
        status.set_fixed_width(170)
        icon = Gtk.CellRendererPixbuf()
        status.pack_start(icon, False)
        status.add_attribute(icon, "icon-name", COL_ICON)
        word = Gtk.CellRendererText()
        status.pack_start(word, True)
        status.add_attribute(word, "text", COL_STATUS)
        self.view.append_column(status)
        for title, column, width, expand, dim in ((_("Host"), COL_ALIAS, 150, False, False),
                                                  (_("Group"), COL_GROUP, 130, False, True),
                                                  (_("Exit"), COL_EXIT, 50, False, False),
                                                  (_("Time"), COL_DURATION, 60, False, False),
                                                  (_("Output"), COL_FIRST, 200, True, False)):
            cell = Gtk.CellRendererText()
            cell.set_property("ellipsize", Pango.EllipsizeMode.END)
            if dim:
                cell.set_property("style", Pango.Style.ITALIC)
            col = Gtk.TreeViewColumn(title, cell, text=column)
            col.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            col.set_fixed_width(width)
            col.set_expand(expand)
            col.set_resizable(True)
            self.view.append_column(col)
        self.view.get_selection().connect("changed", lambda *_a: self._show_selected())

        # the grouped table
        self.groups = Gtk.TreeStore(str, str, str)
        self.group_view = Gtk.TreeView(model=self.groups)
        self.group_view.set_headers_visible(False)
        set_accessible_name(self.group_view, _("Results grouped by output"))
        gcol = Gtk.TreeViewColumn("")
        gicon = Gtk.CellRendererPixbuf()
        gcol.pack_start(gicon, False)
        gcol.add_attribute(gicon, "icon-name", G_ICON)
        gtext = Gtk.CellRendererText()
        gtext.set_property("ellipsize", Pango.EllipsizeMode.END)
        gcol.pack_start(gtext, True)
        gcol.add_attribute(gtext, "text", G_LABEL)
        self.group_view.append_column(gcol)
        self.group_view.get_selection().connect("changed", lambda *_a: self._show_selected())

        self.tables = Gtk.Stack()
        self.tables.add_named(_scrolled(self.view, 160), "flat")
        self.tables.add_named(_scrolled(self.group_view, 160), "grouped")

        # the output of the selected host
        output = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        self.output_search = Gtk.SearchEntry()
        self.output_search.set_placeholder_text(_("Search this host's output"))
        set_accessible_name(self.output_search, _("Search this host's output"))
        self.output_search.connect("search-changed", lambda *_a: self.highlight())
        output.pack_start(self.output_search, False, False, 0)
        self.notice = Gtk.Label()
        self.notice.set_xalign(0.0)
        self.notice.set_line_wrap(True)
        self.notice.get_style_context().add_class("form-help")
        output.pack_start(self.notice, False, False, 0)
        # Host key changed / unknown: the shared host-key dialogs (set by the Run Command dialog).
        self.on_host_key_details: Callable[[R.HostResult], None] = lambda result: None
        self.details_button = Gtk.Button.new_with_mnemonic(_("Deta_ils…"))
        self.details_button.set_halign(Gtk.Align.START)
        self.details_button.set_no_show_all(True)
        self.details_button.connect("clicked", lambda *_a: self._host_key_details())
        output.pack_start(self.details_button, False, False, 0)
        self.stdout_view, self.stderr_view = _text_view(), _text_view()
        set_accessible_name(self.stdout_view, _("Standard output"))
        set_accessible_name(self.stderr_view, _("Standard error"))
        self.output_tabs = Gtk.Notebook()
        self.output_tabs.append_page(_scrolled(self.stdout_view), Gtk.Label(label=_("stdout")))
        self.output_tabs.append_page(_scrolled(self.stderr_view), Gtk.Label(label=_("stderr")))
        output.pack_start(self.output_tabs, True, True, 0)

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.pack1(self.tables, resize=True, shrink=False)
        paned.pack2(output, resize=True, shrink=False)
        paned.set_position(560)
        self.pack_start(paned, True, True, 0)
        self.selected: Optional[str] = None
        self._sync()

    # -------------------------------------------------------------- data
    def reset(self, targets: Iterable[R.Target]) -> None:
        """One row per target, queued (built detached from the view: one pass)."""

        self.view.set_model(None)
        self.store.clear()
        self.results.clear()
        self._iters.clear()
        for index, target in enumerate(targets):
            result = R.HostResult(target.key, target.alias, target.group)
            self.results[target.key] = result
            self._iters[target.key] = self.store.append(self._row(result, index))
        self.view.set_model(self.filtered)
        self.groups.clear()
        self.group_toggle.set_active(False)
        self.selected = None
        self._show_selected()

    def _row(self, result: R.HostResult, index: int = 0) -> list:
        duration = f"{result.duration:.1f} s" if result.finished and result.status != R.SKIPPED else ""
        exit_code = "" if result.exit_code is None else str(result.exit_code)
        return [result.key, ICONS.get(result.status, ""), R.status_label(result), result.alias, result.group,
                exit_code, duration, result.first_line[:300], result.status, index]

    def update(self, results: List[R.HostResult]) -> None:
        for result in results:
            tree_iter = self._iters.get(result.key)
            if tree_iter is None:
                continue
            self.results[result.key] = result
            values = self._row(result, self.store[tree_iter][COL_SORT])
            for column in range(COL_ICON, COL_SORT):
                if self.store[tree_iter][column] != values[column]:
                    self.store[tree_iter][column] = values[column]
            if result.key == self.selected:
                self._show_selected()

    def _visible(self, model, tree_iter, _data=None) -> bool:
        state = model[tree_iter][COL_STATE]
        if self.show_filter == "failed":
            return state in R.FAILURES
        if self.show_filter == "succeeded":
            return state == R.SUCCESS
        return True

    def set_filter(self, name: str) -> None:
        self.show_filter = name
        if not self.filter_buttons[name].get_active():
            self.filter_buttons[name].set_active(True)
        self.filtered.refilter()

    def keys_with(self, states: Iterable[str]) -> List[str]:
        wanted = set(states)
        return [key for key, result in self.results.items() if result.status in wanted]

    def ordered(self) -> List[R.HostResult]:
        return [self.results[row[COL_KEY]] for row in self.store]

    # ---------------------------------------------------------- grouping
    def set_grouped(self, grouped: bool) -> None:
        if self.group_toggle.get_active() != grouped:
            self.group_toggle.set_active(grouped)
            return
        if grouped:
            self.rebuild_groups()
        self.tables.set_visible_child_name("grouped" if grouped else "flat")
        self._sync()

    def rebuild_groups(self) -> List[P.OutputGroup]:
        self.groups.clear()
        groups = P.group_by_output(self.ordered())
        for group in groups:
            first = group.text.splitlines()[0] if group.text else _("(no output)")
            parent = self.groups.append(None, [f"{group.label}: {first}"[:300], "", "view-list-symbolic"])
            for key in group.keys:
                result = self.results[key]
                self.groups.append(parent, [f"{result.alias} — {R.status_label(result)}", key,
                                            ICONS.get(result.status, "")])
        others = [r for r in self.ordered() if r.status not in P.GROUPED]
        if others:
            parent = self.groups.append(None, [ngettext("{n} host without output to compare",
                                                        "{n} hosts without output to compare",
                                                        len(others)).format(n=len(others)), "",
                                               "dialog-information-symbolic"])
            for result in others:
                self.groups.append(parent, [f"{result.alias} — {R.status_label(result)}", result.key,
                                            ICONS.get(result.status, "")])
        self._groups = groups
        return groups

    def differences(self) -> str:
        key = self.selected
        groups = getattr(self, "_groups", None) or P.group_by_output(self.ordered())
        main = P.majority(groups)
        if key is None or main is None or key not in self.results:
            return ""
        result = self.results[key]
        diff = P.unified_diff(main.text, P.normalise(result.stdout), main.label, result.alias)
        self.show_diff(_("Differences: {host}").format(host=result.alias),
                       diff or _("{host} has the same output as the majority.").format(host=result.alias))
        return diff

    def _show_diff_dialog(self, title: str, text: str) -> None:
        from .base import BaseDialog, DialogSize

        dialog = BaseDialog(self.get_toplevel() if isinstance(self.get_toplevel(), Gtk.Window) else None, title,
                            size=DialogSize.LARGE, height=480)
        view = _text_view()
        view.get_buffer().set_text(text)
        dialog.add_body(_scrolled(view), expand=True)
        dialog.add_primary(_("_Close"), Gtk.ResponseType.CLOSE)
        dialog.run_and_destroy()

    # ------------------------------------------------------------ output
    def _selected_from_views(self) -> Optional[str]:
        if self.tables.get_visible_child_name() == "grouped":
            model, tree_iter = self.group_view.get_selection().get_selected()
            return (model[tree_iter][G_KEY] or None) if tree_iter is not None else None
        model, tree_iter = self.view.get_selection().get_selected()
        return model[tree_iter][COL_KEY] if tree_iter is not None else None

    def select(self, key: str) -> bool:
        for row in self.filtered:
            if row[COL_KEY] == key:
                self.view.get_selection().select_iter(row.iter)
                return True
        return False

    def selected_key(self) -> Optional[str]:
        return self.selected

    def _show_selected(self) -> None:
        self.selected = self._selected_from_views()
        result = self.results.get(self.selected) if self.selected else None
        self.stdout_view.get_buffer().set_text(result.stdout if result else "")
        self.stderr_view.get_buffer().set_text(result.stderr if result else "")
        notes = []
        if result is not None:
            if result.message:
                notes.append(result.message)
            if result.hint:
                notes.append(result.hint)
            if result.stdout_truncated or result.stderr_truncated:
                notes.append(_("The output was longer than the limit; only its beginning is shown."))
        self.notice.set_text("\n".join(notes))
        self.notice.set_visible(bool(notes))
        self.details_button.set_visible(result is not None and result.host_key_error is not None and
                                        result.status in (R.HOST_KEY_CHANGED, R.HOST_KEY_UNKNOWN))
        self.highlight()
        self._sync()

    def _host_key_details(self) -> None:
        result = self.results.get(self.selected) if self.selected else None
        if result is not None and result.host_key_error is not None:
            self.on_host_key_details(result)

    def highlight(self) -> int:
        needle = self.output_search.get_text()
        found = 0
        for view in (self.stdout_view, self.stderr_view):
            buffer = view.get_buffer()
            buffer.remove_tag_by_name("match", buffer.get_start_iter(), buffer.get_end_iter())
            if not needle:
                continue
            start = buffer.get_start_iter()
            first = None
            while True:
                match = start.forward_search(needle, Gtk.TextSearchFlags.CASE_INSENSITIVE, None)
                if not match:
                    break
                buffer.apply_tag_by_name("match", match[0], match[1])
                first = first or match[0]
                found += 1
                start = match[1]
            if first is not None:
                view.scroll_to_iter(first, 0.1, False, 0, 0)
        return found

    def _sync(self) -> None:
        self.diff_button.set_sensitive(self.selected is not None and bool(self.results))


__all__ = ["ResultsView", "ICONS"]
