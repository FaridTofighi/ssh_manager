"""Terminal macros in the window: recording, playback, the encrypted store, menus and shortcuts.

* **Recording** listens to VTE's ``commit`` (what the terminal sends to its
  program) on the terminal that was active when it started, only while it
  records; the handlers are always disconnected when it stops. A keystroke is
  *secret* when the terminal does not echo (``termios``: ECHO off in canonical
  mode, e.g. ``sudo`` or ``read -s`` in a local shell) or when the screen ends
  with a password prompt (ssh keeps its own pty in raw mode, so the remote
  prompt is what tells). Recorded steps live only in the recorder until Save.
* **Playback** types into the sessions typed input would reach: the current
  session, plus (broadcast on, the current session included) every other
  included, connected session (``TerminalNotebook.macro_targets``, the same
  rule as broadcast). The set is frozen when the run starts. Each session gets
  its own player (a *run group*); steps go straight to the terminal through
  :meth:`TerminalTab.macro_send`, so they are never mirrored a second time and
  never seen by the password-change heuristics. Players run from the GLib main
  loop; each stops when its Stop is pressed, its session ends or its tab
  closes, and *Stop All* stops the group.
* **The store** (``macros.enc``) is unlocked with the master password typed at
  start-up, in a thread (PBKDF2 takes a few hundred ms). Without a master
  password macros cannot be saved: the user sets one or discards the macro.
* **Shortcuts:** every macro is an extra entry of the window's
  :class:`ShortcutRegistry` (``win.run-macro('<id>')``), so conflicts, the
  shortcuts window and the Keyboard Shortcuts page include it. A macro's
  shortcut does nothing unless a terminal has the keyboard focus.

Nothing here logs, notifies or shows the content of a step.
"""

from __future__ import annotations

import logging
import termios
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import GLib, Gtk, Vte  # type: ignore  # noqa: E402

from ...auth_flow import ends_with_password_prompt  # noqa: E402
from ...macros import (  # noqa: E402
    Macro,
    MacroError,
    MacroFileError,
    MacroPlayer,
    MacroRecorder,
    MacroStore,
    MacroWriteError,
    default_macros_path,
)
from ...master_auth import hash_master_password, verify_master_password  # noqa: E402
from ..constants import Icons  # noqa: E402
from ..dialogs import macros as dialogs  # noqa: E402
from ..dialogs.confirm import confirm  # noqa: E402
from ..dialogs.secrets import NewSecretDialog, UnlockDialog  # noqa: E402
from ..dialogs.shortcuts_page import accel_text  # noqa: E402
from ..i18n import _, ngettext  # noqa: E402
from ..menu_spec import MenuItemSpec, MenuSection  # noqa: E402
from ..theme.tokens import Level  # noqa: E402
from .shortcut_registry import ExtraEntry  # noqa: E402
from .shortcuts import G_MACROS, accel_label  # noqa: E402

LOGGER = logging.getLogger(__name__)

TYPED_WINDOW = 0.3  # s: a commit this soon after a key press on the terminal was typed, else pasted
FOCUS_REPORTS = ("\x1b[I", "\x1b[O")  # focus in / out reports are not user input


def macro_key(macro_id: str) -> str:
    return f"macro:{macro_id}"


def macro_action(macro_id: str) -> str:
    return f"win.run-macro('{macro_id}')"


class _Recording:
    def __init__(self, tab, term: Vte.Terminal, recorder: MacroRecorder) -> None:
        self.tab = tab
        self.term = term
        self.recorder = recorder
        self.handlers: List[Tuple[object, int]] = []
        self.timer = 0
        self.bar: Optional[Gtk.InfoBar] = None
        self.last_key = 0.0


class _Group:
    """One macro run: a player per target session (they are not synchronised after the start)."""

    def __init__(self, name: str, source, sessions: int) -> None:
        self.name = name
        self.source = source
        self.sessions = sessions  # every session the run was meant for (skipped ones included)
        self.playbacks: List["_Playback"] = []
        self.results: Dict[object, str] = {}  # tab → done | stopped | disconnected | closed | busy
        self.labels: Dict[object, str] = {}

    @property
    def running(self) -> bool:
        return any(p.player is not None and p.player.running for p in self.playbacks)


class _Playback:
    def __init__(self, tab, term: Vte.Terminal, name: str, group: Optional[_Group] = None) -> None:
        self.tab = tab
        self.term = term
        self.name = name
        self.group = group
        self.player: Optional[MacroPlayer] = None
        self.handlers: List[Tuple[object, int]] = []
        self.bar: Optional[Gtk.InfoBar] = None
        self.label: Optional[Gtk.Label] = None
        self.reason = ""


class _Sink:
    """Where the player's steps go: the playback's terminal, while its session runs."""

    def __init__(self, playback: _Playback) -> None:
        self._playback = playback

    def _alive(self) -> bool:
        playback = self._playback
        if playback.tab.is_session_active():
            return True
        playback.reason = "disconnected"
        if playback.player is not None:
            playback.player.stop()
        return False

    def feed(self, data: bytes) -> None:
        if self._alive():
            self._playback.tab.macro_send(self._playback.term, data)

    def paste(self, text: str) -> None:
        if self._alive():
            self._playback.tab.macro_send(self._playback.term, paste=text)


_RESPONSE_STOP_ALL = 1


def _info_bar(text: str, stop: Callable[[], None], stop_label: str,
              stop_all: Optional[Callable[[], None]] = None) -> Tuple[Gtk.InfoBar, Gtk.Label]:
    bar = Gtk.InfoBar()
    bar.set_message_type(Gtk.MessageType.WARNING)
    bar.get_style_context().add_class("macro-bar")
    label = Gtk.Label(label=text)
    label.set_xalign(0.0)
    label.set_line_wrap(True)
    label.set_selectable(False)
    bar.get_content_area().pack_start(label, True, True, 0)
    button = bar.add_button(stop_label, Gtk.ResponseType.CLOSE)
    button.get_style_context().add_class("destructive-action")
    if stop_all is not None:
        everything = bar.add_button(_("Stop _All"), _RESPONSE_STOP_ALL)
        everything.set_tooltip_text(_("Stop the macro in every session it runs in"))
        everything.get_style_context().add_class("destructive-action")
    bar.connect("response", lambda _bar, response: stop_all() if stop_all is not None and
                response == _RESPONSE_STOP_ALL else stop())
    bar.show_all()
    return bar, label


ERROR_TEXT = {
    "empty_name": _("Enter a name."),
    "long_name": _("The name is too long."),
    "duplicate_name": _("A macro with this name already exists."),
    "no_steps": _("The macro has no steps."),
    "too_many_steps": _("The macro has too many steps."),
    "too_much_text": _("The macro holds too much text."),
    "too_many_macros": _("There are too many macros; delete one first."),
}


class MacroController:
    """Owns the macro store, the recording (at most one) and the playbacks (one per tab)."""

    def __init__(self, win, store: Optional[MacroStore] = None) -> None:
        self.win = win
        self.store = store if store is not None else MacroStore(default_macros_path())
        self.state = "locked"  # locked (no master password), unlocking, ready, disabled (file kept unopened)
        self.recording: Optional[_Recording] = None
        self.playbacks: Dict[object, _Playback] = {}
        self.groups: List[_Group] = []
        self.ask_run_on_sessions = dialogs.ask_run_on_sessions
        self.ask_damaged = dialogs.ask_damaged_file  # tests replace the dialogs
        self.ask_master_password_needed = dialogs.ask_master_password_needed
        self.run_dialog = dialogs.run_macro_dialog
        self.confirm = confirm
        self._after_unlock: List[Callable[[], None]] = []
        self._told_locked = False
        self.unlock_seconds = 0.0

    # =========================================================== set-up
    def bind(self) -> None:
        """Connect to the notebook (toolbar button, menu) once the window's UI exists."""

        tabs = self.win.terminal_tabs
        tabs.on_record_clicked = self.toggle_recording
        tabs.macro_menu = self.menu_sections
        tabs.refresh_overflow_menu()
        self.win.connect("destroy", lambda *_a: self.shutdown())

    def callbacks(self) -> Dict[str, Callable]:
        return {
            "macro-record-toggle": self.toggle_recording,
            "macro-manage": self.open_manager,
            "macro-stop-playback": self.stop_playback,
            "run-macro": self.run_from_shortcut,
        }

    def start(self) -> None:
        """Open the macros with the keybox's ``macros`` key (or, before the keybox, the typed master password)."""

        keybox = getattr(self.win, "keybox", None)
        if keybox is not None and not keybox.locked:
            self.unlock_with_keybox(keybox)
            return
        password = self._master_password()
        if password is None:
            self.state = "locked"
            self.sync()
            return
        self.unlock(password)

    def _master_password(self) -> Optional[str]:
        if self.win._settings.master_password_disabled or not self.win._settings.master_password_hash:
            return None
        return self.win._master_password or None

    def unlock(self, password: str, then: Optional[Callable[[], None]] = None) -> None:
        if then is not None:
            self._after_unlock.append(then)
        if self.state == "unlocking":
            return
        self.state = "unlocking"
        self.sync()
        store = self.store

        def work() -> None:
            began = time.perf_counter()
            error: Optional[str] = None
            try:
                store.unlock(password)
            except MacroFileError as exc:
                error = exc.kind
            except Exception as exc:  # never the content: only the type
                LOGGER.warning("macros: unlocking failed (%s)", type(exc).__name__)
                error = "damaged"
            self.unlock_seconds = time.perf_counter() - began
            GLib.idle_add(self._unlocked, error)

        threading.Thread(target=work, name="macro-unlock", daemon=True).start()

    def unlock_with_keybox(self, keybox, then: Optional[Callable[[], None]] = None) -> None:
        """No key derivation (the keybox is unlocked already): synchronous."""

        if then is not None:
            self._after_unlock.append(then)
        try:
            self.store.unlock_with_key(keybox.subkey("macros"), keybox.id)
            error: Optional[str] = None
        except MacroFileError as exc:
            error = exc.kind
        self._unlocked(error)

    def _unlocked(self, error: Optional[str]) -> bool:
        if getattr(self.win, "closing", False):
            return False
        if error == "legacy":  # the migration converts it: not damaged, just not there yet
            self.store.keep_damaged()
            self.state = "disabled"
            self._after_unlock.clear()
            self._notify(_("Macros will be available once the migration to the new key is finished "
                           "(Settings → Security)."), Level.WARNING)
        elif error is None:
            self.state = "ready"
        else:
            LOGGER.warning("macros: %s cannot be opened (%s)", self.store.path.name, error)
            if self.ask_damaged(self.win, error) == "empty":
                self.store.start_empty()
                self.state = "locked"
                keybox = getattr(self.win, "keybox", None)
                if keybox is not None and not keybox.locked:
                    self.unlock_with_keybox(keybox)
                    return False
                password = self._master_password()
                if password is not None:
                    self.unlock(password)
                    return False
            else:
                self.store.keep_damaged()
                self.state = "disabled"
                self._after_unlock.clear()
        self.sync()
        pending, self._after_unlock = self._after_unlock, []
        if self.state == "ready":
            for callback in pending:
                callback()
        return False

    def shutdown(self) -> None:
        if self.recording is not None:
            self._end_recording()
        for playback in list(self.playbacks.values()):
            if playback.player is not None:
                playback.player.stop()
        self.store.lock()

    # ======================================================= presentation
    def sync(self) -> None:
        """Shortcuts (registry extras) and menus follow the store."""

        registry = getattr(self.win, "shortcut_registry", None)
        if registry is not None:
            macros = self.store.list_sorted() if self.state == "ready" else []
            registry.set_extras({
                macro_key(m.id): ExtraEntry(m.name, G_MACROS, macro_action(m.id), tuple(m.shortcut)) for m in macros})
        self.win.terminal_tabs.refresh_overflow_menu()

    def menu_sections(self) -> List[MenuSection]:
        recording = self.recording is not None
        items = [MenuItemSpec(_("Stop _Recording") if recording else _("_Record Macro"), self.toggle_recording,
                              Icons.STOP if recording else Icons.RECORD, accel_label("macro-record-toggle"))]
        current = self.win.terminal_tabs.current_tab()
        if current in self.playbacks:
            items.append(MenuItemSpec(_("Stop the Running _Macro"), self.stop_playback, Icons.STOP,
                                      accel_label("macro-stop-playback")))
        if self.state == "ready":
            entries = [MenuItemSpec(m.name.replace("_", "__"), lambda mid=m.id: self.run_macro(mid),
                                    accel=accel_text(m.shortcut), sensitive=not recording)
                       for m in self.store.list_sorted()]
            entries = entries or [MenuItemSpec(_("No macros yet"), sensitive=False)]
        else:
            entries = [MenuItemSpec({
                "unlocking": _("Unlocking macros…"),
                "disabled": _("Macros are off: the macros file cannot be opened"),
            }.get(self.state, _("Macros need a master password")), sensitive=False)]
        items.append(MenuItemSpec(_("Run _Macro"), submenu=[MenuSection("macros", entries)]))
        items.append(MenuItemSpec(_("Manage _Macros…"), self.open_manager, accel=accel_label("macro-manage")))
        return [MenuSection("macros", items)]

    def _notify(self, message: str, level: Level = Level.INFO) -> None:
        self.win._notify(message, level)

    def _label(self, tab) -> str:
        try:
            return self.win.terminal_tabs.label_for(tab)
        except Exception:
            return getattr(tab, "title", "") or _("this session")

    # ========================================================== recording
    def toggle_recording(self) -> None:
        if self.recording is not None:
            self.stop_recording()
        else:
            self.start_recording()

    def _current_terminal(self):
        tab = self.win.terminal_tabs.current_tab()
        if tab is None or not getattr(tab, "is_terminal", False) or not tab.is_session_active():
            return None, None
        return tab, tab.active_terminal

    def start_recording(self, *, tab=None, term: Optional[Vte.Terminal] = None,
                        recorder: Optional[MacroRecorder] = None) -> bool:
        if self.recording is not None:
            return False
        if tab is None:
            tab, term = self._current_terminal()
        if tab is None or term is None or not tab.is_session_active():
            self._notify(_("Open a connected session first."), Level.WARNING)
            return False
        if tab in self.playbacks:
            self._notify(_("Wait until the macro finishes, or stop it."), Level.WARNING)
            return False
        recording = _Recording(tab, term, recorder or MacroRecorder())
        recording.handlers = [
            (term, term.connect("commit", self._on_commit)),
            (term, term.connect("key-press-event", self._on_key_press)),
            (term, term.connect("destroy", lambda *_a: self._recording_target_gone())),
            (tab, tab.connect("status-changed", self._on_recording_status)),
            (tab, tab.connect("destroy", lambda *_a: self._recording_target_gone())),
        ]
        text = _("Recording in {host}. Everything you type is saved, including passwords.").format(
            host=self._label(tab))
        mirrored = len(self.win.terminal_tabs.macro_targets(tab)) - 1  # broadcast: what typing reaches
        if mirrored > 0:
            text += "\n" + ngettext("Broadcast is on: what you type is also sent to {n} other session.",
                                    "Broadcast is on: what you type is also sent to {n} other sessions.",
                                    mirrored).format(n=mirrored)
        recording.bar, _label = _info_bar(text, self.stop_recording, _("_Stop Recording"))
        tab.attach_info_bar(recording.bar)
        recording.timer = GLib.timeout_add(500, self._tick_recording)
        self.recording = recording
        self._show_recording()
        self.win.terminal_tabs.refresh_overflow_menu()
        return True

    def _status_text(self) -> str:
        recording = self.recording
        if recording is None:
            return ""
        seconds = int(recording.recorder.elapsed())
        steps = recording.recorder.step_count
        return _("{m}:{s:02d} · {n} steps").format(m=seconds // 60, s=seconds % 60, n=steps)

    def _show_recording(self) -> None:
        recording = self.recording
        if recording is not None:
            self.win.terminal_tabs.set_macro_recording(recording.tab, True, "● " + self._status_text())

    def _tick_recording(self) -> bool:
        if self.recording is None:
            return False
        self._show_recording()
        return True

    def _on_key_press(self, _term, _event) -> bool:
        if self.recording is not None:
            self.recording.last_key = time.monotonic()
        return False

    def _on_commit(self, term: Vte.Terminal, text: str, _length: int) -> None:
        recording = self.recording
        if recording is None or term is not recording.term or not text or text in FOCUS_REPORTS:
            return
        if getattr(recording.tab, "_macro_feeding", False):
            return  # a macro playing, not the user
        typed = time.monotonic() - recording.last_key < TYPED_WINDOW
        paste = not typed and len(text) > 1 and not text.startswith("\x1b")
        recording.recorder.add(text, secret=self.is_secret_input(recording.tab, term), paste=paste)

    @staticmethod
    def is_secret_input(tab, term: Vte.Terminal) -> bool:
        """The terminal does not echo what is typed (password prompts)."""

        try:
            pty = term.get_pty()
            lflag = termios.tcgetattr(pty.get_fd())[3] if pty is not None else None
        except (AttributeError, OSError, termios.error):
            lflag = None
        if lflag is not None and not lflag & termios.ECHO and lflag & termios.ICANON:
            return True  # e.g. sudo, read -s, passwd in a local shell
        try:
            return ends_with_password_prompt(tab._terminal_tail(term))  # ssh: the remote prompt
        except Exception:
            return False

    def _on_recording_status(self, _tab, _alias: str, connected: bool) -> None:
        if not connected and self.recording is not None:
            self._notify(_("The session ended, so the recording stopped."), Level.WARNING)
            self.stop_recording()

    def _recording_target_gone(self) -> None:
        if self.recording is not None:
            self.stop_recording()

    def _end_recording(self) -> Optional[_Recording]:
        recording, self.recording = self.recording, None
        if recording is None:
            return None
        for obj, handler in recording.handlers:
            try:
                obj.disconnect(handler)
            except (TypeError, ValueError):
                pass
        recording.handlers.clear()
        if recording.timer:
            GLib.source_remove(recording.timer)
            recording.timer = 0
        if recording.bar is not None:
            recording.bar.destroy()
            recording.bar = None
        self.win.terminal_tabs.set_macro_recording(recording.tab, False)
        self.win.terminal_tabs.refresh_overflow_menu()
        return recording

    def stop_recording(self) -> None:
        recording = self._end_recording()
        if recording is None:
            return
        if getattr(self.win, "closing", False):
            recording.recorder.clear()
            return
        if not recording.recorder.steps():
            recording.recorder.clear()
            self._notify(_("Nothing was recorded, so no macro was saved."))
            return
        GLib.idle_add(lambda: self._offer_save(recording) and False)  # after the click / key that stopped it

    def _offer_save(self, recording: _Recording) -> None:
        if self.state != "ready":
            if not self._ensure_unlocked(lambda: self._offer_save(recording)):
                recording.recorder.clear()
            return
        steps = recording.recorder.steps()
        macro = Macro(name=self._suggest_name(), steps=steps)
        while True:
            dialog = dialogs.MacroDialog(self.win, macro, recording=True, name_error=self.name_error(macro.id),
                                         conflicts=self.conflicts(macro.id), verify_master=self.verify_master)
            response, values = self.run_dialog(dialog)
            replace = list(dialog.replace_owners)
            if response == dialogs.RESPONSE_KEEP_RECORDING:
                if recording.tab.get_parent() is None or not self.start_recording(
                        tab=recording.tab, term=recording.term, recorder=recording.recorder):
                    self._notify(_("The session is gone, so recording cannot go on."), Level.WARNING)
                    continue
                return
            if values is not None:
                if self._store_new(values, replace):
                    recording.recorder.clear()
                    return
                macro = values
                continue
            if response == Gtk.ResponseType.DELETE_EVENT and not self.confirm(
                    self.win, _("Discard the recording?"), _("The recorded steps are deleted."),
                    confirm_label=_("_Discard"), destructive=True):
                continue
            recording.recorder.clear()
            for step in steps:
                step.clear()
            return

    def _suggest_name(self) -> str:
        number = len(self.store) + 1
        while self.store.name_taken(_("Macro {n}").format(n=number)):
            number += 1
        return _("Macro {n}").format(n=number)

    # ============================================== master password and store
    def _ensure_unlocked(self, then: Callable[[], None]) -> bool:
        """Make the store usable (asking for a master password when there is none); ``then`` runs once it is."""

        if self.state == "ready":
            then()
            return True
        if self.state == "unlocking":
            self._after_unlock.append(then)
            self._notify(_("Unlocking macros…"))
            return True
        if self.state == "disabled":
            self.win._notify_error(_("Macros are off for this session: the macros file cannot be opened."))
            return False
        if not self.ask_master_password_needed(self.win):
            return False
        password = self.set_master_password()
        if password is None:
            return False
        self.unlock(password, then)
        return True

    def set_master_password(self) -> Optional[str]:
        """Turn the master password on (create one, or confirm the existing one) as Preferences would."""

        settings = self.win._settings
        if settings.master_password_hash:
            stored = settings.master_password_hash
            password = UnlockDialog(self.win, _("Turn On the Master Password"), _("Enter your master password"),
                                    _("It encrypts your macros and the passwords stored on this computer."),
                                    lambda text: verify_master_password(text, stored),
                                    error_text=_("Incorrect master password. Please try again.")).ask()
        else:
            password = NewSecretDialog(self.win, _("Set Master Password"), _("Create a master password"),
                                       _("It encrypts your macros and the passwords stored on this computer."),
                                       accept_label=_("_Save")).ask()
            if password is not None:
                settings.master_password_hash = hash_master_password(password)
        if password is None:
            return None
        settings.master_password_disabled = False
        settings.save()
        self.win._master_password = password
        self.win.creds.rekey_password_file(password)
        return password

    def verify_master(self, text: str) -> bool:
        return verify_master_password(text, self.win._settings.master_password_hash)

    def rekey(self, new_password: str) -> bool:
        """Before the master password changes: re-encrypt the macros (``False``: keep the old password)."""

        if self.state == "unlocking":
            self.win._notify_error(_("The macros are still being unlocked. Try again in a moment."))
            return False
        if self.state != "ready":
            return True  # nothing open to re-encrypt (a file that could not be opened stays as it is)
        try:
            self.store.rekey(new_password)
        except MacroWriteError as exc:
            LOGGER.warning("macros: re-encrypting failed (%s)", exc)
            self.win._notify_error(_("The macros could not be re-encrypted, so the master password was not "
                                     "changed. Check that the configuration folder can be written."))
            return False
        return True

    def allow_disabling_master_password(self) -> bool:
        """Macros are never stored unencrypted: turning the master password off needs no macros."""

        if self.state == "ready" and self.store.remove_file_if_empty():
            return True
        if self.state != "ready" and not self.store.file_exists():
            return True
        self.win._notify_error(_("The master password stays on: it encrypts your macros. Delete all macros "
                                 "(Manage Macros) first to turn it off."))
        return False

    def master_password_enabled(self) -> None:
        if self.state == "locked":
            self.start()

    def _save(self) -> bool:
        try:
            self.store.save()
        except MacroWriteError as exc:
            reason = str(exc)
            LOGGER.warning("macros: saving failed (%s)", reason)
            if reason == "changed_key":
                text = _("The macros could not be saved: another SSH Manager window changed the master password. "
                         "Restart SSH Manager.")
            elif reason.startswith("external_"):
                text = _("The macros could not be saved: macros.enc was changed and cannot be read.")
            else:
                text = _("The macros could not be saved: the configuration folder cannot be written. "
                         "They are kept until you quit; the next change saves them again.")
            self.win._notify_error(text)
            return False
        return True

    def name_error(self, macro_id: str) -> Callable[[str], Optional[str]]:
        def check(name: str) -> Optional[str]:
            name = name.strip()
            if not name:
                return ERROR_TEXT["empty_name"]
            if self.store.name_taken(name, ignoring=macro_id):
                return ERROR_TEXT["duplicate_name"]
            return None

        return check

    def conflicts(self, macro_id: str) -> Callable[[str], List[Tuple[str, str]]]:
        def check(accel: str) -> List[Tuple[str, str]]:
            registry = getattr(self.win, "shortcut_registry", None)
            if registry is None:
                return []
            return [(owner, registry.title(owner)) for owner in registry.conflicts(accel, ignoring=macro_key(macro_id))]

        return check

    def _take_shortcut_from(self, owners: List[str], accel: str) -> None:
        registry = self.win.shortcut_registry
        for owner in owners:
            if owner.startswith("macro:"):
                other = self.store.get(owner.split(":", 1)[1])
                if other is not None:
                    other = other.copy()
                    other.shortcut = [a for a in other.shortcut if a != accel]
                    self.store.update(other)
            else:
                registry.set_accels(owner, [a for a in registry.effective_accels(owner) if a != accel])

    def _store_new(self, macro: Macro, replace: List[str]) -> bool:
        try:
            if replace and macro.shortcut:
                self._take_shortcut_from(replace, macro.shortcut[0])
            self.store.add(macro)
        except MacroError as exc:
            self.win._notify_error(ERROR_TEXT.get(exc.code, _("The macro cannot be saved.")))
            return False
        self._save()  # on failure the macro stays in memory (retried with the next change)
        self.sync()
        self._notify(_("Macro “{name}” saved.").format(name=macro.name), Level.SUCCESS)
        return True

    # ============================================================ playback
    def run_from_shortcut(self, macro_id: str) -> None:
        """A macro's shortcut: only into the terminal that has the keyboard focus."""

        focus = self.win.get_focus()
        if not isinstance(focus, Vte.Terminal):
            return  # never type into a terminal that does not have the focus
        tab = next((t for t in self.win.terminal_tabs.tabs()
                    if getattr(t, "is_terminal", False) and focus in t._terminals()), None)
        if tab is not None:
            self.run_macro(macro_id, tab=tab, term=focus)

    def run_macro(self, macro_id: str, *, tab=None, term: Optional[Vte.Terminal] = None) -> bool:
        """Run on ``tab`` (default: the current session) and, with broadcast, on the sessions typing reaches."""

        if self.state != "ready":
            self._ensure_unlocked(lambda: None)
            return False
        macro = self.store.get(macro_id)
        if macro is None:
            return False
        if self.recording is not None:
            self._notify(_("Stop the recording before running a macro."), Level.WARNING)
            return False
        if tab is None:
            tab, term = self._current_terminal()
        notebook = self.win.terminal_tabs
        targets = notebook.macro_targets(tab) if tab is not None and term is not None else []
        if not targets or not tab.is_session_active():
            self._notify(_("Open a connected session first."), Level.WARNING)
            return False
        if self._busy(tab):
            self._notify(_("A macro is already running in this session."), Level.WARNING)
            return False
        busy = [t for t in targets[1:] if self._busy(t)]
        runnable = [t for t in targets if t not in busy]  # frozen: later broadcast changes do not matter
        unreachable = notebook.macro_unreachable(tab)
        if not self._confirm_run(macro, runnable):
            return False
        group = _Group(macro.name, tab, len(targets) + len(unreachable))
        for skipped, reason in [(t, "busy") for t in busy] + [(t, "disconnected") for t in unreachable]:
            group.results[skipped] = reason
            group.labels[skipped] = self._label(skipped)
        self.groups.append(group)
        for target in runnable:
            self._start_playback(group, macro, target, term if target is tab else target.active_terminal,
                                 stop_all=len(runnable) > 1 and target is tab)
        self.win.terminal_tabs.refresh_overflow_menu()
        for playback in list(group.playbacks):  # all start in the same main-loop iteration
            if playback.player is not None:
                playback.player.start()
        return True

    def _busy(self, tab) -> bool:
        return tab in self.playbacks or any(g.source is tab and g.running for g in self.groups)

    def _confirm_run(self, macro: Macro, runnable: List[object]) -> bool:
        """One question at most: the multi-session one (it covers "Ask before running it" too)."""

        settings = self.win._settings.macros
        if len(runnable) > 1 and (settings.confirm_multi_session or macro.confirm_before_run):
            run, dont_ask = self.ask_run_on_sessions(self.win, macro.name, [self._label(t) for t in runnable])
            if run and dont_ask and settings.confirm_multi_session:
                settings.confirm_multi_session = False
                self.win._settings.save()
            return run
        if macro.confirm_before_run:
            return self.confirm(self.win, _("Run macro “{name}” in {host}?").format(
                name=macro.name, host=self._label(runnable[0])), confirm_label=_("_Run"))
        return True

    def _start_playback(self, group: _Group, macro: Macro, tab, term: Vte.Terminal, *, stop_all: bool) -> None:
        playback = _Playback(tab, term, macro.name, group)
        group.labels[tab] = self._label(tab)
        playback.bar, playback.label = _info_bar(
            self._progress_text(playback, 0, 0), lambda: self._stop(playback), _("_Stop"),
            (lambda: self.stop_group(group)) if stop_all else None)
        tab.attach_info_bar(playback.bar)
        playback.handlers = [
            (tab, tab.connect("status-changed", lambda _t, _a, connected: None if connected else self._stop(
                playback, "disconnected"))),
            (tab, tab.connect("destroy", lambda *_a: self._stop(playback, "closed"))),
            (term, term.connect("destroy", lambda *_a: self._stop(playback, "closed"))),
        ]
        playback.player = MacroPlayer(
            macro.steps, _Sink(playback), GLib.timeout_add, GLib.source_remove, timing=macro.timing,
            on_progress=lambda index, total: self._progress(playback, index, total),
            on_finished=lambda completed: self._finished(playback, completed))
        group.playbacks.append(playback)
        self.playbacks[tab] = playback

    def stop_group(self, group: _Group) -> None:
        for playback in list(group.playbacks):
            self._stop(playback)

    def _progress_text(self, playback: _Playback, index: int, total: int) -> str:
        return _("Running macro “{name}” (step {i}/{n})").format(name=playback.name, i=index, n=total)

    def _progress(self, playback: _Playback, index: int, total: int) -> None:
        if playback.label is not None:
            playback.label.set_text(self._progress_text(playback, index, total))

    def _stop(self, playback: _Playback, reason: str = "stopped") -> None:
        if playback.player is not None and playback.player.running:
            playback.reason = playback.reason or reason
            playback.player.stop()

    def _finished(self, playback: _Playback, completed: bool) -> None:
        for obj, handler in playback.handlers:
            try:
                obj.disconnect(handler)
            except (TypeError, ValueError):
                pass
        playback.handlers.clear()
        if playback.bar is not None:
            playback.bar.destroy()
            playback.bar = playback.label = None
        if self.playbacks.get(playback.tab) is playback:
            del self.playbacks[playback.tab]
        group = playback.group
        if group is not None:
            group.results[playback.tab] = "done" if completed else (playback.reason or "stopped")
        closing = getattr(self.win, "closing", False)
        if not closing:
            self.win.terminal_tabs.refresh_overflow_menu()
        if group is not None and not group.running and group in self.groups:
            self.groups.remove(group)
            if not closing:
                self._group_finished(group)

    def _group_finished(self, group: _Group) -> None:
        """One toast per run (a single session: only when its session ended under it, as before)."""

        failed = [(tab, reason) for tab, reason in group.results.items() if reason != "done"]
        if group.sessions <= 1:
            if failed and failed[0][1] == "disconnected":
                self._notify(_("The macro “{name}” stopped: the session ended.").format(name=group.name),
                             Level.WARNING)
            return
        if not failed:
            self._notify(_("Macro “{name}” finished on {n} sessions.").format(name=group.name, n=group.sessions),
                         Level.SUCCESS)
            return
        reasons = {"disconnected": _("disconnected"), "busy": _("busy"), "closed": _("closed"),
                   "stopped": _("stopped")}
        names = ", ".join(f"{group.labels.get(tab, '?')} ({reasons.get(reason, reason)})" for tab, reason in failed)
        self._notify(_("Macro “{name}” finished on {done} of {n} sessions; {sessions} did not complete.").format(
            name=group.name, done=group.sessions - len(failed), n=group.sessions, sessions=names), Level.WARNING)

    def stop_playback(self) -> None:
        current = self.win.terminal_tabs.current_tab()
        targets = [self.playbacks[current]] if current in self.playbacks else list(self.playbacks.values())
        for playback in targets:
            self._stop(playback)

    # ===================================================== manage / edit
    def open_manager(self) -> None:
        if self.state != "ready":
            self._ensure_unlocked(self.open_manager)
            return
        window = dialogs.MacrosWindow(self.win, self.store.list_sorted, on_edit=self.edit_macro,
                                      on_duplicate=self.duplicate_macro, on_delete=self.delete_macro,
                                      confirm_multi_session=(self.win._settings.macros.confirm_multi_session,
                                                             self.set_confirm_multi_session))
        window.show_all()
        try:
            response = int(window.run())
            request = window.run_request
        finally:
            window.destroy()
        if response == dialogs.RESPONSE_RUN and request:
            self.run_macro(request)

    def set_confirm_multi_session(self, active: bool) -> None:
        self.win._settings.macros.confirm_multi_session = bool(active)
        self.win._settings.save()

    def edit_macro(self, macro_id: str, parent: Optional[Gtk.Window] = None) -> bool:
        macro = self.store.get(macro_id)
        if macro is None:
            return False
        dialog = dialogs.MacroDialog(parent or self.win, macro, recording=False, name_error=self.name_error(macro_id),
                                     conflicts=self.conflicts(macro_id), verify_master=self.verify_master)
        response, values = self.run_dialog(dialog)
        if values is None:
            return False
        before = macro.copy()
        try:
            if dialog.replace_owners and values.shortcut:
                self._take_shortcut_from(list(dialog.replace_owners), values.shortcut[0])
            self.store.update(values)
        except MacroError as exc:
            self.store.restore(before)
            self.win._notify_error(ERROR_TEXT.get(exc.code, _("The macro cannot be saved.")))
            return False
        self._save()
        self.sync()
        return True

    def duplicate_macro(self, macro_id: str) -> Optional[Macro]:
        macro = self.store.get(macro_id)
        if macro is None:
            return None
        copy = Macro.from_dict({**macro.to_dict(), "id": Macro().id, "shortcut": []})
        name = _("{name} (copy)").format(name=macro.name)[:80]
        number = 2
        while self.store.name_taken(name):
            name = _("{name} (copy {n})").format(name=macro.name, n=number)[:80]
            number += 1
        copy.name = name
        try:
            self.store.add(copy)
        except MacroError as exc:
            self.win._notify_error(ERROR_TEXT.get(exc.code, _("The macro cannot be saved.")))
            return None
        self._save()
        self.sync()
        return copy

    def delete_macro(self, macro_id: str, parent: Optional[Gtk.Window] = None, *, ask: bool = True) -> bool:
        macro = self.store.get(macro_id)
        if macro is None:
            return False
        if ask and not self.confirm(parent or self.win, _("Delete the macro “{name}”?").format(name=macro.name),
                                    _("Its steps and its shortcut are removed."), confirm_label=_("_Delete"),
                                    destructive=True):
            return False
        self.store.delete(macro_id)
        if not self._save():
            self.store.restore(macro)
            return False
        self.sync()
        return True


__all__ = ["MacroController", "macro_action", "macro_key"]
