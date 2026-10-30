"""Run Command: the confirmation before a run on many hosts or with a command that can disrupt servers.

The host count in the title, the hosts in a scrollable, searchable list of
fixed height (``SearchableList``: never a giant message), and for a command
that matches :data:`~ssh_manager.multirun.safety.DANGEROUS_PATTERNS` a check
box "I understand this can disrupt the servers" that enables **Run**. The
check is advisory: it never says a command is safe.
"""

from __future__ import annotations

from typing import Optional, Sequence

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ...multirun.engine import Target  # noqa: E402
from ...multirun.safety import dangerous_matches  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..widgets.searchable_list import SearchableList  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402


class RunConfirmDialog(BaseDialog):
    def __init__(self, parent: Optional[Gtk.Window], targets: Sequence[Target], command: str) -> None:
        BaseDialog.__init__(self, parent, _("Run Command"), size=DialogSize.MEDIUM, resizable=False)
        count = len(targets)
        self.matches = dangerous_matches(command)
        self.title_label = Gtk.Label(label=ngettext("Run the command on {n} host?", "Run the command on {n} hosts?",
                                                    count).format(n=count))
        self.title_label.set_xalign(0.0)
        self.title_label.get_style_context().add_class("section-title")
        self.add_body(self.title_label)
        first = command.strip().splitlines()[0] if command.strip() else ""
        preview = Gtk.Label(label=first[:120] + ("…" if len(first) > 120 or "\n" in command.strip() else ""))
        preview.set_xalign(0.0)
        preview.get_style_context().add_class("mono")
        self.add_body(preview)
        self.matched_label = Gtk.Label()
        self.matched_label.set_xalign(0.0)
        self.matched_label.set_line_wrap(True)
        self.advisory_label = Gtk.Label()
        self.advisory_label.set_xalign(0.0)
        self.advisory_label.set_line_wrap(True)
        self.advisory_label.get_style_context().add_class("form-help")
        self.understand = Gtk.CheckButton.new_with_mnemonic(_("_I understand this can disrupt the servers"))
        if self.matches:
            self.matched_label.set_text(_("⚠ The command contains: {list}").format(list=", ".join(self.matches)))
            self.matched_label.get_style_context().add_class("form-error")
            self.add_body(self.matched_label)
            self.add_body(self.understand)
        self.advisory_label.set_text(_("This check only looks for some known patterns; it never means a command "
                                       "is safe."))
        self.add_body(self.advisory_label)
        items = [(f"{t.alias}   {t.group}" if t.group else t.alias, t.key) for t in targets]
        self.hosts = SearchableList(items, search_label=_("Search the hosts"), list_label=_("Hosts the command runs on"),
                                    placeholder=_("Search the hosts"), min_height=8 * 24)
        self.hosts.scroller.set_max_content_height(8 * 24)
        self.hosts.scroller.set_propagate_natural_height(False)
        self.add_body(self.hosts)
        self.add_cancel()
        self.add_primary(_("_Run"), destructive=bool(self.matches), default=not self.matches)
        self.understand.connect("toggled", lambda *_a: self._sync())
        self._sync()

    def _sync(self) -> None:
        self.primary_button.set_sensitive(not self.matches or self.understand.get_active())

    def ask(self) -> bool:
        return is_accept(self.run_and_destroy())


__all__ = ["RunConfirmDialog"]
