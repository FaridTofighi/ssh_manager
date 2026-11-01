"""Import → From OpenSSH config…: pick files/folders, preview, choose a conflict policy."""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ...inventory.openssh import OpenSSHPlan, Policy, plan_openssh_import  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.forms import FormSection  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from .base import BaseDialog, DialogSize, is_accept  # noqa: E402
from .files import choose_folder, choose_open_file  # noqa: E402


def default_sources(home: Optional[Path] = None) -> List[Path]:
    home = home or Path.home()
    return [home / ".ssh" / "config", home / ".ssh" / "conf.d"]


class OpenSSHImportDialog(BaseDialog):
    """Choose sources (``~/.ssh/config``, ``~/.ssh/conf.d`` or others), see what will be imported, import.

    The files are only read. ``plan()`` / ``policy()`` give the result after an accepting response.
    """

    def __init__(self, parent: Optional[Gtk.Window], *, initial: Optional[Path] = None, home: Optional[Path] = None) -> None:
        BaseDialog.__init__(self, parent, _("Import from OpenSSH Config"), size=DialogSize.MEDIUM, height=520)
        self._sources: List[Path] = []
        self._checks: List[Gtk.CheckButton] = []
        self._plan: Optional[OpenSSHPlan] = None
        section = FormSection(
            _("Sources"),
            _("Hosts are copied into Ophion; the files themselves are never changed. In a folder, each "
              "folder and each file becomes a group."),
        )
        self._source_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=Space.XS)
        section.add_widget(self._source_box)
        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        add_file = Gtk.Button.new_with_mnemonic(_("Add _File…"))
        add_file.connect("clicked", lambda *_a: self._add_chosen(choose_open_file(self, _("Choose an SSH Config File"))))
        add_folder = Gtk.Button.new_with_mnemonic(_("Add F_older…"))
        add_folder.connect("clicked", lambda *_a: self._add_chosen(choose_folder(self, _("Choose a Folder of SSH Configs"))))
        buttons.pack_start(add_file, False, False, 0)
        buttons.pack_start(add_folder, False, False, 0)
        section.add_widget(buttons)
        self.policy_combo = Gtk.ComboBoxText()
        for policy, label in ((Policy.SKIP, _("Skip the existing host")),
                              (Policy.RENAME, _("Import with a suffix")),
                              (Policy.OVERWRITE, _("Overwrite the existing host"))):
            self.policy_combo.append(policy.value, label)
        self.policy_combo.set_active_id(Policy.SKIP.value)
        set_accessible_name(self.policy_combo, _("When a host already exists"))
        section.add(_("When a host already _exists"), self.policy_combo,
                    help_text=_("Hosts with the same host name, port and user in the same group are always skipped, "
                                "so importing again adds nothing."))
        self.add_body(section)
        self.summary = Gtk.Label(xalign=0.0)
        self.summary.set_line_wrap(True)
        self.summary.get_style_context().add_class("section-title")
        self.add_body(self.summary)
        self.notes = Gtk.TextView()
        self.notes.set_editable(False)
        self.notes.set_cursor_visible(False)
        self.notes.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        set_accessible_name(self.notes, _("Import notes"))
        scroller = Gtk.ScrolledWindow()
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.set_vexpand(True)
        scroller.add(self.notes)
        self.add_body(scroller, expand=True)
        self.add_cancel()
        self.import_button = self.add_primary(_("_Import"))
        self.set_response_validator(lambda response: not is_accept(response) or bool(self._plan and self._plan.host_count))
        for path in default_sources(home):
            if path.exists():
                self.add_source(path, active=initial is None)
        if initial is not None:
            self.add_source(Path(initial), active=True)
        self.show_all()
        self.refresh_preview()

    def add_source(self, path: Path, *, active: bool = True) -> None:
        if path in self._sources:
            self._checks[self._sources.index(path)].set_active(active)
            return
        check = Gtk.CheckButton(label=str(path))
        check.set_active(active)
        check.connect("toggled", lambda *_a: self.refresh_preview())
        self._sources.append(path)
        self._checks.append(check)
        self._source_box.pack_start(check, False, False, 0)
        check.show()

    def _add_chosen(self, chosen: Optional[str]) -> None:
        if chosen:
            self.add_source(Path(chosen), active=True)
            self.refresh_preview()

    def selected_sources(self) -> List[Path]:
        return [path for path, check in zip(self._sources, self._checks) if check.get_active()]

    def refresh_preview(self) -> None:
        sources = self.selected_sources()
        folders = [p for p in sources if p.is_dir()]
        files = [p for p in sources if not p.is_dir()]
        plan = plan_openssh_import(folders)
        if files:
            extra = plan_openssh_import(files, exclude_under=folders)  # Included folder hosts come once
            plan.hosts += extra.hosts
            plan.groups += [g for g in extra.groups if g not in plan.groups]
            plan.notes += extra.notes
            plan.skipped += extra.skipped
            plan.sources += extra.sources
        self._plan = plan
        if not sources:
            self.summary.set_text(_("Choose at least one source."))
        else:
            self.summary.set_text(ngettext("{hosts} host in {groups} group will be imported.",
                                           "{hosts} hosts in {groups} groups will be imported.",
                                           plan.host_count).format(hosts=plan.host_count, groups=len(plan.groups)))
        self.notes.get_buffer().set_text("\n".join(note.message for note in plan.notes) or _("No problems found."))
        self.import_button.set_sensitive(plan.host_count > 0)

    def plan(self) -> Optional[OpenSSHPlan]:
        return self._plan

    def policy(self) -> Policy:
        return Policy(self.policy_combo.get_active_id() or Policy.SKIP.value)
