"""Run Command on Hosts: targets, the command, options, Run / Cancel, progress and the results table.

Built instantly whatever the number of targets: the dialog shows a count
("37 hosts selected"), never a list of names; the host chooser is built only
when **Change…** is clicked. While a run is going the command, targets and
options are locked; results arrive through the engine's queue, drained every
100 ms in batches.
"""

from __future__ import annotations

import time
from typing import Callable, List, Optional, Sequence

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # type: ignore  # noqa: E402

from ...multirun import engine as R  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..theme.tokens import Level, Space  # noqa: E402
from ..widgets.icon_button import set_accessible_name  # noqa: E402
from .base import BaseDialog, DialogSize  # noqa: E402
from .multirun_results import ResultsView  # noqa: E402

VARIABLES = ("{host}", "{alias}", "{user}", "{port}")
SHELLS = ("sh", "bash")
DRAIN_MS = 100
BATCH = 400  # results applied per drain


class MultiRunDialog(BaseDialog):
    def __init__(self, parent: Optional[Gtk.Window], controller, keys: Sequence[str] = ()) -> None:
        BaseDialog.__init__(self, parent, _("Run Command on Hosts"), size=DialogSize.LARGE, height=640)
        self.controller = controller
        self.keys: List[str] = list(dict.fromkeys(keys))
        self.run: Optional[R.MultiRun] = None
        self._drain_id = 0
        self.confirm_cancel: Callable[[], bool] = self._confirm_cancel  # test hook
        self.confirm_run: Callable[[List[R.Target], str], bool] = self._confirm_run  # test hook
        self.choose_targets: Callable[[], Optional[set]] = self._choose_targets  # test hook
        if controller.dialog_size:
            self.set_default_size(*controller.dialog_size)

        # ------------------------------------------------------- targets
        targets = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.targets_label = Gtk.Label()
        self.targets_label.set_xalign(0.0)
        targets.pack_start(self.targets_label, True, True, 0)
        self.change_button = Gtk.Button.new_with_mnemonic(_("_Change…"))
        self.change_button.connect("clicked", lambda *_a: self.change_targets())
        targets.pack_end(self.change_button, False, False, 0)
        self.add_body(targets)

        # ------------------------------------------------------- command
        self.command = Gtk.TextView()
        self.command.set_monospace(True)
        self.command.set_accepts_tab(False)
        set_accessible_name(self.command, _("Command or script"))
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.set_shadow_type(Gtk.ShadowType.IN)
        scroller.set_min_content_height(6 * 18)
        scroller.add(self.command)
        self.add_body(scroller)
        tools = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        variables = Gtk.MenuButton(label=_("Insert Variable"))
        menu = Gtk.Menu()
        for name in VARIABLES:
            item = Gtk.MenuItem(label=name)
            item.connect("activate", lambda _i, text=name: self.command.get_buffer().insert_at_cursor(text))
            menu.append(item)
        menu.show_all()
        variables.set_popup(menu)
        tools.pack_start(variables, False, False, 0)
        tools.pack_start(Gtk.Label(label=_("Shell")), False, False, 0)
        self.shell = Gtk.ComboBoxText.new_with_entry()
        for shell in SHELLS:
            self.shell.append_text(shell)
        self.shell.set_active(0)
        set_accessible_name(self.shell, _("Shell that runs the script"))
        tools.pack_start(self.shell, False, False, 0)
        self.history = Gtk.ComboBoxText()
        set_accessible_name(self.history, _("Recent commands"))
        self.history.connect("changed", self._on_history_chosen)
        tools.pack_end(self.history, False, False, 0)
        self.saved_button = Gtk.MenuButton(label=_("Saved Commands"))
        self.saved_button.connect("toggled", lambda button: button.get_active() and self._fill_saved_menu())
        self.saved_menu = Gtk.Menu()
        self.saved_button.set_popup(self.saved_menu)
        tools.pack_end(self.saved_button, False, False, 0)
        self.tools = tools
        self.add_body(tools)
        self.ask_name: Callable[[], Optional[str]] = self._ask_name  # test hook

        # ------------------------------------------------------- options
        self.options_box = Gtk.Expander(label=_("Options"))
        grid = Gtk.Grid(column_spacing=Space.M, row_spacing=Space.XS)
        defaults = controller.last_options or R.RunOptions()
        self.concurrency = Gtk.SpinButton.new_with_range(1, 50, 1)
        self.concurrency.set_value(defaults.concurrency)
        self.connect_timeout = Gtk.SpinButton.new_with_range(1, 300, 1)
        self.connect_timeout.set_value(defaults.connect_timeout)
        self.command_timeout = Gtk.SpinButton.new_with_range(0, 86400, 5)
        self.command_timeout.set_value(defaults.command_timeout)
        self.max_output = Gtk.SpinButton.new_with_range(1, 64 * 1024, 64)
        self.max_output.set_value(defaults.max_output // 1024)
        for row, (label, widget) in enumerate(((_("Hosts at a time"), self.concurrency),
                                               (_("Connect timeout (s)"), self.connect_timeout),
                                               (_("Command timeout (s, 0 = none)"), self.command_timeout),
                                               (_("Output kept per host (KiB)"), self.max_output))):
            text = Gtk.Label(label=label)
            text.set_xalign(0.0)
            grid.attach(text, 0, row, 1, 1)
            grid.attach(widget, 1, row, 1, 1)
            set_accessible_name(widget, label)
        self.stop_first = Gtk.CheckButton.new_with_mnemonic(_("Stop at the first _failure"))
        self.stop_first.set_active(defaults.stop_at_first_failure)
        self.pty = Gtk.CheckButton.new_with_mnemonic(_("Allocate a _terminal (PTY): the text runs as one -c string"))
        self.pty.set_active(defaults.pty)
        self.lang = Gtk.CheckButton.new_with_mnemonic(_("Run with _LANG=C.UTF-8"))
        self.lang.set_active(defaults.lang_c_utf8)
        jump = Gtk.Label(label=_("Jump hosts and keys are used as configured for each host."))
        jump.set_xalign(0.0)
        jump.get_style_context().add_class("dim-label")
        for row, widget in enumerate((self.stop_first, self.pty, self.lang, jump), start=4):
            grid.attach(widget, 0, row, 2, 1)
        self.options_box.add(grid)
        self.add_body(self.options_box)

        # ------------------------------------------------------- run
        run_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.run_button = Gtk.Button.new_with_mnemonic(_("_Run"))
        self.run_button.get_style_context().add_class("suggested-action")
        self.run_button.connect("clicked", lambda *_a: self.start())
        self.cancel_button = Gtk.Button.new_with_mnemonic(_("C_ancel Run"))
        self.cancel_button.connect("clicked", lambda *_a: self.cancel())
        self.cancel_button.set_sensitive(False)
        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(True)
        self.progress.set_text("")
        run_row.pack_start(self.run_button, False, False, 0)
        run_row.pack_start(self.cancel_button, False, False, 0)
        run_row.pack_start(self.progress, True, True, 0)
        self.add_body(run_row)

        self.results = ResultsView()
        self.add_body(self.results, expand=True)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=Space.S)
        self.result_buttons = {}
        for name, label, callback in (("copy", _("_Copy Results"), self.copy_results),
                                      ("export", _("_Export…"), self.export),
                                      ("rerun_failed", _("Re-run _Failed Only"), self.rerun_failed),
                                      ("rerun_all", _("Re-run A_ll"), self.rerun_all),
                                      ("sessions", _("Open _Sessions to Failed Hosts"), self.open_failed_sessions)):
            button = Gtk.Button.new_with_mnemonic(label)
            button.connect("clicked", lambda _b, cb=callback: cb())
            actions.pack_start(button, False, False, 0)
            self.result_buttons[name] = button
        self.add_body(actions)
        self.choose_export: Callable[[], Optional[str]] = self._choose_export  # test hook
        self.add_primary(_("_Close"), Gtk.ResponseType.CLOSE)
        self.connect("response", self._on_close_response)  # Close and the window's × (BaseDialog lets them pass)
        self.connect("destroy", lambda *_a: self._stop_drain())
        self.command.get_buffer().connect("changed", lambda *_a: self._sync())
        self._update_targets()
        self._fill_history()
        self._sync()

    # ------------------------------------------------- history and saved
    def _fill_history(self) -> None:
        self._filling = True
        self.history.remove_all()
        self.history.append("", _("History"))
        for text in self.controller.store().history():
            first = text.strip().splitlines()[0] if text.strip() else text
            self.history.append(text, first[:60] + ("…" if len(first) > 60 or "\n" in text.strip() else ""))
        self.history.set_active(0)
        self._filling = False

    def _on_history_chosen(self, combo: Gtk.ComboBoxText) -> None:
        text = combo.get_active_id()
        if text and not getattr(self, "_filling", False):
            self.set_command(text)

    def _fill_saved_menu(self) -> None:
        menu = self.saved_menu
        for child in menu.get_children():
            menu.remove(child)
        store = self.controller.store()

        def item(label: str, callback, sensitive: bool = True) -> None:
            entry = Gtk.MenuItem.new_with_mnemonic(label)
            entry.set_sensitive(sensitive)
            entry.connect("activate", lambda *_a: callback())
            menu.append(entry)

        item(_("_Save Current Command…"), self.save_current, store.persistent and bool(self.command_text.strip()))
        if not store.persistent:
            item(_("Saving needs a master password (Settings → Security)"), lambda: None, False)
        commands = store.commands()
        if commands:
            menu.append(Gtk.SeparatorMenuItem())
            for command in commands:
                item(command.name.replace("_", "__"), lambda c=command: self.load_saved(c))
            menu.append(Gtk.SeparatorMenuItem())
            for command in commands:
                item(_("Delete ‘{name}’").format(name=command.name.replace("_", "__")),
                     lambda c=command: self.delete_saved(c))
        menu.append(Gtk.SeparatorMenuItem())
        item(_("C_lear History"), self.clear_history, bool(store.history()))
        menu.show_all()

    def _ask_name(self) -> Optional[str]:
        return self.controller.win._prompt_text(_("Save Command"), _("Name for this command:"),
                                                accept_label=_("_Save"))

    def save_current(self):
        from ...multirun.store import CommandFileError

        name = self.ask_name()
        if not name or not name.strip():
            return None
        options = self.options()
        try:
            return self.controller.store().save_command(
                name, self.command_text, shell=options.shell,
                options={"concurrency": options.concurrency, "command_timeout": options.command_timeout,
                         "pty": options.pty})
        except (CommandFileError, OSError) as exc:
            self.controller.win._notify(_("The command was not saved ({reason}).").format(
                reason=getattr(exc, "kind", type(exc).__name__)), Level.WARNING)
            return None

    def load_saved(self, command) -> None:
        self.set_command(command.text)
        entry = self.shell.get_child()
        entry.set_text(command.shell)
        options = command.options or {}
        if "concurrency" in options:
            self.concurrency.set_value(int(options["concurrency"]))
        if "command_timeout" in options:
            self.command_timeout.set_value(float(options["command_timeout"]))
        if "pty" in options:
            self.pty.set_active(bool(options["pty"]))

    def delete_saved(self, command) -> None:
        self.controller.store().delete_command(command.id)

    def clear_history(self) -> None:
        self.controller.store().clear_history()
        self._fill_history()

    # ------------------------------------------------------------- targets
    def _update_targets(self) -> None:
        hosts = self.controller.host_by_key()
        chosen = [hosts[key] for key in self.keys if key in hosts]
        runnable = sum(1 for host in chosen if (getattr(host, "protocol", "ssh") or "ssh") == "ssh")
        text = ngettext("{n} host selected", "{n} hosts selected", len(chosen)).format(n=len(chosen))
        if runnable != len(chosen):
            text += " · " + ngettext("{n} is not an SSH host and is skipped", "{n} are not SSH hosts and are skipped",
                                     len(chosen) - runnable).format(n=len(chosen) - runnable)
        self.targets_label.set_text(text)
        self.runnable_count = runnable

    def _choose_targets(self) -> Optional[set]:
        from .multirun_targets import TargetChooserDialog

        return TargetChooserDialog(self, self.controller.win.manager.tree, self.keys,
                                   key=self.controller.win.creds.credential_key).ask()

    def change_targets(self) -> None:
        chosen = self.choose_targets()
        if chosen is not None:
            order = {key: index for index, key in enumerate(self.controller.host_by_key())}
            self.keys = sorted(chosen, key=lambda key: order.get(key, len(order)))
            self._update_targets()
            self._sync()

    # ------------------------------------------------------------- command
    @property
    def command_text(self) -> str:
        buffer = self.command.get_buffer()
        return buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)

    def set_command(self, text: str) -> None:
        self.command.get_buffer().set_text(text)

    def options(self) -> R.RunOptions:
        shell = (self.shell.get_active_text() or "sh").strip() or "sh"
        return R.RunOptions(concurrency=self.concurrency.get_value_as_int(),
                            connect_timeout=float(self.connect_timeout.get_value()),
                            command_timeout=float(self.command_timeout.get_value()),
                            stop_at_first_failure=self.stop_first.get_active(), pty=self.pty.get_active(),
                            lang_c_utf8=self.lang.get_active(), max_output=self.max_output.get_value_as_int() * 1024,
                            shell=shell)

    @property
    def running(self) -> bool:
        return self.run is not None and not self.run.done

    def _sync(self) -> None:
        running = self.running
        for widget in (self.command, self.change_button, self.options_box, self.tools):
            widget.set_sensitive(not running)
        self.run_button.set_sensitive(not running and bool(self.command_text.strip()) and self.runnable_count > 0)
        self.cancel_button.set_sensitive(running)
        finished = self.run is not None and not running
        failed = bool(self.results.keys_with(R.FAILURES)) if finished else False
        for name, button in getattr(self, "result_buttons", {}).items():
            button.set_sensitive(finished and (failed or name not in ("rerun_failed", "sessions")))

    # ---------------------------------------------------------------- run
    def approve(self, targets: List[R.Target], command: str) -> bool:
        """More than 5 hosts, or a command matching a dangerous pattern: ask first (``True`` to run)."""

        from ...multirun.safety import needs_confirmation

        if not needs_confirmation(len(targets), command):
            return True
        return self.confirm_run(targets, command)

    def _confirm_run(self, targets: List[R.Target], command: str) -> bool:
        from .multirun_confirm import RunConfirmDialog

        return RunConfirmDialog(self, targets, command).ask()

    def start(self, keys: Optional[Sequence[str]] = None) -> Optional[R.MultiRun]:
        command = self.command_text
        if not command.strip() or self.running:
            return None
        targets = self.controller.targets(keys if keys is not None else self.keys)
        if not targets or not self.approve(targets, command):
            return None
        options = self.options()
        self.controller.last_options = options
        self.run = R.MultiRun(targets, command, options, connect=self.controller.connect)
        self.results.reset(targets)
        self.run.start()
        self._started = time.monotonic()
        self._update_progress()
        self._stop_drain()
        self._drain_id = GLib.timeout_add(DRAIN_MS, self._drain)
        self._sync()
        self.on_started(command)
        return self.run

    def on_started(self, command: str) -> None:
        try:
            self.controller.store().add_history(command)  # encrypted, or memory only without a key
        except OSError as exc:
            self.controller.win._notify(_("The history could not be saved ({error}).").format(
                error=type(exc).__name__), Level.WARNING)
        self._fill_history()

    def cancel(self) -> None:
        if self.run is not None and not self.run.done:
            self.run.cancel()

    def _drain(self) -> bool:
        run = self.run
        if run is None:
            self._drain_id = 0
            return False
        latest = {}
        for _index in range(BATCH):
            try:
                key, result = run.events.get_nowait()
            except Exception:
                break
            latest[key] = result
        if latest:
            self.results.update(list(latest.values()))
        self._update_progress()
        if run.done and run.events.empty():
            self._drain_id = 0
            self._finished()
            return False
        return True

    def _update_progress(self) -> None:
        if self.run is None:
            return
        results = self.results.results.values()
        total = len(self.results.results)
        done = sum(1 for r in results if r.finished)
        self.progress.set_fraction(done / total if total else 0.0)
        self.progress.set_text(_("{done} / {total} done").format(done=done, total=total))

    def _finished(self) -> None:
        self.controller.log(R.audit_line(self.run))
        self._sync()
        self.on_finished()

    def on_finished(self) -> None:
        if self.results.group_toggle.get_active():
            self.results.rebuild_groups()

    # ------------------------------------------------------------ results
    def copy_results(self) -> str:
        from gi.repository import Gdk  # type: ignore

        from ...multirun.report import to_tsv

        text = to_tsv(self.results.ordered())
        Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(text, -1)
        return text

    def _choose_export(self) -> Optional[str]:
        from .files import choose_save_file

        return choose_save_file(self, _("Export Results (.csv, .json or .txt)"), current_name="results.csv")

    def export(self) -> Optional[str]:
        """The only way output reaches the disk: a file the user chose (CSV, JSON or text by its extension)."""

        from ...multirun import report

        name = self.choose_export()
        if not name:
            return None
        results = self.results.ordered()
        lower = name.lower()
        text = report.to_json(results) if lower.endswith(".json") else \
            report.to_text(results) if lower.endswith(".txt") else report.to_csv(results)
        from pathlib import Path

        from ...persistence import write_bytes_private

        write_bytes_private(Path(name), text.encode("utf-8"))  # 0600, atomic
        return name

    def rerun_failed(self) -> Optional[R.MultiRun]:
        keys = self.results.keys_with(R.FAILURES)
        return self.start(keys) if keys else None

    def rerun_all(self) -> Optional[R.MultiRun]:
        return self.start([key for key in self.results.results])

    def open_failed_sessions(self) -> None:
        self.controller.open_sessions(self.results.keys_with(R.FAILURES))

    def _stop_drain(self) -> None:
        if self._drain_id:
            GLib.source_remove(self._drain_id)
            self._drain_id = 0

    # -------------------------------------------------------------- close
    def _confirm_cancel(self) -> bool:
        from .confirm import confirm

        return confirm(self, _("Cancel the running command?"),
                       _("Hosts that have not started are skipped; running ones are stopped."),
                       confirm_label=_("Cancel the _Run"), destructive=True)

    def may_close(self) -> bool:
        """``False`` keeps the dialog open (a run is going and the user did not want to cancel it)."""

        if self.running:
            if not self.confirm_cancel():
                return False
            self.cancel()
        self.controller.dialog_size = tuple(self.get_size())
        return True

    def _on_close_response(self, dialog: Gtk.Dialog, response: int) -> None:
        if response in (Gtk.ResponseType.CLOSE, Gtk.ResponseType.DELETE_EVENT) and not self.may_close():
            dialog.stop_emission_by_name("response")

    def present_and_run(self) -> None:
        self.run_and_destroy()


__all__ = ["MultiRunDialog"]
