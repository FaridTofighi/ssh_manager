"""The Transfers panel of a file-browser tab, and the "file already exists" dialog.

Each job is a row:
- **Shows:** direction arrow, name, progress bar, done / total, speed, ETA,
  status and the error of a failure.
- **Actions:** pause / resume, cancel, retry (the failed files), reveal the
  destination, remove.
- **Toolbar:** pause all, resume all, clear finished and a speed limit.

Rows are updated on the GTK thread only (the tab posts the engine's
snapshots); a finished row announces itself to assistive technology.
"""

from __future__ import annotations

from typing import Callable, Dict, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, Pango  # type: ignore  # noqa: E402

from ...transfer.browse import format_mtime, human_size  # noqa: E402
from ...transfer.engine import FINISHED, Conflict, ConflictInfo, Direction, State, TransferJob  # noqa: E402
from ..constants import Icons  # noqa: E402
from ..dialogs.base import BaseDialog, DialogSize  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..theme.tokens import Space  # noqa: E402
from ..widgets.icon_button import IconButton, set_accessible_name  # noqa: E402

_ARROWS = {Direction.UPLOAD: "↑", Direction.DOWNLOAD: "↓", Direction.REMOTE_COPY: "⇄",
           Direction.LOCAL_COPY: "⇉", Direction.REMOTE_MOVE: "→"}
_STATES = {State.QUEUED: _("Queued"), State.PLANNING: _("Scanning…"), State.RUNNING: _("Transferring"),
           State.PAUSED: _("Paused"), State.DONE: _("Done"), State.FAILED: _("Failed"),
           State.CANCELLED: _("Cancelled"), State.SKIPPED: _("Skipped")}


def format_eta(seconds: Optional[float]) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    if seconds < 60:
        return _("{n} s left").format(n=seconds)
    if seconds < 3600:
        return _("{m} min left").format(m=seconds // 60)
    return _("{h} h {m} min left").format(h=seconds // 3600, m=(seconds % 3600) // 60)


def job_detail(job: TransferJob) -> str:
    """``12.4 MB of 48 MB · 3.1 MB/s · 11 s left`` / ``Scanning… 340 files`` / the error."""

    if job.state is State.PLANNING:
        return _("Scanning… {n} files").format(n=job.planned)
    if job.state is State.FAILED:
        failures = job.failures()
        reason = failures[0].error if failures else job.error
        return ngettext("{n} file failed: {reason}", "{n} files failed: {reason}", max(1, len(failures))).format(
            n=max(1, len(failures)), reason=reason)
    parts = []
    total = job.total
    if total:
        parts.append(_("{done} of {total}").format(done=human_size(job.done_bytes) or "0 B", total=human_size(total)))
    if job.state is State.RUNNING and job.rate:
        parts.append(f"{human_size(int(job.rate))}/s")
        eta = format_eta(job.eta)
        if eta:
            parts.append(eta)
    skipped = job.counts().get(State.SKIPPED, 0)
    if skipped:
        parts.append(ngettext("{n} skipped", "{n} skipped", skipped).format(n=skipped))
    parts.append(_STATES.get(job.state, job.state.value))
    return " · ".join(parts)


class JobRow(Gtk.ListBoxRow):
    def __init__(self, job: TransferJob, panel: "TransferPanel") -> None:
        Gtk.ListBoxRow.__init__(self)
        self.job = job
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        box.set_margin_start(Space.S)
        box.set_margin_end(Space.S)
        box.set_margin_top(Space.XS)
        box.set_margin_bottom(Space.XS)
        arrow = Gtk.Label(label=_ARROWS.get(job.direction, "→"))
        arrow.set_tooltip_text({Direction.UPLOAD: _("Upload"), Direction.DOWNLOAD: _("Download")}.get(
            job.direction, _("Copy")))
        box.pack_start(arrow, False, False, 0)
        middle = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.title = Gtk.Label(label=job.title)  # remote names: plain text
        self.title.set_xalign(0.0)
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.bar = Gtk.ProgressBar()
        self.detail = Gtk.Label()
        self.detail.set_xalign(0.0)
        self.detail.set_ellipsize(Pango.EllipsizeMode.END)
        self.detail.get_style_context().add_class("form-help")
        middle.pack_start(self.title, False, False, 0)
        middle.pack_start(self.bar, False, False, 0)
        middle.pack_start(self.detail, False, False, 0)
        box.pack_start(middle, True, True, 0)
        self.pause_button = IconButton("media-playback-pause-symbolic", _("Pause"), lambda: panel.toggle_pause(job))
        self.cancel_button = IconButton(Icons.CLOSE, _("Cancel"), lambda: panel.on_cancel(job))
        self.retry_button = IconButton(Icons.RECONNECT, _("Retry the failed files"), lambda: panel.on_retry(job))
        self.reveal_button = IconButton(Icons.OPEN_FOLDER, _("Open the destination folder"),
                                        lambda: panel.on_reveal(job))
        self.remove_button = IconButton(Icons.CLEAR, _("Remove from the list"), lambda: panel.remove(job))
        for button in (self.pause_button, self.cancel_button, self.retry_button, self.reveal_button,
                       self.remove_button):
            button.set_no_show_all(True)
            box.pack_start(button, False, False, 0)
        self.add(box)
        self._announced = False
        self.update()

    def update(self) -> None:
        job = self.job
        total = job.total
        fraction = (job.done_bytes / total) if total else (1.0 if job.state is State.DONE else 0.0)
        self.bar.set_fraction(min(1.0, max(0.0, fraction)))
        if job.state is State.PLANNING:
            self.bar.pulse()
        self.detail.set_text(job_detail(job))
        finished = job.state in FINISHED
        self.pause_button.set_visible(not finished)
        self.pause_button.set_icon("media-playback-start-symbolic" if job.state is State.PAUSED
                                   else "media-playback-pause-symbolic")
        self.pause_button.set_tooltip_text(_("Resume") if job.state is State.PAUSED else _("Pause"))
        self.cancel_button.set_visible(not finished)
        self.retry_button.set_visible(job.state is State.FAILED and bool(job.failures()))
        self.reveal_button.set_visible(job.state is State.DONE and job.target.local)
        self.remove_button.set_visible(finished)
        set_accessible_name(self, f"{job.title}: {self.detail.get_text()}")
        if finished and not self._announced:
            self._announced = True
            accessible = self.get_accessible()
            if accessible is not None:  # completion is announced, not only shown
                accessible.set_description(_STATES.get(job.state, ""))


class TransferPanel(Gtk.Revealer):
    """Collapsible list of jobs at the bottom of the tab."""

    def __init__(self) -> None:
        Gtk.Revealer.__init__(self)
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.rows: Dict[int, JobRow] = {}
        self.on_pause: Callable[[TransferJob], None] = lambda _j: None
        self.on_resume: Callable[[TransferJob], None] = lambda _j: None
        self.on_cancel: Callable[[TransferJob], None] = lambda _j: None
        self.on_retry: Callable[[TransferJob], None] = lambda _j: None
        self.on_reveal: Callable[[TransferJob], None] = lambda _j: None
        self.on_clear: Callable[[], None] = lambda: None
        self.on_limit: Callable[[int], None] = lambda _k: None
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.get_style_context().add_class("transfer-panel")
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.XS)
        bar.set_margin_start(Space.S)
        bar.set_margin_end(Space.S)
        title = Gtk.Label(label=_("Transfers"))
        title.get_style_context().add_class("section-title")
        bar.pack_start(title, False, False, 0)
        self.summary = Gtk.Label()
        self.summary.get_style_context().add_class("form-help")
        bar.pack_start(self.summary, False, False, Space.S)
        bar.pack_end(IconButton(Icons.CLEAR, _("Clear finished"), self._clear), False, False, 0)
        bar.pack_end(IconButton("media-playback-start-symbolic", _("Resume all"), self.resume_all), False, False, 0)
        bar.pack_end(IconButton("media-playback-pause-symbolic", _("Pause all"), self.pause_all), False, False, 0)
        self.limit = Gtk.SpinButton.new_with_range(0, 10_000_000, 100)
        self.limit.set_tooltip_text(_("Speed limit in KB/s for this tab (0 = unlimited)"))
        set_accessible_name(self.limit, _("Speed limit"))
        self.limit.connect("value-changed", lambda spin: self.on_limit(int(spin.get_value())))
        bar.pack_end(self.limit, False, False, 0)
        limit_label = Gtk.Label.new_with_mnemonic(_("_Limit (KB/s)"))
        limit_label.set_mnemonic_widget(self.limit)
        limit_label.get_style_context().add_class("dim-label")
        bar.pack_end(limit_label, False, False, 0)
        outer.pack_start(bar, False, False, 0)
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        set_accessible_name(self.list, _("Transfers"))
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_min_content_height(120)
        scroller.set_max_content_height(220)
        scroller.set_propagate_natural_height(True)
        scroller.add(self.list)
        outer.pack_start(scroller, True, True, 0)
        self.add(outer)

    def update(self, job: TransferJob) -> None:
        row = self.rows.get(job.id)
        if row is None:
            row = JobRow(job, self)
            self.rows[job.id] = row
            self.list.insert(row, 0)
            row.show_all()
            self.set_reveal_child(True)
        row.update()
        self._summary()

    def _summary(self) -> None:
        running = [row.job for row in self.rows.values() if row.job.state not in FINISHED]
        rate = sum(job.rate for job in running if job.state is State.RUNNING)
        text = ngettext("{n} transfer", "{n} transfers", len(running)).format(n=len(running))
        if rate:
            text += f" · {human_size(int(rate))}/s"
        self.summary.set_text(text if running else _("All transfers finished"))

    def summary_text(self) -> str:
        return self.summary.get_text()

    def toggle_pause(self, job: TransferJob) -> None:
        (self.on_resume if job.state is State.PAUSED else self.on_pause)(job)

    def pause_all(self) -> None:
        for row in list(self.rows.values()):
            if row.job.state not in FINISHED:
                self.on_pause(row.job)

    def resume_all(self) -> None:
        for row in list(self.rows.values()):
            if row.job.state is State.PAUSED:
                self.on_resume(row.job)

    def remove(self, job: TransferJob) -> None:
        row = self.rows.pop(job.id, None)
        if row is not None:
            self.list.remove(row)
        self._summary()

    def _clear(self) -> None:
        for job_id, row in list(self.rows.items()):
            if row.job.state in FINISHED:
                self.list.remove(row)
                del self.rows[job_id]
        self.on_clear()
        self._summary()


class ConflictDialog(BaseDialog):
    """"“report.pdf” already exists": both sides' size and date; a choice; "Apply to all"."""

    def __init__(self, parent: Optional[Gtk.Window], info: ConflictInfo) -> None:
        name = info.item.name
        BaseDialog.__init__(self, parent, _("“{name}” already exists").format(name=name), size=DialogSize.SMALL,
                            resizable=False)
        heading = Gtk.Label(label=_("“{name}” already exists in the destination.").format(name=name))
        heading.set_xalign(0.0)
        heading.set_line_wrap(True)
        heading.get_style_context().add_class("section-title")
        self.add_body(heading)
        grid = Gtk.Grid(column_spacing=Space.M, row_spacing=Space.XS)
        for column, title in enumerate((_("New"), _("Existing")), start=1):
            label = Gtk.Label(label=title)
            label.get_style_context().add_class("dim-label")
            grid.attach(label, column, 0, 1, 1)
        rows = ((_("Size"), human_size(info.item.size) or "—", human_size(info.target_size) or "—"),
                (_("Modified"), format_mtime(info.item.mtime)[1] or "—", format_mtime(info.target_mtime)[1] or "—"))
        for row, (label, new, old) in enumerate(rows, start=1):
            key = Gtk.Label(label=label)
            key.set_xalign(1.0)
            key.get_style_context().add_class("dim-label")
            grid.attach(key, 0, row, 1, 1)
            grid.attach(Gtk.Label(label=new), 1, row, 1, 1)
            grid.attach(Gtk.Label(label=old), 2, row, 1, 1)
        self.add_body(grid)
        self.choice = Gtk.ComboBoxText()
        for policy, label in ((Conflict.OVERWRITE, _("Overwrite")), (Conflict.OVERWRITE_NEWER, _("Overwrite if newer")),
                              (Conflict.SKIP, _("Skip")), (Conflict.RENAME, _("Keep both (rename the new one)")),
                              (Conflict.RESUME, _("Resume the partial file"))):
            if policy is Conflict.RESUME and not info.can_resume:
                continue
            self.choice.append(policy.value, label)
        self.choice.set_active_id(Conflict.RESUME.value if info.can_resume else Conflict.OVERWRITE.value)
        set_accessible_name(self.choice, _("What to do"))
        self.add_body(self.choice)
        self.apply_all = Gtk.CheckButton.new_with_mnemonic(_("_Apply to all conflicts in this transfer"))
        self.add_body(self.apply_all)
        self.add_cancel(_("Cancel the _Transfer"))
        self.add_primary(_("C_ontinue"))
        self.show_all()

    def ask(self) -> Tuple[Optional[Conflict], bool]:
        """``(decision or None for "cancel the transfer", apply to all)``."""

        response = self.run()
        decision = Conflict(self.choice.get_active_id()) if response == Gtk.ResponseType.OK else None
        apply_all = self.apply_all.get_active()
        self.destroy()
        return decision, apply_all

