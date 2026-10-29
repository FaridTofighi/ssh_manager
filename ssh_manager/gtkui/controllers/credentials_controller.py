"""Credential bindings, permissions and password persistence for hosts."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from ...auth_flow import NO_PASSWORD, PasswordResolution, SaveContext, source_phrase  # noqa: E402
from ...config import HostEntry  # noqa: E402
from ...credentials import KEEP, Credential, PasswordChoice  # noqa: E402
from ...host_utils import host_config_key, host_connection_params, host_option, with_user  # noqa: E402
from ...profiles import SessionProfile  # noqa: E402
from ...secure_store import FileKeyring, KeyringError, SecureStoreError  # noqa: E402
from ..dialogs.confirm import confirm  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Level  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow


LOGGER = logging.getLogger(__name__)


class CredentialController:
    """Credential bindings, permissions and password persistence for hosts."""

    def __init__(self, win: "SSHManagerWindow") -> None:
        self.win = win

    def permissions_for_host(self, host: HostEntry) -> Dict[str, bool]:
        perms = host.metadata.get("permissions")
        if isinstance(perms, dict):
            return {
                "can_view": bool(perms.get("can_view", True)),
                "can_connect": bool(perms.get("can_connect", True)),
                "can_edit": bool(perms.get("can_edit", True)),
            }
        return {"can_view": True, "can_connect": True, "can_edit": True}

    def host_is_read_only(self, host: HostEntry) -> bool:
        key = self.win.tree.make_host_key(host.parent_group, host.child_group, host.alias)
        perms = self.permissions_for_host(host)
        return key in self.win.tree.read_only_keys or not perms.get("can_edit", True)

    def render_permissions(self, host: HostEntry) -> str:
        perms = self.permissions_for_host(host)
        parts = []
        if not perms.get("can_view", True):
            parts.append("view locked")
        if not perms.get("can_connect", True):
            parts.append("connect locked")
        if not perms.get("can_edit", True):
            parts.append("edit locked")
        return ", ".join(parts)

    def host_key(self, host: HostEntry) -> str:
        """Path key (``parent::child::alias``) for status, history and colors."""

        return host_config_key(host)

    def credential_key(self, host: HostEntry) -> str:
        """Key of the host's credential binding: its stable id (app store), else the path key."""

        return host.id or host_config_key(host)

    @property
    def _ids_are_stable(self) -> bool:
        return bool(getattr(self.win.manager, "allows_display_names", False))

    def bind_many(self, mapping: Dict[str, Optional[str]]) -> bool:
        try:
            self.win._credential_store.bind_many(mapping)
            return True
        except (SecureStoreError, KeyError) as exc:
            self.win._notify(_("Could not save credential bindings. See logs for details."), Level.ERROR)
            LOGGER.warning("credential bind_many failed: %s", exc)
            return False

    def prune_bindings(self) -> None:
        """App store: forget bindings of deleted hosts (credentials are kept)."""

        ids = getattr(self.win.manager, "all_host_ids", None)
        if ids is None:
            return
        try:
            self.win._credential_store.prune_bindings(ids())
        except Exception as exc:  # pragma: no cover - best effort
            LOGGER.warning("credential prune failed: %s", exc)

    def bind_credential(self, host_key: str, cred_id: Optional[str]) -> bool:
        try:
            self.win._credential_store.bind_host(host_key, cred_id)
            return True
        except SecureStoreError as exc:
            self.win._notify(_("Could not save credential binding. See logs for details."), Level.ERROR)
            LOGGER.warning("credential bind failed: %s", exc)
            return False

    def rebind_credential(self, old_key: str, new_key: str) -> bool:
        if self._ids_are_stable:
            return True  # bindings follow the host id across renames and moves
        try:
            self.win._credential_store.rebind_host(old_key, new_key)
            return True
        except SecureStoreError as exc:
            self.win._notify(_("Could not update credential binding. See logs for details."), Level.ERROR)
            LOGGER.warning("credential rebind failed: %s", exc)
            return False

    def remap_credential_prefix(self, old_prefix: str, new_prefix: str) -> bool:
        if self._ids_are_stable:
            return True
        try:
            self.win._credential_store.remap_bindings_prefix(old_prefix, new_prefix)
            return True
        except SecureStoreError as exc:
            self.win._notify(_("Could not update credential bindings. See logs for details."), Level.ERROR)
            LOGGER.warning("credential prefix remap failed: %s", exc)
            return False

    def unbind_credential_prefix(self, prefix: str) -> bool:
        if self._ids_are_stable:
            self.prune_bindings()  # deleted hosts lose their bindings
            return True
        try:
            self.win._credential_store.unbind_prefix(prefix)
            return True
        except SecureStoreError as exc:
            self.win._notify(_("Could not remove credential bindings. See logs for details."), Level.ERROR)
            LOGGER.warning("credential unbind failed: %s", exc)
            return False

    def credential_for_host(self, host: HostEntry) -> Optional[Credential]:
        return self.win._credential_store.credential_for_host(self.credential_key(host))

    def profile_for_host(self, host: HostEntry) -> Optional[SessionProfile]:
        pid = host.metadata.get("default_session_profile_id")
        if not pid:
            return None
        return self.win._profiles.get(pid)

    def host_connection_params(self, host: HostEntry) -> Tuple[str, int, str]:
        return host_connection_params(host)

    def save_detected_password_change(self, host: HostEntry, password: str) -> bool:
        """Persist a password changed inside an active terminal session.

        A host with its own (private) password gets only that updated; other
        hosts keep the previous behaviour (per-host secret, bound credential).
        """

        if self.private_credential_for_host(host) is not None:
            try:
                return self.win._credential_store.update_private(self.credential_key(host), password=password)
            except SecureStoreError as exc:
                LOGGER.warning("host password update failed: %s", type(exc).__name__)
                return False

        resolved = self.host_with_resolved_identity(host)
        hostname, port, user = self.host_connection_params(resolved)
        if not hostname:
            return False

        saved_any = False
        required_sources_ok = True
        try:
            self.win._secure_store.set(hostname, port, user, password)
            saved_any = True
        except SecureStoreError as exc:
            LOGGER.warning("per-host password update failed: %s", exc)

        cred_id = self.win._credential_store.binding_for_host(self.credential_key(host))
        if cred_id and self.win._credential_store.get(cred_id):
            required_sources_ok = False
            try:
                self.win._credential_store.set_password(cred_id, password)
                saved_any = True
                required_sources_ok = True
            except SecureStoreError as exc:
                LOGGER.warning("credential password update failed: %s", exc)

        return saved_any and required_sources_ok

    def host_with_resolved_identity(self, host: HostEntry) -> HostEntry:
        cred = self.credential_for_host(host)
        if not cred:
            return host
        if cred.username:  # a host-specific credential may leave User to the host / ssh
            return with_user(host, cred.username)
        return host

    def credential_password_for_host(self, host: HostEntry) -> Optional[str]:
        cred = self.credential_for_host(host)
        if cred:
            try:
                return self.win._credential_store.get_password(cred.id)
            except SecureStoreError as exc:
                self.win._notify(_("Credential '{name}' is unavailable: {error}").format(name=cred.name, error=exc), Level.WARNING)
        return None

    def resolve_password(self, host: HostEntry) -> PasswordResolution:
        """The password for connecting to ``host`` and its source — read only, never writes a store.

        Order: the bound credential's password → the legacy ``(host, port, user)``
        secret (existing users) → none.
        A host-specific password is the bound (private) credential; its source
        is ``"host"`` instead of ``"credential"``.
        """

        cred = self.credential_for_host(host)
        if cred:
            try:
                password = self.win._credential_store.get_password(cred.id)
            except SecureStoreError as exc:
                LOGGER.warning("credential password unavailable: %s", type(exc).__name__)
                self.win._notify(_("Credential '{name}' is unavailable.").format(name=cred.name), Level.WARNING)
                password = None
            if password:
                return PasswordResolution(password, "host" if cred.private else "credential", cred.id)
        hostname, port, user = self.host_connection_params(self.host_with_resolved_identity(host))
        if hostname:
            try:
                legacy = self.win._secure_store.get(hostname, port, user)
            except SecureStoreError as exc:
                LOGGER.warning("stored password unavailable: %s", type(exc).__name__)
                legacy = None
            if legacy:
                return PasswordResolution(legacy, "legacy", None)
        return NO_PASSWORD

    def resolve_host_credentials(self, host: HostEntry) -> Tuple[HostEntry, PasswordResolution]:
        """``(host with the credential's username, password resolution)``; nothing is written."""

        return self.host_with_resolved_identity(host), self.resolve_password(host)

    def display_username_for_host(self, host: HostEntry) -> str:
        cred = self.credential_for_host(host)
        if cred and cred.username:
            return cred.username
        return host_option(host, "user") or ""

    # ------------------------------------------------ host-specific passwords
    def private_credential_for_host(self, host: HostEntry) -> Optional[Credential]:
        return self.win._credential_store.private_for_host(self.credential_key(host))

    def password_summary(self, host: HostEntry) -> str:
        """``credential ‘X’`` / ``host-specific`` / ``""`` — where the host's password comes from (never the value)."""

        cred = self.credential_for_host(host)
        if not cred:
            return ""
        if cred.private:
            return _("host-specific")
        return _("credential ‘{name}’").format(name=cred.name)

    def password_store_problem(self) -> Optional[str]:
        """Why passwords cannot be saved right now (locked password file, keyring error), or ``None``."""

        backend = self.win._secure_store.backend
        if isinstance(backend, FileKeyring) and backend.locked:
            return _("Saved passwords are locked. Unlock them with the master password, then try again.")
        try:
            self.win._credential_store.get_password("probe")
        except SecureStoreError as exc:
            LOGGER.warning("secret store unavailable: %s", type(exc).__name__)
            return _("The password store is unavailable. Nothing was changed.")
        return None

    def apply_host_credentials(self, host: HostEntry, cred_id: Optional[str],
                               choice: Optional[PasswordChoice] = None) -> bool:
        """Bind ``host`` (already saved) to ``cred_id`` or to its own password, as chosen in the host dialog.

        * Credential's password: bind ``cred_id``; a private credential the host
          had is deleted with its secret (same save).
        * Host-specific: create the private credential (``based_on`` the
          selected shared one, whose username it copies; the shared credential
          is untouched) or update its password / clear it / its username.

        Also the entry point for Phase 3 ("Save this password for this
        host?"): ``PasswordChoice(host_specific=True, password=…)``. On
        :class:`SecureStoreError` nothing is changed and the user is told.
        """

        try:
            self._apply_host_credentials(host, cred_id, choice)
            return True
        except SecureStoreError as exc:
            LOGGER.warning("host password update failed: %s", type(exc).__name__)
            self.win._notify(_("Could not save the password for ‘{alias}’. Nothing was changed.").format(alias=host.alias),
                             Level.ERROR)
            return False

    def _apply_host_credentials(self, host: HostEntry, cred_id: Optional[str],
                                choice: Optional[PasswordChoice]) -> None:
        """:meth:`apply_host_credentials` without the notification (raises :class:`SecureStoreError`)."""

        store = self.win._credential_store
        key = self.credential_key(host)
        if choice is None or not choice.host_specific:
            store.bind_host(key, cred_id)
            return
        shared = store.get(cred_id) if cred_id else None
        if shared is not None and shared.private:
            shared = None
        username = shared.username if shared else (host_option(host, "user") or "")
        if store.private_for_host(key) is None:
            store.create_private(key, username, None if choice.clear else choice.password,
                                 label=host.alias, based_on=shared.id if shared else None)
            return
        store.update_private(
            key,
            password=None if choice.clear else (choice.password or KEEP),
            username=username,
            based_on=shared.id if shared else None,
        )

    # ---------------------------------------- recovery: "Save this password?"
    def describe_password_source(self, resolution: PasswordResolution) -> str:
        """``saved password (credential ‘X’)`` … for the retry dialog (never the password)."""

        name = ""
        if resolution.source == "credential" and resolution.credential_id:
            cred = self.win._credential_store.get(resolution.credential_id)
            name = cred.name if cred else ""
        return source_phrase(resolution.source, name)

    def find_current_host(self, host: HostEntry) -> Optional[HostEntry]:
        """The host as the inventory has it now (by id, else by group path + alias), or ``None`` when gone."""

        for groups in self.win.manager.tree.values():
            for group in groups.values():
                for candidate in group.hosts:
                    if host.id:
                        if candidate.id == host.id:
                            return candidate
                    elif (candidate.parent_group, candidate.child_group, candidate.alias) == (
                            host.parent_group, host.child_group, host.alias):
                        return candidate
        return None

    def _save_blocker(self, host: HostEntry) -> str:
        """Why a password cannot be saved for ``host`` ("" = it can)."""

        if getattr(self.win.manager, "read_only", False):
            return _("The inventory is read-only.")
        if not self.permissions_for_host(host).get("can_edit", True) or self.host_is_read_only(host):
            return _("Editing this host is disabled by its permissions.")
        backend = self.win._secure_store.backend
        policy = (self.win._settings.secrets_policy_with_defaults()
                  if hasattr(self.win._settings, "secrets_policy_with_defaults") else {})
        if (isinstance(backend, FileKeyring) and not backend.encrypted
                and not bool(policy.get("allow_plaintext_host_passwords", True))):
            return _("The secrets policy does not allow saving passwords without a master password.")
        return ""

    def password_save_context(self, host: HostEntry) -> SaveContext:
        """What "Save this password for this host?" would do for ``host`` now."""

        current = self.find_current_host(host) or host
        cred = self.credential_for_host(current)
        blocker = self._save_blocker(current)
        shared_name = ""
        if cred is not None and not cred.private:
            shared_name = cred.name
        elif cred is not None and cred.based_on:
            based = self.win._credential_store.get(cred.based_on)
            shared_name = based.name if based else ""
        return SaveContext(
            can_save=not blocker,
            reason=blocker,
            credential_name=shared_name,
            has_private=bool(cred and cred.private),
        )

    def save_host_password(self, host: HostEntry, password: str) -> Tuple[str, str]:
        """Save ``password`` as ``host``'s own password; ``(status, message)`` with status ``saved|gone|blocked|error``.

        The host is looked up again (it may have been renamed, moved or deleted
        while the dialog was open). The shared credential and other hosts are
        never changed. Nothing is logged but the error type.
        """

        current = self.find_current_host(host)
        if current is None:
            return "gone", _("‘{alias}’ no longer exists; the password was not saved.").format(alias=host.alias)
        blocker = self._save_blocker(current)
        if blocker:
            return "blocked", blocker
        store = self.win._credential_store
        binding = store.binding_for_host(self.credential_key(current))
        shared_id = store.shared_for_copy(self.credential_key(current)) if binding else None
        try:
            self._apply_host_credentials(current, shared_id, PasswordChoice(host_specific=True, password=password))
        except SecureStoreError as exc:
            LOGGER.warning("saving the host password failed: %s", type(exc).__name__)
            problem = self.password_store_problem()
            return "error", problem or _("Could not save the password. The session stays connected.")
        try:  # "Password: host-specific" in the tree tooltip and the details
            self.win.tree.rebuild(refresh=False)
            host_page = getattr(self.win, "host_page", None)
            if host_page is not None:
                host_page.refresh()
        except Exception as exc:  # pragma: no cover - display refresh is best effort
            LOGGER.debug("display refresh after password save failed: %s", type(exc).__name__)
        return "saved", _("Password saved for ‘{alias}’.").format(alias=current.alias)

    def binding_for_copy(self, host: HostEntry) -> Tuple[Optional[str], bool]:
        """``(credential id for a copy of host, whether a host-specific password was left behind)``."""

        store = self.win._credential_store
        key = self.credential_key(host)
        return store.shared_for_copy(key), store.private_for_host(key) is not None

    def forget_password(self) -> None:
        host = self.win.tree.selected_host()
        if not host:
            self.win._notify(_("Select a host to remove stored password."), Level.INFO)
            return
        resolved = self.host_with_resolved_identity(host)
        hostname, port, username = self.host_connection_params(resolved)
        try:
            self.win._secure_store.delete(hostname, port, username)
            binding = self.win._credential_store.binding_for_host(self.credential_key(host))
            if binding:
                self.bind_credential(self.credential_key(host), None)
            self.win._notify(_("Password removed for {alias}. You will be prompted next time.").format(alias=host.alias), Level.SUCCESS)
        except SecureStoreError as exc:
            self.win._notify(_("Could not remove the stored password. See logs for details."), Level.WARNING)
            LOGGER.warning("forget password failed: %s", exc)

    def rekey_password_file(self, master_password: Optional[str]) -> None:
        """Re-encrypt the local password file (if used) under a new master password."""

        backend = self.win._secure_store.backend
        if not isinstance(backend, FileKeyring):
            return
        try:
            backend.rekey(master_password)
        except KeyringError as exc:
            LOGGER.warning("password file rekey failed: %s", exc)
            self.win._notify(_("Could not re-encrypt saved passwords. See logs for details."), Level.WARNING)
            return
        if master_password is None:
            self.win._notify_warning(
                "Saved passwords are now stored unencrypted because the master password is disabled."
            )

    def warn_if_password_file_locked(self) -> None:
        backend = self.win._secure_store.backend
        if isinstance(backend, FileKeyring) and backend.locked:
            self.win._notify_warning(
                _("Saved passwords are encrypted but could not be unlocked; enable the master password to use them.")
            )

    def host_has_plaintext_password(self, host: HostEntry) -> bool:
        for key, value in host.options.items():
            if value and "password" in key.lower():
                return True
        return False

    def confirm_policy_override(self, message: str) -> bool:
        return confirm(
            self.win,
            _("Save plain-text password?"),
            message,
            confirm_label=_("_Save Anyway"),
            destructive=True,
        )

    def check_secrets_policy(self, host: HostEntry) -> bool:
        policy = (
            self.win._settings.secrets_policy_with_defaults()
            if hasattr(self.win._settings, "secrets_policy_with_defaults")
            else {
                "allow_plaintext_host_passwords": True,
                "warn_on_plaintext_host_passwords": True,
            }
        )
        if not self.host_has_plaintext_password(host):
            return True
        allow_plain = bool(policy.get("allow_plaintext_host_passwords", True))
        warn_plain = bool(policy.get("warn_on_plaintext_host_passwords", True))

        if allow_plain:
            if warn_plain:
                self.win._notify_warning(_("This host stores a password in plain text. Consider using a "
                                           "credential instead."))
            return True

        message = _("Plain-text passwords are disallowed by the current secrets policy. "
                    "Use a credential instead or confirm to bypass.")
        if self.confirm_policy_override(message):
            return True
        self.win._notify(_("Host save cancelled by secrets policy."), Level.WARNING)
        return False
