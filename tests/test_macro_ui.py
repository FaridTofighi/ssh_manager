"""Terminal macros in the window: record → save → replay, secrets, shortcuts, edit / delete, failure modes."""

import logging
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.gtk_window_fixture import display_available

PASSWORD = "master-pw-for-tests"
SECRET = "hunter2-SENTINEL-secret"


def pump(seconds=0.0, until=None):
    from gi.repository import Gtk  # type: ignore

    deadline = time.monotonic() + seconds
    while True:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        if until is not None and until():
            return True
        if time.monotonic() >= deadline:
            return until() if until is not None else True
        time.sleep(0.01)


def key_event(widget, accel: str):
    from gi.repository import Gdk, Gtk  # type: ignore

    keyval, mods = Gtk.accelerator_parse(accel)
    event = Gdk.Event.new(Gdk.EventType.KEY_PRESS)
    event.key.keyval, event.key.state = keyval, mods
    entries = Gdk.Keymap.get_for_display(Gdk.Display.get_default()).get_entries_for_keyval(keyval)[1]
    if entries:
        event.key.hardware_keycode, event.key.group = entries[0].keycode, entries[0].group
    event.key.window = widget.get_window()
    event.set_device(Gdk.Display.get_default().get_default_seat().get_keyboard())
    unicode = Gdk.keyval_to_unicode(keyval)
    event.key.string = chr(unicode) if unicode and not mods else ""
    event.key.length = len(event.key.string)
    return event


@unittest.skipUnless(display_available(), "needs a display")
class MacroWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.win.realize()
        self.tabs = self.win.terminal_tabs
        self.macros = self.win.macros
        self.logs = self.enterContext(self.assertLogs(level=logging.DEBUG))
        logging.getLogger("ophion").debug("macro test")  # assertLogs needs at least one record
        self.win._settings.local_shell.command = "/bin/sh"
        self.win._settings.local_shell.on_exit = "hold"
        self.box = self.sandbox.use_keybox(PASSWORD)
        self.macros.start()
        self.assertTrue(pump(10, until=lambda: self.macros.state == "ready"), self.macros.state)
        self.dialogs = []

    def tearDown(self) -> None:
        for tab in list(self.tabs.tabs()):
            with mock.patch("ophion.gtkui.local_terminal.confirm", return_value=True):
                self.tabs.close_tab(tab)
        pump(0.2)

    # ---------------------------------------------------------------- helpers
    def shell(self):
        tab = self.tabs.new_local_terminal()
        self.assertTrue(pump(5, until=lambda: tab._shell_pid is not None and tab.is_session_active()))
        self.win.set_focus(tab._terminal)
        pump(0.3)
        return tab

    def text(self, tab) -> str:
        from ophion.gtk_terminal import terminal_text

        return terminal_text(tab._terminal)

    def type(self, term, text: str, gap: float = 0.0) -> None:
        """What VTE does for a key press: the key event, then the bytes to the program (``commit``).

        (The sandbox window is never mapped, so real key events cannot reach the terminal.)
        """

        for char in text:
            self.macros._on_key_press(term, None)
            term.feed_child(char.encode())
            pump(gap)

    def save_with(self, name="", shortcut="", **kw):
        """Replace the dialog's run: fill it like the user would, keep a record of what it showed."""

        from gi.repository import Gtk  # type: ignore

        def run(dialog):
            self.dialogs.append({"rows": dialog.step_rows(), "conflict": dialog.conflict_label.get_text()
                                 if dialog.conflict_pending else "", "macro": dialog.macro.copy()})
            if name:
                dialog.name_entry.set_text(name)
            if shortcut:
                dialog.set_shortcut(shortcut)
                self.dialogs[-1]["conflict"] = dialog.conflict_label.get_text() if dialog.conflict_pending else ""
            for step in kw.get("edit", []):
                step(dialog)
            if kw.get("replace"):
                dialog.replace_conflict()
            self.dialogs[-1]["can_save"] = dialog.can_save()
            self.dialogs[-1]["conflict_after"] = dialog.conflict_label.get_text() if dialog.conflict_pending else ""
            response = kw.get("response", Gtk.ResponseType.OK if dialog.can_save() else Gtk.ResponseType.CANCEL)
            values = dialog.values() if response == Gtk.ResponseType.OK else None
            replace = list(dialog.replace_owners)
            dialog.replace_owners = replace
            dialog.destroy()
            return response, values

        return mock.patch.object(self.macros, "run_dialog", side_effect=run)

    def record(self, tab, text: str, *, name="Say hi", shortcut="<Primary><Shift>F7", gap=0.0):
        # (Ctrl+Alt+<key> does not match synthetic key events in this X session; Ctrl+Shift does.)
        self.assertTrue(self.macros.start_recording())
        self.type(tab._terminal, text, gap)
        pump(0.3)
        with self.save_with(name, shortcut):
            self.macros.stop_recording()
            self.assertTrue(pump(5, until=lambda: self.macros.store.name_taken(name) or self.dialogs))
            pump(0.2)
        return next((m for m in self.macros.store.list_sorted() if m.name == name), None)

    def add_macro(self, name, shortcut=None, steps=None):
        from ophion.macros import Macro

        macro = Macro(name=name, shortcut=shortcut or [], steps=steps or [{"type": "text", "text": "echo m\r"}])
        self.macros.store.add(macro)
        self.macros.store.save()
        self.macros.sync()
        return macro

    def all_text(self) -> str:
        return "\n".join(self.logs.output) + "\n".join(m.text for m in self.win._toasts.history.items())

    # ------------------------------------------------------------------ tests
    def test_record_save_and_replay_with_the_shortcut(self) -> None:
        from ophion.gtkui.controllers.macro_controller import macro_action

        tab = self.shell()
        before = self.text(tab).count("\nhi")
        macro = self.record(tab, "echo hi\r")
        self.assertIsNotNone(macro, self.dialogs)
        self.assertEqual(macro.shortcut, ["<Primary><Shift>F7"])
        typed = "".join(s["text"] for s in macro.steps if s["type"] == "text")  # split by timing under load
        self.assertEqual((typed, [s.get("key") for s in macro.steps if s["type"] == "key"]), ("echo hi", ["Enter"]))
        self.assertIn("echo", self.dialogs[0]["rows"][0])
        app = self.win.get_application()
        self.assertEqual(app.get_accels_for_action(macro_action(macro.id)), ["<Primary><Shift>F7"])
        self.assertTrue(pump(3, until=lambda: self.text(tab).count("\nhi") > before))
        after_recording = self.text(tab).count("\nhi")
        self.win.set_focus(tab._terminal)
        self.win.activate_key(key_event(self.win, "F8").key)  # the first synthetic key never matches (keymap load)
        self.assertTrue(self.win.activate_key(key_event(self.win, "<Primary><Shift>F7").key))
        self.assertTrue(pump(5, until=lambda: self.text(tab).count("\nhi") > after_recording), self.text(tab)[-300:])
        self.assertFalse(self.macros.playbacks)
        # The file holds none of it in plain text.
        raw = self.macros.store.path.read_bytes()
        for plain in (b"Say hi", b"echo hi", b"<Primary><Shift>F7"):
            self.assertNotIn(plain, raw)
        # Not focused on a terminal: the shortcut does nothing.
        self.win.set_focus(self.win.sidebar.search_entry if hasattr(self.win, "sidebar") else None)
        with mock.patch.object(self.macros, "run_macro") as run:
            self.macros.run_from_shortcut(macro.id)
        run.assert_not_called()

    def test_secret_steps_are_flagged_masked_and_never_leak(self) -> None:
        from ophion.gtkui.dialogs.macros import MASK

        tab = self.shell()
        tab._terminal.feed_child(b"stty -echo; read p; stty echo; echo got:$p\n")
        self.assertTrue(pump(5, until=lambda: self.macros.is_secret_input(tab, tab._terminal)))
        macro = self.record(tab, SECRET + "\r", name="Login")
        self.assertTrue(pump(3, until=lambda: "got:" + SECRET in self.text(tab)))  # the program got it
        self.assertIsNotNone(macro)
        secret_steps = [s for s in macro.steps if s.get("secret")]
        self.assertTrue(secret_steps)
        self.assertEqual("".join(s["text"] for s in secret_steps if s["type"] == "text"), SECRET)
        rows = "\n".join(self.dialogs[0]["rows"])
        self.assertIn(MASK, rows)
        self.assertNotIn(SECRET, rows)
        raw = self.macros.store.path.read_bytes() + self.macros.store.backup_path.read_bytes() \
            if self.macros.store.backup_path.exists() else self.macros.store.path.read_bytes()
        self.assertNotIn(SECRET.encode(), raw)
        self.assertNotIn(SECRET, self.all_text())
        config = Path(os.environ["XDG_CONFIG_HOME"])
        for path in config.rglob("*"):
            if path.is_file():
                self.assertNotIn(SECRET.encode(), path.read_bytes(), path)
        # The menu shows names only.
        labels = [item.label for section in self.macros.menu_sections() for item in section.items]
        self.assertNotIn(SECRET, " ".join(labels))

    def test_playback_is_not_user_input(self) -> None:
        tab = self.shell()
        seen = []
        tab._terminal.connect("commit", lambda *_a: seen.append(tab._macro_feeding))
        observed = mock.Mock()
        tab.set_input_observer(observed)  # broadcast listens here
        macro = self.add_macro("Plain", steps=[{"type": "text", "text": "echo played\r"}])
        self.assertTrue(self.macros.run_macro(macro.id))
        self.assertTrue(pump(3, until=lambda: "\nplayed" in self.text(tab)))
        self.assertTrue(seen and all(seen))  # VTE's commit happens while the guard is up
        observed.assert_not_called()

    def test_duplicate_shortcuts_are_reported_and_replace_works(self) -> None:
        registry = self.win.shortcut_registry
        other = self.add_macro("Other", ["<Primary><Alt>j"])
        tab = self.shell()
        # A built-in action: named, blocks saving until replaced.
        self.assertTrue(self.macros.start_recording())
        self.type(tab._terminal, "a")
        with self.save_with("First", "<Primary><Shift>c"):
            self.macros.stop_recording()
            pump(1, until=lambda: self.dialogs)
        self.assertIn("Copy", self.dialogs[-1]["conflict_after"])
        self.assertFalse(self.dialogs[-1]["can_save"])
        self.assertFalse(self.macros.store.name_taken("First"))
        # Another macro, then Replace: the other owner loses it.
        self.dialogs.clear()
        self.assertTrue(self.macros.start_recording())
        self.type(tab._terminal, "b")
        with self.save_with("Second", "<Primary><Alt>j", replace=True):
            self.macros.stop_recording()
            pump(3, until=lambda: self.macros.store.name_taken("Second"))
        self.assertEqual(self.dialogs[-1]["conflict"], "This shortcut is already assigned to “Other”.")
        self.assertTrue(self.dialogs[-1]["can_save"])
        self.assertEqual(self.macros.store.get(other.id).shortcut, [])
        second = next(m for m in self.macros.store.list_sorted() if m.name == "Second")
        self.assertEqual(registry.conflicts("<Primary><Alt>j"), [f"macro:{second.id}"])
        # Replace a built-in action's key.
        self.dialogs.clear()
        self.assertTrue(self.macros.start_recording())
        self.type(tab._terminal, "c")
        with self.save_with("Third", "<Primary><Shift>c", replace=True):
            self.macros.stop_recording()
            pump(3, until=lambda: self.macros.store.name_taken("Third"))
        self.assertNotIn("<Primary><Shift>c", registry.effective_accels("copy"))
        self.assertIn("<Primary><Shift>c", [a for m in self.macros.store.list_sorted() for a in m.shortcut])
        # The Keyboard Shortcuts page sees the macro and does not take its key.
        from ophion.gtkui.dialogs.shortcuts_page import ShortcutEditor

        editor = ShortcutEditor(registry)
        result = editor.assign("paste", "<Primary><Alt>j", replace=True)
        self.assertFalse(result.ok)
        self.assertEqual(editor.title(result.conflicts[0]), "Second")

    def test_conflict_messages_name_the_owner(self) -> None:
        from ophion.gtkui.dialogs.macros import MacroDialog
        from ophion.macros import Macro

        owner = self.add_macro("Owner", ["<Primary><Alt>k"])
        macro = Macro(name="New", steps=[{"type": "text", "text": "x"}])
        dialog = MacroDialog(self.win, macro, recording=True, name_error=self.macros.name_error(macro.id),
                             conflicts=self.macros.conflicts(macro.id))
        self.addCleanup(dialog.destroy)
        dialog.set_shortcut("<Primary><Alt>k")
        self.assertEqual(dialog.conflict_label.get_text(), "This shortcut is already assigned to “Owner”.")
        self.assertFalse(dialog.can_save())
        dialog.set_shortcut("<Primary><Shift>v")
        self.assertIn("“Paste”", dialog.conflict_label.get_text())
        dialog.set_shortcut("<Primary><Alt>F3")
        self.assertFalse(dialog.conflict_pending)
        self.assertTrue(dialog.can_save())
        dialog.set_shortcut("x")  # allowed, with a warning
        self.assertIn("no longer receive", dialog.shortcut_hint.get_text())
        dialog.name_entry.set_text("owner")
        self.assertFalse(dialog.can_save())
        self.assertIn("already exists", dialog.name_message.get_text())
        self.assertTrue(owner)

    def test_edit_cancel_and_delete(self) -> None:
        from gi.repository import Gtk  # type: ignore

        from ophion.gtkui.controllers.macro_controller import macro_action

        macro = self.add_macro("Edit me", ["<Primary><Alt>e"], steps=[
            {"type": "text", "text": "one"}, {"type": "delay", "ms": 100}, {"type": "text", "text": "two"},
            {"type": "key", "key": "Enter"}])
        original = self.macros.store.get(macro.id).to_dict()

        def change(dialog):
            dialog.select_step(0)
            with mock.patch("ophion.gtkui.dialogs.macros.prompt_text", return_value="ONE"):
                dialog.edit_step()
            dialog.select_step(1)
            dialog.delete_step()
            dialog.select_step(1)
            dialog.move_step(1)

        with self.save_with("Edit me", "<Primary><Alt>e", edit=[change], response=Gtk.ResponseType.CANCEL):
            self.assertFalse(self.macros.edit_macro(macro.id))
        self.assertEqual(self.macros.store.get(macro.id).to_dict(), original)
        with self.save_with("Edited", "<Primary><Alt>r", edit=[change]):
            self.assertTrue(self.macros.edit_macro(macro.id))
        edited = self.macros.store.get(macro.id)
        self.assertEqual(edited.name, "Edited")
        self.assertEqual(edited.steps, [{"type": "text", "text": "ONE"}, {"type": "key", "key": "Enter"},
                                        {"type": "text", "text": "two"}])
        app = self.win.get_application()
        self.assertEqual(app.get_accels_for_action(macro_action(macro.id)), ["<Primary><Alt>r"])
        with mock.patch.object(self.macros, "confirm", return_value=True) as ask:
            self.assertTrue(self.macros.delete_macro(macro.id))
        self.assertIn("Edited", ask.call_args.args[1])
        self.assertIsNone(self.macros.store.get(macro.id))
        self.assertEqual(app.get_accels_for_action(macro_action(macro.id)), [])
        self.assertEqual(self.win.shortcut_registry.conflicts("<Primary><Alt>r"), [])
        from ophion.macros import MacroStore

        reread = MacroStore(self.macros.store.path)
        reread.unlock_with_key(self.box.subkey("macros"), self.box.id)
        self.assertEqual(len(reread), 0)

    def test_manage_window_lists_filters_and_runs(self) -> None:
        from ophion.gtkui.dialogs.macros import MacrosWindow

        self.add_macro("Alpha", ["<Primary><Alt>a"])
        self.add_macro("Beta")
        window = MacrosWindow(self.win, self.macros.store.list_sorted, on_edit=lambda *_a: None,
                              on_duplicate=self.macros.duplicate_macro, on_delete=lambda *_a: None)
        self.addCleanup(window.destroy)
        self.assertEqual(window.names(), ["Alpha", "Beta"])
        window.search.set_text("bet")
        window.filter.refilter()
        self.assertEqual(window.names(), ["Beta"])
        window.search.set_text("")
        window.filter.refilter()
        window.select(self.macros.store.list_sorted()[1].id)
        window.buttons["duplicate"].clicked()
        window.refresh()
        self.assertEqual(window.names(), ["Alpha", "Beta", "Beta (copy)"])

    def test_a_session_excluded_from_broadcast_runs_its_macro_alone(self) -> None:
        # Since macros follow broadcast (tests/test_macro_broadcast.py), an excluded session -- local
        # terminals are excluded by default -- still receives only its own macro.
        first = self.shell()
        second = self.shell()
        self.tabs.set_broadcast_mode(True)
        pump(0.3)
        macro = self.add_macro("Here", steps=[{"type": "text", "text": "echo only-here\r"}])
        self.assertTrue(self.macros.run_macro(macro.id, tab=first, term=first._terminal))
        self.assertTrue(pump(3, until=lambda: "\nonly-here" in self.text(first)))
        pump(0.5)
        self.assertNotIn("only-here", self.text(second))
        self.tabs.set_broadcast_mode(False)

    def test_refused_when_disconnected_and_stopped_when_the_tab_closes(self) -> None:
        from gi.repository import GLib  # type: ignore

        tab = self.shell()
        tab._terminal.feed_child(b"exit\n")
        self.assertTrue(pump(5, until=lambda: not tab.is_session_active()))
        macro = self.add_macro("Slow", steps=[{"type": "text", "text": "echo a\r"}, {"type": "delay", "ms": 4000},
                                              {"type": "text", "text": "echo b\r"}])
        self.assertFalse(self.macros.run_macro(macro.id, tab=tab, term=tab._terminal))
        self.assertIn("Open a connected session first.", [m.text for m in self.win._toasts.history.items()])
        running = self.shell()
        self.assertTrue(self.macros.run_macro(macro.id, tab=running, term=running._terminal))
        self.assertFalse(self.macros.run_macro(macro.id, tab=running, term=running._terminal))  # no overlap
        player = self.macros.playbacks[running].player
        pump(0.3)
        source = player._source
        self.assertTrue(source)
        with mock.patch("ophion.gtkui.local_terminal.confirm", return_value=True):
            self.tabs.close_tab(running)
        pump(0.3)
        self.assertFalse(self.macros.playbacks)
        self.assertFalse(player.running)
        self.assertIsNone(GLib.main_context_default().find_source_by_id(source))
        # Stop from the banner's button.
        third = self.shell()
        self.assertTrue(self.macros.run_macro(macro.id, tab=third, term=third._terminal))
        pump(0.2)
        self.macros.stop_playback()
        self.assertFalse(self.macros.playbacks)

    def test_recording_handlers_exist_only_while_recording(self) -> None:
        tab = self.shell()
        self.assertTrue(self.macros.start_recording())
        handlers = list(self.macros.recording.handlers)
        self.assertTrue(all(obj.handler_is_connected(h) for obj, h in handlers))
        other = self.add_macro("Not now")
        self.assertFalse(self.macros.run_macro(other.id))  # no playback while recording
        self.assertIn("Stop the recording before running a macro.", [m.text for m in self.win._toasts.history.items()])
        with mock.patch.object(self.macros, "_offer_save") as offer:
            self.macros.stop_recording()  # nothing typed: nothing to save
            pump(0.2)
        offer.assert_not_called()
        self.assertFalse(any(obj.handler_is_connected(h) for obj, h in handlers))
        self.assertIn("Nothing was recorded, so no macro was saved.", [m.text for m in self.win._toasts.history.items()])
        # Closing the recorded tab stops the recording and offers to save.
        self.assertTrue(self.macros.start_recording())
        self.type(tab._terminal, "ls")
        pump(0.2)
        with mock.patch.object(self.macros, "_offer_save") as offer:
            with mock.patch("ophion.gtkui.local_terminal.confirm", return_value=True):
                self.tabs.close_tab(tab)
            pump(0.5)
        self.assertIsNone(self.macros.recording)
        offer.assert_called_once()

    def test_dont_save_passwords_is_blocked_while_macros_exist(self) -> None:
        security = self.win.security
        security.keyring_factory = lambda: None
        security.confirm = lambda *_a: True
        self.add_macro("Keep me")
        self.assertFalse(security.set_passwords_off(True))
        self.assertIsNotNone(self.win.keybox)
        self.assertTrue(self.macros.store.path.exists())
        self.assertTrue(any("master password stays on" in m.text for m in self.win._toasts.history.items()))
        # With no macros left, the (empty) file is removed and saving can be turned off.
        for macro in self.macros.store.list_sorted():
            self.macros.delete_macro(macro.id, ask=False)
        self.assertTrue(security.set_passwords_off(True))
        self.assertIsNone(self.win.keybox)
        self.assertTrue(self.win._settings.passwords_off)
        self.assertFalse(self.macros.store.path.exists())
        self.assertFalse(self.macros.store.backup_path.exists())
        self.assertFalse(self.box.path.exists())

    def test_master_password_change_keeps_the_macros(self) -> None:
        from ophion import keybox as K
        from ophion.macros import MacroStore

        self.add_macro("Survivor")
        before = self.macros.store.path.read_bytes()
        with mock.patch("ophion.gtkui.controllers.app_dialogs.NewSecretDialog") as dialog:
            dialog.return_value.ask.return_value = "a new master password"
            self.win.app_dialogs.change_master_password()
        self.assertEqual(self.macros.store.path.read_bytes(), before)  # only the key file changes
        loaded = K.Keybox.load(self.box.path)
        self.assertFalse(loaded.unlock(PASSWORD))
        self.assertTrue(loaded.unlock("a new master password"))
        reread = MacroStore(self.macros.store.path)
        reread.unlock_with_key(loaded.subkey("macros"), loaded.id)
        self.assertEqual([m.name for m in reread.list_sorted()], ["Survivor"])
        # A failed write keeps the previous password.
        with mock.patch("ophion.persistence.write_bytes_private", side_effect=OSError(28, "No space")):
            with mock.patch("ophion.gtkui.controllers.app_dialogs.NewSecretDialog") as dialog:
                dialog.return_value.ask.return_value = "third password"
                self.win.app_dialogs.change_master_password()
        self.assertTrue(K.Keybox.load(self.box.path).unlock("a new master password"))
        self.assertIsNone(getattr(self.win, "_master_password", None))


class SecretDetectionTests(unittest.TestCase):
    """Echo off in canonical mode (local prompts), or a password prompt on screen (ssh's raw pty)."""

    def detect(self, lflag, tail):
        import termios

        from ophion.gtkui.controllers.macro_controller import MacroController

        term = mock.Mock()
        if lflag is None:
            term.get_pty.return_value = None
        tab = mock.Mock()
        tab._terminal_tail.return_value = tail
        with mock.patch.object(termios, "tcgetattr", return_value=[0, 0, 0, lflag or 0, 0, 0, []]):
            return MacroController.is_secret_input(tab, term)

    def test_rules(self) -> None:
        import termios

        cooked_no_echo = termios.ICANON
        raw = 0  # ssh's own pty, readline editing: no echo, no canonical mode
        self.assertTrue(self.detect(cooked_no_echo, "$ "))
        self.assertFalse(self.detect(termios.ICANON | termios.ECHO, "$ "))
        self.assertFalse(self.detect(raw, "user@web:~$ "))
        self.assertTrue(self.detect(raw, "[sudo] password for user: "))
        self.assertTrue(self.detect(raw, "Enter passphrase for key '/home/u/.ssh/id_ed25519': "))
        self.assertTrue(self.detect(None, "user@host's password: "))  # no pty: the screen decides
        self.assertFalse(self.detect(None, "ls"))


@unittest.skipUnless(display_available(), "needs a display")
class MacroWithoutMasterPasswordTests(unittest.TestCase):
    def setUp(self) -> None:
        from tests.gtk_window_fixture import WindowSandbox

        self.sandbox = WindowSandbox(store=True)  # no keybox: "Don't save passwords"
        self.addCleanup(self.sandbox.close)
        self.win = self.sandbox.window
        self.win.realize()
        self.macros = self.win.macros
        self.win._settings.local_shell.command = "/bin/sh"

    def tearDown(self) -> None:
        for tab in list(self.win.terminal_tabs.tabs()):
            with mock.patch("ophion.gtkui.local_terminal.confirm", return_value=True):
                self.win.terminal_tabs.close_tab(tab)
        pump(0.2)

    def test_saving_offers_a_master_password_or_discard_and_writes_nothing(self) -> None:
        self.assertEqual(self.macros.state, "locked")
        tab = self.win.terminal_tabs.new_local_terminal()
        self.assertTrue(pump(5, until=lambda: tab.is_session_active()))
        self.assertTrue(self.macros.start_recording())
        self.macros._on_key_press(None, None)
        self.macros._on_commit(tab._terminal, "ls", 2)
        with mock.patch.object(self.macros, "ask_master_password_needed", return_value=False) as ask, \
                mock.patch.object(self.macros, "run_dialog") as run:
            self.macros.stop_recording()
            pump(0.5)
        ask.assert_called_once()
        run.assert_not_called()
        self.assertFalse(self.macros.store.path.exists())
        self.assertFalse(any(Path(os.environ["XDG_CONFIG_HOME"]).rglob("macros.enc*")))
        # "Set master password…" turns it on and then shows the save dialog.
        self.assertTrue(self.macros.start_recording())
        self.macros._on_key_press(None, None)
        self.macros._on_commit(tab._terminal, "ls", 2)
        from ophion import keybox as K
        from ophion.secure_store import EncryptedFileKeyring

        with mock.patch.object(self.macros, "ask_master_password_needed", return_value=True), \
                mock.patch("ophion.gtkui.unlock_flow.NewSecretDialog") as new, \
                mock.patch("ophion.gtkui.unlock_flow.RecoveryKeyDialog") as recovery, \
                mock.patch.object(self.macros, "run_dialog", return_value=(0, None)) as run:
            new.return_value.ask.return_value = "fresh master"
            recovery.return_value.ask.return_value = False  # skipped
            self.macros.stop_recording()
            self.assertTrue(pump(10, until=lambda: run.called))
        self.assertIsNotNone(self.win.keybox)
        self.assertIsInstance(self.win._secure_store.backend, EncryptedFileKeyring)
        self.assertTrue(K.Keybox.load(self.win.keybox.path).unlock("fresh master"))
        self.assertIsNone(getattr(self.win, "_master_password", None))

    def test_a_damaged_file_at_start_up_is_kept(self) -> None:
        path = self.macros.store.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'{"format": "ssh-manager-macros", "version": 2, "trunc')
        self.sandbox.use_keybox(PASSWORD)
        with mock.patch.object(self.macros, "ask_damaged", return_value="keep") as ask:
            self.macros.start()
            self.assertTrue(pump(10, until=lambda: self.macros.state != "unlocking"))
        ask.assert_called_once()
        self.assertEqual(ask.call_args.args[1], "damaged")
        self.assertEqual(self.macros.state, "disabled")
        self.assertEqual(path.read_bytes(), b'{"format": "ssh-manager-macros", "version": 2, "trunc')
        with mock.patch.object(self.macros, "ask_damaged", return_value="empty"):
            self.macros.state = "locked"
            self.macros.start()
            self.assertTrue(pump(10, until=lambda: self.macros.state == "ready"))
        moved = list(path.parent.glob("macros.enc.damaged-*"))
        self.assertEqual(len(moved), 1)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
