"""Preferences → Security: the master password, how SSH Manager unlocks, the recovery key, leftovers.

Every action applies at once (it changes ``keybox.json``, the OS keyring or the stores, not
``settings.json``); the page asks :meth:`SecurityActions.state` again afterwards. Tests replace
the ``ask_*`` / ``confirm_*`` callables and ``keyring_factory``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from ...keybox import Keybox, KeyboxError
from ...migration import keyring_copies, remove_keyring_copies, secure_remove
from ...secure_store import EncryptedFileKeyring, DisabledSecureStore
from ...unlock import Startup, decide, forget_remembered, inspect, is_remembered, remember, usable_keyring
from ..dialogs.confirm import confirm
from ..i18n import _, ngettext
from ..theme.tokens import Level

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow

LOGGER = logging.getLogger(__name__)


@dataclass
class SecurityState:
    has_keybox: bool = False
    passwords_off: bool = False
    migration_pending: bool = False  # safe mode, or a move that has not finished its clean-up
    keyring_available: bool = False
    remembered: bool = False
    has_recovery: bool = False
    saved: int = 0
    store_error: str = ""
    keyring_leftovers: bool = False


class SecurityActions:
    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win
        self.keyring_factory: Callable[[], object] = usable_keyring
        self.ask_password: Callable[[str], Optional[str]] = self._ask_password
        self.confirm: Callable[[str, str, str], bool] = self._confirm
        self.show_recovery: Optional[Callable[[str], bool]] = None  # None: the dialog

    @property
    def config_dir(self) -> Path:
        return Path(self.win._settings.config_path).parent

    @property
    def keybox(self) -> Optional[Keybox]:
        return getattr(self.win, "keybox", None)

    # ----------------------------------------------------------------- state
    def state(self) -> SecurityState:
        keybox = self.keybox
        keyring = self.keyring_factory()
        state = SecurityState(keyring_available=keyring is not None)
        backend = self.win._secure_store.backend
        state.passwords_off = getattr(self.win._secure_store, "reason", "") == "off"
        state.migration_pending = bool(getattr(self.win, "migration_pending", False)) or (
            decide(inspect(self.config_dir, self.win._settings)) is Startup.RESUME)
        if keybox is not None and not getattr(self.win, "migration_pending", False):
            state.has_keybox = True
            state.has_recovery = keybox.has_recovery
            state.remembered = is_remembered(keyring, keybox.id)
            migrated = keybox.migrated or {}
            counts = migrated.get("counts") or {}
            state.keyring_leftovers = bool(counts.get("from_keyring")) and not migrated.get("keyring_removed")
        if isinstance(backend, EncryptedFileKeyring):
            state.saved = backend.count()
            state.store_error = backend.error
        return state

    # ---------------------------------------------------------------- actions
    def change_password(self) -> None:
        self.win.app_dialogs.change_master_password()

    def _verify(self, text: str) -> bool:
        try:
            return Keybox.load(self.keybox.path).unlock(text)
        except KeyboxError:
            return False

    def set_remember(self, on: bool) -> bool:
        """"Remember on this computer": the data key in the OS keyring (after the master password); off removes it."""

        keybox, keyring = self.keybox, self.keyring_factory()
        if keybox is None or keyring is None:
            return False
        if not on:
            forget_remembered(keyring, keybox.id)
            return True
        if self.ask_password(_("Confirm with your master password to remember it on this computer.")) is None:
            return False
        try:
            remember(keyring, keybox)
        except Exception as exc:
            LOGGER.warning("security: the key could not be stored in the system keyring (%s)", type(exc).__name__)
            self.win._notify_error(_("The system keyring did not accept the key. Ophion keeps asking for the "
                                     "master password."))
            return False
        return True

    def recovery_key(self) -> bool:
        from ..unlock_flow import offer_recovery_key

        keybox = self.keybox
        if keybox is None:
            return False
        if keybox.has_recovery and not self.confirm(
                _("Create a new recovery key?"),
                _("The current recovery key stops working."), _("_Create New Key")):
            return False
        try:
            confirmed = offer_recovery_key(self.win, keybox, show=self.show_recovery)
        except KeyboxError as exc:
            LOGGER.warning("security: recovery key not saved (%s)", exc.code)
            self.win._notify_error(_("The recovery key could not be saved. Nothing was changed."))
            return False
        if not confirmed:
            self.win._notify(_("No recovery key is set up."), Level.WARNING)
        return confirmed

    def set_passwords_off(self, off: bool) -> bool:
        return self._turn_saving_off() if off else self._turn_saving_on()

    def _turn_saving_on(self) -> bool:
        from ..unlock_flow import set_master_password

        keybox = set_master_password(self.win, self.win._settings, self.config_dir, show_recovery=self.show_recovery)
        if keybox is None:
            return False
        self.win.adopt_keybox(keybox)
        self.win._settings.passwords_off = False
        macros = getattr(self.win, "macros", None)
        if macros is not None:
            macros.start()
        return True

    def _turn_saving_off(self) -> bool:
        keybox = self.keybox
        if keybox is None:
            return False
        macros = getattr(self.win, "macros", None)
        if macros is not None and not macros.allow_turning_saving_off():
            return False
        count = self.state().saved
        if count and not self.confirm(
                ngettext("Delete {n} saved password?", "Delete {n} saved passwords?", count).format(n=count),
                _("Ophion stops saving passwords and asks for them every time. The saved ones are deleted "
                  "and cannot be brought back."),
                ngettext("_Delete {n} Password", "_Delete {n} Passwords", count).format(n=count)):
            return False
        forget_remembered(self.keyring_factory(), keybox.id)
        for name in ("passwords.json", "passwords.json.bak", "keybox.json", "keybox.json.bak"):
            secure_remove(self.config_dir / name)
        keybox.forget()
        self.win.keybox = None
        self.win._secure_store = DisabledSecureStore()
        self.win._credential_store._secure_store = self.win._secure_store
        self.win._settings.passwords_off = True
        self.win._settings.save()  # no key file now: "Don't save passwords" is remembered
        if macros is not None:
            macros.start()
        return True

    def remove_keyring_copies(self) -> int:
        keybox, keyring = self.keybox, self.keyring_factory()
        backend = self.win._secure_store.backend
        if keybox is None or keyring is None or not isinstance(backend, EncryptedFileKeyring):
            return 0
        moved = backend.items()
        keys = [k for k in keyring_copies(keyring, self.config_dir, self.win.manager) if ("ssh-manager", k) in moved]
        removed = remove_keyring_copies(keyring, keys)
        try:
            keybox.record_migration(dict(keybox.migrated or {}, keyring_removed=removed))
        except KeyboxError:
            pass
        self.win._notify(ngettext("Removed {n} old copy from the system keyring.",
                                  "Removed {n} old copies from the system keyring.", removed).format(n=removed),
                         Level.SUCCESS)
        return removed

    def finish_migration(self) -> bool:
        return self.win.finish_migration()

    # --------------------------------------------------------------- dialogs
    def _ask_password(self, description: str) -> Optional[str]:
        from ..dialogs.secrets import UnlockDialog

        return UnlockDialog(self.win, _("Master Password"), _("Enter your master password"), description,
                            self._verify, in_thread=True,
                            error_text=_("Incorrect master password. Please try again.")).ask()

    def _confirm(self, title: str, detail: str, label: str) -> bool:
        return confirm(self.win, title, detail, confirm_label=label, destructive=True)


__all__ = ["SecurityActions", "SecurityState"]
