"""Run Command → the results table: one row per host in a ``Gtk.ListStore`` (no widget per host).

Columns: status (an icon and a word), host, group (lighter), exit code,
duration, the first line of output. Rows are created once per run and
updated in place from the engine's events.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # type: ignore  # noqa: E402

from ...multirun import engine as R  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402

(COL_KEY, COL_ICON, COL_STATUS, COL_ALIAS, COL_GROUP, COL_EXIT, COL_DURATION, COL_FIRST, COL_STATE,
 COL_SORT) = range(10)
ICONS = {R.QUEUED: "content-loading-symbolic", R.RUNNING: "media-playback-start-symbolic",
         R.SUCCESS: "emblem-ok-symbolic", R.FAILED: "dialog-error-symbolic", R.TIMEOUT: "alarm-symbolic",
         R.AUTH_FAILED: "dialog-password-symbolic", R.HOST_KEY_CHANGED: "dialog-warning-symbolic",
         R.HOST_KEY_UNKNOWN: "dialog-question-symbolic", R.UNREACHABLE: "network-offline-symbolic",
         R.CANCELLED: "process-stop-symbolic", R.SKIPPED: "action-unavailable-symbolic"}


class ResultsView(Gtk.Box):
    def __init__(self) -> None:
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        self.results: Dict[str, R.HostResult] = {}
        self._iters: Dict[str, Gtk.TreeIter] = {}
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
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.scroller.set_shadow_type(Gtk.ShadowType.IN)
        self.scroller.set_min_content_height(160)
        self.scroller.add(self.view)
        self.pack_start(self.scroller, True, True, 0)
        self.show_filter = "all"

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

    def _row(self, result: R.HostResult, index: int = 0) -> list:
        duration = f"{result.duration:.1f} s" if result.finished and result.status != R.SKIPPED else ""
        exit_code = "" if result.exit_code is None else str(result.exit_code)
        first = result.first_line[:300]
        return [result.key, ICONS.get(result.status, ""), R.status_label(result), result.alias, result.group,
                exit_code, duration, first, result.status, index]

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

    def _visible(self, model, tree_iter, _data=None) -> bool:
        state = model[tree_iter][COL_STATE]
        if self.show_filter == "failed":
            return state in R.FAILURES
        if self.show_filter == "succeeded":
            return state == R.SUCCESS
        return True

    def set_filter(self, name: str) -> None:
        self.show_filter = name
        self.filtered.refilter()

    def keys_with(self, states: Iterable[str]) -> List[str]:
        wanted = set(states)
        return [key for key, result in self.results.items() if result.status in wanted]

    def selected_key(self) -> Optional[str]:
        model, tree_iter = self.view.get_selection().get_selected()
        return model[tree_iter][COL_KEY] if tree_iter is not None else None


__all__ = ["ResultsView", "ICONS"]
