"""Preferences → Remote Desktop → **Diagnostics**: which FreeRDP clients and engines exist, and why.

Discovery runs in a worker thread (it starts each client with ``/version``
and ``/help``); the dialog shows the plain-text report from
:meth:`EngineReport.text` with **Copy** and **Check Again**. The report holds
no credentials or host data.
"""

from __future__ import annotations

from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # type: ignore  # noqa: E402

from ...rdp.engines import EngineReport, discover_async  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Size, Space  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from .base import BaseDialog, DialogSize  # noqa: E402

RESPONSE_COPY = 1
RESPONSE_REFRESH = 2


class RdpDiagnosticsDialog(BaseDialog):
    """The engine report with Copy and Check Again (``discover`` may be replaced in tests)."""

    def __init__(self, parent: Optional[Gtk.Window], *, preference: str = "auto", override: str = "",
                 report: Optional[EngineReport] = None,
                 discover: Optional[Callable[..., object]] = None) -> None:
        BaseDialog.__init__(self, parent, _("Remote Desktop Diagnostics"), size=DialogSize.MEDIUM, height=480)
        self._preference = preference
        self._override = override
        self._discover = discover  # None: discover_async (looked up when used)
        self._closed = False
        self.report: Optional[EngineReport] = None
        self.summary = Gtk.Label()
        self.summary.set_xalign(0.0)
        self.summary.set_line_wrap(True)
        self.view = Gtk.TextView()
        self.view.set_editable(False)
        self.view.set_cursor_visible(False)
        self.view.set_monospace(True)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_left_margin(Space.S)
        set_accessible_name(self.view, _("Diagnostics report"))
        scroller = Gtk.ScrolledWindow()
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(10 * Size.TREE_ROW)
        scroller.add(self.view)
        self.spinner = Gtk.Spinner()
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        top.pack_start(self.summary, True, True, 0)
        top.pack_start(self.spinner, False, False, 0)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.S)
        box.pack_start(top, False, False, 0)
        box.pack_start(scroller, True, True, 0)
        self.add_body(box, expand=True)
        self.refresh_button = self.add_secondary(_("Check _Again"), RESPONSE_REFRESH)
        self.copy_button = self.add_secondary(_("_Copy"), RESPONSE_COPY)
        self.add_primary(_("_Close"))
        self.connect("response", self._on_response)
        self.connect("destroy", lambda *_: setattr(self, "_closed", True))
        self.show_all()
        if report is not None:
            self.show_report(report)
        else:
            self.refresh()

    def refresh(self) -> None:
        self.spinner.start()
        self.refresh_button.set_sensitive(False)
        self.copy_button.set_sensitive(False)
        self.summary.set_text(_("Looking for FreeRDP clients…"))
        run = self._discover or discover_async
        run(lambda report: GLib.idle_add(self.show_report, report), preference=self._preference,
            override=self._override)

    def show_report(self, report: EngineReport) -> bool:
        if self._closed:
            return GLib.SOURCE_REMOVE
        self.report = report
        self.spinner.stop()
        self.refresh_button.set_sensitive(True)
        self.copy_button.set_sensitive(True)
        if report.chosen is not None:
            self.summary.set_text(_("Remote Desktop sessions use: {engine}.").format(engine=_(report.chosen.label)))
        else:
            self.summary.set_text(_("No RDP engine is available. Install FreeRDP; the report says how."))
        self.view.get_buffer().set_text(report.text())  # plain text: paths and versions are never markup
        return GLib.SOURCE_REMOVE

    def copy(self) -> str:
        text = self.report.text() if self.report is not None else ""
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        return text

    def _on_response(self, _dialog, response: int) -> None:
        if response == RESPONSE_COPY:
            self.copy()
            self.stop_emission_by_name("response")
        elif response == RESPONSE_REFRESH:
            self.refresh()
            self.stop_emission_by_name("response")
