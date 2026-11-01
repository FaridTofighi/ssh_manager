"""Start-up: set or ask for the master password, open the keybox (the decisions are in ``ophion/unlock.py``).

* **First run** — "Set a master password" with **Set Master Password** (creates
  the keybox, then offers a recovery key) or **Don't Save Passwords**.
* **Unlock** — the data key remembered in the OS keyring ("Remember on this
  computer"), else the unlock dialog; the key derivation runs in a worker
  thread. "Forgot master password?" opens the keybox with the recovery key and
  sets a new password.
* **Key file missing** — quit (default), or start without the old stores
  (they are kept as ``*.orphaned-<time>``).
* **Migration** — older data is moved by ``gtkui/migration_flow.py``.

Key derivations take a few hundred milliseconds; :func:`run_off_thread` runs
them in a thread while the GTK main loop keeps going. The master password is
never kept after the keybox is open.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, Gtk  # type: ignore  # noqa: E402

from ..keybox import Keybox, KeyboxError, default_keybox_path  # noqa: E402
from ..unlock import (  # noqa: E402
    Startup,
    decide,
    inspect,
    orphan_stores,
    remembered_dek,
    usable_keyring,
)
from .dialogs.base import BaseDialog, DialogSize, is_accept  # noqa: E402
from .dialogs.secrets import NewSecretDialog, UnlockDialog  # noqa: E402
from .i18n import _  # noqa: E402
from .widgets.icon_button import set_accessible_name  # noqa: E402
from .widgets.secret_entry import SecretEntry  # noqa: E402

RESPONSE_OFF = 10
RESPONSE_SKIP = 11


def run_off_thread(func: Callable[[], Any]) -> Any:
    """``func()`` in a worker thread; the GTK main loop keeps running meanwhile. Re-raises its exception."""

    box: dict = {}

    def work() -> None:
        try:
            box["value"] = func()
        except BaseException as exc:  # handed back to the caller
            box["error"] = exc

    thread = threading.Thread(target=work, name="key-derivation", daemon=True)
    thread.start()
    while thread.is_alive():
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)
        time.sleep(0.005)
    if "error" in box:
        raise box["error"]
    return box.get("value")


@dataclass
class StartupResult:
    keybox: Optional[Keybox] = None
    passwords_off: bool = False
    migrate: bool = False  # older data to move (the migration flow runs next)
    resume: bool = False  # the keybox exists, the migration did not finish
    unfinished: bool = False  # safe mode: the move failed or was put off (old data untouched)
    typed: Optional[str] = field(default=None, repr=False)  # the password just typed (resume only; dropped after)
    report: Any = None


class RecoveryKeyDialog(BaseDialog):
    """Shows the recovery key once; the last two groups must be typed again (or **Skip**)."""

    def __init__(self, parent: Optional[Gtk.Window], code: str, *, can_skip: bool = True) -> None:
        BaseDialog.__init__(self, parent, _("Recovery Key"), size=DialogSize.SMALL, resizable=False)
        self.code = code
        intro = Gtk.Label(label=_("If you forget the master password, this key opens your saved passwords. Write "
                                  "this down and keep it somewhere safe. It is shown only now."))
        intro.set_xalign(0.0)
        intro.set_line_wrap(True)
        self.add_body(intro)
        self.code_label = Gtk.Label(label=code)
        self.code_label.set_selectable(True)
        self.code_label.get_style_context().add_class("mono")
        self.code_label.get_style_context().add_class("section-title")
        set_accessible_name(self.code_label, _("Recovery key"))
        self.add_body(self.code_label)
        copy = Gtk.Button.new_with_mnemonic(_("_Copy"))
        copy.set_halign(Gtk.Align.START)
        copy.connect("clicked", lambda *_a: Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(code, -1))
        self.add_body(copy)
        prompt = Gtk.Label(label=_("To confirm, type the last two groups:"))
        prompt.set_xalign(0.0)
        self.add_body(prompt)
        self.check = Gtk.Entry()
        self.check.set_placeholder_text("XXXX-XXXX")
        set_accessible_name(self.check, _("The last two groups of the recovery key"))
        self.check.set_activates_default(True)
        self.add_body(self.check)
        self.error = Gtk.Label()
        self.error.set_xalign(0.0)
        self.error.get_style_context().add_class("form-error")
        self.add_body(self.error)
        if can_skip:
            self.add_secondary(_("_Skip"), RESPONSE_SKIP)
        self.add_primary(_("_Done"))
        self.set_initial_focus(self.check)
        self.set_response_validator(lambda r: not is_accept(r) or self._confirmed())

    def _confirmed(self) -> bool:
        wanted = "".join(self.code.split("-")[-2:]).upper()
        typed = "".join(ch for ch in self.check.get_text().upper() if ch.isalnum())
        if typed != wanted:
            self.error.set_text(_("That does not match the last two groups."))
            return False
        return True

    def ask(self) -> bool:
        self.show_all()
        try:
            return is_accept(int(self.run()))
        finally:
            self.destroy()


def offer_recovery_key(parent: Optional[Gtk.Window], keybox: Keybox, *,
                       show: Optional[Callable[[str], bool]] = None) -> bool:
    """Create a recovery key and show it; ``True`` when the user confirmed it (a skipped one is removed)."""

    code = keybox.add_recovery()
    confirmed = (show or (lambda c: RecoveryKeyDialog(parent, c).ask()))(code)
    if not confirmed:
        keybox.remove_recovery()  # never leave a key behind the user did not write down
    return confirmed


def set_master_password(parent: Optional[Gtk.Window], settings, config_dir: Optional[Path] = None, *,
                        ask: Optional[Callable[[], Optional[str]]] = None,
                        show_recovery: Optional[Callable[[str], bool]] = None) -> Optional[Keybox]:
    """Saving passwords was off: create the keybox now (``None``: cancelled or failed, nothing changed)."""

    password = (ask or (lambda: NewSecretDialog(
        parent, _("Set Master Password"), _("Set a master password"),
        _("It encrypts your saved passwords and macros on this computer."),
        accept_label=_("_Set Master Password")).ask()))()
    if not password:
        return None
    flow = StartupFlow(settings, config_dir)
    if show_recovery is not None:
        flow.show_recovery = show_recovery
    else:
        flow.show_recovery = lambda code: RecoveryKeyDialog(parent, code).ask()
    return flow.create(password)


class StartupFlow:
    """One decision, then the dialogs it needs (tests replace the ``ask_*`` callables)."""

    def __init__(self, settings, config_dir: Optional[Path] = None, *, keyring_factory=usable_keyring) -> None:
        from ..settings import _default_config_dir

        self.settings = settings
        self.config_dir = Path(config_dir or _default_config_dir())
        self.keybox_path = self.config_dir / "keybox.json" if config_dir is not None else default_keybox_path()
        self.keyring_factory = keyring_factory
        self._typed: Optional[str] = None
        self.ask_first_run: Callable[[], tuple] = self._ask_first_run
        self.ask_password: Callable[[Keybox], tuple] = self._ask_password
        self.ask_recovery_code: Callable[[], Optional[str]] = self._ask_recovery_code
        self.ask_new_password: Callable[[], Optional[str]] = self._ask_new_password
        self.ask_key_missing: Callable[[], bool] = self._ask_key_missing
        self.show_recovery: Callable[[str], bool] = lambda code: RecoveryKeyDialog(None, code).ask()
        self.message: Callable[[str, str], None] = self._message

    def run(self) -> Optional[StartupResult]:
        """``None``: quit without changes."""

        decision = decide(inspect(self.config_dir, self.settings))
        if decision is Startup.PASSWORDS_OFF:
            return StartupResult(passwords_off=True)
        if decision is Startup.MIGRATE:
            return StartupResult(migrate=True)
        if decision is Startup.KEY_MISSING:
            if not self.ask_key_missing():
                return None
            orphan_stores(self.config_dir)
            return self.first_run()
        if decision is Startup.FIRST_RUN:
            return self.first_run()
        try:
            keybox = Keybox.load(self.keybox_path)
        except KeyboxError as exc:
            self.message(_("The key file cannot be used"), exc.message)
            return None
        if not self.unlock(keybox):
            return None
        resume = decision is Startup.RESUME
        typed, self._typed = (self._typed if resume else None), None
        return StartupResult(keybox=keybox, resume=resume, typed=typed)

    # ------------------------------------------------------------- first run
    def first_run(self) -> Optional[StartupResult]:
        choice, password = self.ask_first_run()
        if choice == "off":
            self.settings.passwords_off = True
            self.settings.save()
            return StartupResult(passwords_off=True)
        if choice != "set" or not password:
            return None
        keybox = self.create(password)
        return StartupResult(keybox=keybox) if keybox is not None else None

    def create(self, password: str) -> Optional[Keybox]:
        try:
            keybox = run_off_thread(lambda: Keybox.create(password, self.keybox_path))
        except KeyboxError as exc:
            self.message(_("The key file could not be created"), exc.message)
            return None
        self.settings.passwords_off = False
        offer_recovery_key(None, keybox, show=self.show_recovery)
        return keybox

    def _ask_first_run(self) -> tuple:
        dialog = NewSecretDialog(None, _("Set Master Password"), _("Set a master password"),
                                 _("It encrypts your saved passwords and macros on this computer. Without it, "
                                   "Ophion does not save passwords and asks for them every time."),
                                 accept_label=_("_Set Master Password"))
        dialog.add_secondary(_("_Don't Save Passwords"), RESPONSE_OFF)
        dialog.show_all()
        try:
            response = int(dialog.run())
            password = dialog.new_password
        finally:
            dialog.destroy()
        if response == RESPONSE_OFF:
            return "off", None
        return ("set", password) if is_accept(response) and password else ("cancel", None)

    # ---------------------------------------------------------------- unlock
    def unlock(self, keybox: Keybox) -> bool:
        keyring = self.keyring_factory()
        dek = remembered_dek(keyring, keybox.id)
        if dek is not None and keybox.unlock_with_dek(dek):
            return True  # "Remember on this computer"; any failure falls back to the dialog silently
        while True:
            password, forgot = self.ask_password(keybox)
            if password is not None:
                self._typed = password  # kept only to finish an interrupted migration
                return True  # the dialog unlocked the keybox while verifying
            if not forgot:
                return False
            if self.recover(keybox):
                return True

    def recover(self, keybox: Keybox) -> bool:
        code = self.ask_recovery_code()
        if not code:
            return False
        if not run_off_thread(lambda: keybox.unlock_with_recovery(code)):
            self.message(_("The recovery key is not correct"), _("Check it and try again."))
            return False
        new = self.ask_new_password()
        if not new:
            keybox.forget()
            return False
        run_off_thread(lambda: keybox.reset_password(new))
        return True

    def _ask_password(self, keybox: Keybox) -> tuple:
        dialog = UnlockDialog(None, _("Unlock Ophion"), _("Enter your master password"),
                              _("It opens your saved passwords and macros."), keybox.unlock, in_thread=True,
                              error_text=_("Incorrect master password. Please try again."),
                              forgot_label=_("_Forgot master password?"))
        password = dialog.ask()
        return password, dialog.forgot

    def _ask_recovery_code(self) -> Optional[str]:
        dialog = BaseDialog(None, _("Recovery Key"), size=DialogSize.SMALL, resizable=False)
        label = Gtk.Label(label=_("Enter the recovery key you wrote down when the master password was set."))
        label.set_xalign(0.0)
        label.set_line_wrap(True)
        dialog.add_body(label)
        entry = SecretEntry(_("Recovery key"))
        entry.entry.set_activates_default(True)
        dialog.add_body(entry)
        dialog.add_cancel()
        dialog.add_primary(_("_Continue"))
        dialog.show_all()
        try:
            return entry.get_text() if is_accept(int(dialog.run())) else None
        finally:
            dialog.destroy()

    def _ask_new_password(self) -> Optional[str]:
        return NewSecretDialog(None, _("New Master Password"), _("Choose a new master password"),
                               _("The recovery key stays valid."), accept_label=_("_Save")).ask()

    def _ask_key_missing(self) -> bool:
        dialog = Gtk.MessageDialog(modal=True, message_type=Gtk.MessageType.WARNING,
                                   text=_("The key file keybox.json is missing"))
        dialog.format_secondary_text(_(
            "Your saved passwords cannot be opened. Restore keybox.json from a backup, or start without them "
            "(the old files are kept next to the new ones as *.orphaned-<time>)."))
        dialog.add_button(_("Start _Without Them"), 1)
        dialog.add_button(_("_Quit"), 2)
        dialog.set_default_response(2)
        try:
            return dialog.run() == 1
        finally:
            dialog.destroy()

    def _message(self, title: str, detail: str) -> None:
        dialog = Gtk.MessageDialog(modal=True, message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK,
                                   text=title)
        dialog.format_secondary_text(detail)
        try:
            dialog.run()
        finally:
            dialog.destroy()


__all__ = ["StartupFlow", "set_master_password", "StartupResult", "RecoveryKeyDialog", "offer_recovery_key", "run_off_thread"]
