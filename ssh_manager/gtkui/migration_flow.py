"""The move of older data onto the keybox: the dialogs around ``ssh_manager/migration.py``.

One explanation, then a master password for the keybox (step 1 of the migration):

* a master password was set → type it (checked against the old hash) and keep it;
* it was set but "don't ask on startup" was on → choose one again; with a usable OS keyring the new
  key is remembered on this computer so start-up stays prompt-free, without one it must be typed
  (a key file next to the data would protect nothing);
* none was set → create one; **Don't Save Passwords** only while there are no saved secrets,
  otherwise **Not Now**.

Cancelling, **Not Now** or a failure start the app in safe mode: the old data untouched, saved
passwords unavailable, a banner that leads to Preferences → Security → Finish migration.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # type: ignore  # noqa: E402

from ..keybox import Keybox, KeyboxError  # noqa: E402
from ..migration import (  # noqa: E402
    MARKER,
    Migration,
    MigrationError,
    legacy_password_ok,
    needs_legacy_password,
    secure_remove,
    write_marker,
)
from ..unlock import inspect, remember, usable_keyring  # noqa: E402
from .dialogs.base import is_accept  # noqa: E402
from .dialogs.secrets import NewSecretDialog, UnlockDialog  # noqa: E402
from .i18n import _  # noqa: E402
from .unlock_flow import RESPONSE_OFF, RecoveryKeyDialog, StartupResult, offer_recovery_key, run_off_thread  # noqa: E402

LOGGER = logging.getLogger(__name__)

RESPONSE_LATER = 12


def intro_text() -> str:
    return _("SSH Manager now encrypts saved passwords and macros with one key protected by your master "
             "password. Your data will be moved safely; nothing is deleted until the move is verified.")


class MigrationFlow:
    """Tests replace the ``ask_*`` callables and ``message``."""

    def __init__(self, settings, config_dir: Path, manager=None, *, keyring_factory=usable_keyring,
                 app_version: str = "", parent: Optional[Gtk.Window] = None) -> None:
        self.settings = settings
        self.config_dir = Path(config_dir)
        self.manager = manager
        self.keyring_factory = keyring_factory
        self.app_version = app_version
        self.parent = parent
        self.ask_intro: Callable[[], bool] = self._ask_intro
        self.ask_current_password: Callable[[Callable[[str], bool]], Optional[str]] = self._ask_current_password
        self.ask_new_password: Callable[[str, bool], tuple] = self._ask_new_password
        self.ask_remove_keyring: Callable[[int], bool] = self._ask_remove_keyring
        self.show_recovery: Callable[[str], bool] = lambda code: RecoveryKeyDialog(self.parent, code).ask()
        self.message: Callable[[str, str], None] = self._message
        self.after_step: Optional[Callable[[str], None]] = None  # tests: inject failures

    @property
    def keybox_path(self) -> Path:
        return self.config_dir / "keybox.json"

    def _safe(self, keybox: Optional[Keybox] = None) -> StartupResult:
        return StartupResult(keybox=keybox, unfinished=True)

    def _verify_old(self, text: str) -> bool:
        return legacy_password_ok(self.config_dir, text, self.settings.master_password_hash)

    # ---------------------------------------------------------- new migration
    def run(self) -> StartupResult:
        """No keybox yet: steps 1–7. Never returns ``None``: the app always starts."""

        found = inspect(self.config_dir, self.settings)
        if not self.ask_intro():
            return self._safe()
        keyring = self.keyring_factory()
        legacy_password: Optional[str] = None
        remember_key = False
        if found.has_hash and not found.disabled_flag:
            password = self.ask_current_password(self._verify_old)
            if not password:
                return self._safe()
            legacy_password = password
        else:
            if found.has_hash:  # "don't ask on startup" was on
                if keyring is not None:
                    explain = _("Choose a master password. It is remembered on this computer, so SSH Manager "
                                "still starts without asking; you need it to change security settings.")
                    remember_key = True
                else:
                    explain = _("Choose a master password. You will type it when SSH Manager starts: without a "
                                "system keyring, a key file next to the data would not protect anything.")
            else:
                explain = _("Choose a master password to encrypt your saved passwords and macros.")
            choice, password = self.ask_new_password(explain, not found.legacy_secrets)
            if choice == "off":
                self.settings.passwords_off = True
                self.settings.master_password_disabled = False
                self.settings.save()
                return StartupResult(passwords_off=True)
            if choice != "set" or not password:
                return self._safe()
            if needs_legacy_password(self.config_dir):
                legacy_password = password if self._verify_old(password) else self.ask_current_password(
                    self._verify_old)
                if not legacy_password:
                    return self._safe()
        try:
            write_marker(self.config_dir)  # before the keybox: a crash from here on resumes
            keybox = run_off_thread(lambda: Keybox.create(password, self.keybox_path))
        except (KeyboxError, OSError) as exc:
            LOGGER.warning("migration: the key file could not be created (%s)", type(exc).__name__)
            if not self.keybox_path.exists():
                secure_remove(self.config_dir / MARKER)
            self.message(_("Saved passwords could not be moved"), _("The key file could not be created. "
                                                                    "Nothing was changed."))
            return self._safe()
        password = None
        offer_recovery_key(self.parent, keybox, show=self.show_recovery)
        if remember_key:
            try:
                remember(keyring, keybox)
            except Exception as exc:  # the dialog asks at the next start instead
                LOGGER.warning("migration: the key could not be remembered (%s)", type(exc).__name__)
        return self.finish(keybox, legacy_password, keyring)

    # ---------------------------------------------------------------- resume
    def resume(self, keybox: Keybox, typed: Optional[str] = None) -> StartupResult:
        """The keybox exists (unlocked) but the move did not finish: run it again (it is idempotent)."""

        legacy_password = typed
        if needs_legacy_password(self.config_dir) and not (typed and self._verify_old(typed)):
            legacy_password = self.ask_current_password(self._verify_old)
            if not legacy_password:
                return self._safe(keybox)
        return self.finish(keybox, legacy_password, self.keyring_factory())

    def finish(self, keybox: Keybox, legacy_password: Optional[str], keyring) -> StartupResult:
        migration = Migration(self.config_dir, keybox, keyring=keyring, manager=self.manager,
                              legacy_password=legacy_password, app_version=self.app_version,
                              after_step=self.after_step)
        legacy_password = None
        try:
            report = run_off_thread(migration.run)
        except MigrationError as exc:
            LOGGER.warning("migration: stopped at %s (%s)", exc.code, exc)
            self.message(_("Saved passwords could not be moved"),
                         _("Nothing was lost. Try again from Preferences → Security → Finish migration."))
            return self._safe(keybox)
        remove = bool(report.keyring_keys) and self.ask_remove_keyring(len(report.keyring_keys))
        try:
            run_off_thread(lambda: migration.cleanup(remove_keyring=remove))
        except (MigrationError, OSError) as exc:  # the new stores work; the copies go next time
            LOGGER.warning("migration: the old copies were not removed (%s)", type(exc).__name__)
        self.settings.passwords_off = False
        self.settings.save()  # the obsolete master password keys are no longer written
        return StartupResult(keybox=keybox, report=report)

    # --------------------------------------------------------------- dialogs
    def _ask_intro(self) -> bool:
        dialog = Gtk.MessageDialog(transient_for=self.parent, modal=True, message_type=Gtk.MessageType.INFO,
                                   text=_("Saved passwords are moving to a new format"))
        dialog.format_secondary_text(intro_text())
        dialog.add_button(_("_Not Now"), RESPONSE_LATER)
        dialog.add_button(_("_Continue"), Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        try:
            return dialog.run() == Gtk.ResponseType.OK
        finally:
            dialog.destroy()

    def _ask_current_password(self, verify: Callable[[str], bool]) -> Optional[str]:
        return UnlockDialog(self.parent, _("Unlock SSH Manager"), _("Enter your master password"),
                            _("Your current master password protects the new key file too."), verify,
                            in_thread=True, error_text=_("Incorrect master password. Please try again.")).ask()

    def _ask_new_password(self, explain: str, allow_off: bool) -> tuple:
        dialog = NewSecretDialog(self.parent, _("Set Master Password"), _("Set a master password"), explain,
                                 accept_label=_("_Set Master Password"))
        if allow_off:
            dialog.add_secondary(_("_Don't Save Passwords"), RESPONSE_OFF)
        else:
            dialog.add_secondary(_("_Not Now"), RESPONSE_LATER)
        dialog.show_all()
        try:
            response = int(dialog.run())
            password = dialog.new_password
        finally:
            dialog.destroy()
        if response == RESPONSE_OFF:
            return "off", None
        return ("set", password) if is_accept(response) and password else ("later", None)

    def _ask_remove_keyring(self, count: int) -> bool:
        dialog = Gtk.MessageDialog(transient_for=self.parent, modal=True, message_type=Gtk.MessageType.QUESTION,
                                   text=_("Moved {n} saved passwords. Remove the old copies from the system "
                                          "keyring?").format(n=count))
        dialog.format_secondary_text(_("They are encrypted in SSH Manager's own file now. If you keep them, "
                                       "Preferences → Security can remove them later."))
        dialog.add_button(_("_Keep Them"), Gtk.ResponseType.NO)
        dialog.add_button(_("_Remove"), Gtk.ResponseType.YES)
        dialog.set_default_response(Gtk.ResponseType.YES)
        try:
            return dialog.run() == Gtk.ResponseType.YES
        finally:
            dialog.destroy()

    def _message(self, title: str, detail: str) -> None:
        dialog = Gtk.MessageDialog(transient_for=self.parent, modal=True, message_type=Gtk.MessageType.WARNING,
                                   buttons=Gtk.ButtonsType.OK, text=title)
        dialog.format_secondary_text(detail)
        try:
            dialog.run()
        finally:
            dialog.destroy()


__all__ = ["MigrationFlow", "intro_text"]
