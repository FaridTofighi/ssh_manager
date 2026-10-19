"""Credential bindings, vault entries, permissions and password persistence for hosts."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Dict, Optional, Tuple

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")

from ...config import HostEntry  # noqa: E402
from ...credentials import Credential  # noqa: E402
from ...host_utils import host_config_key, host_connection_params, host_option, with_user  # noqa: E402
from ...profiles import SessionProfile  # noqa: E402
from ...secure_store import FileKeyring, KeyringError, SecureStoreError  # noqa: E402
from ...vault import VaultEntry, VaultError  # noqa: E402
from ..dialogs.confirm import confirm  # noqa: E402
from ..i18n import _  # noqa: E402
from ..theme.tokens import Level  # noqa: E402

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow


LOGGER = logging.getLogger(__name__)


class CredentialController:
    """Credential bindings, vault entries, permissions and password persistence for hosts."""

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

    def vault_entry_for_host(self, host: HostEntry) -> Optional[VaultEntry]:
        vault_id = host.metadata.get("vault_id")
        if not vault_id or not getattr(self.win, "_vault", None) or not self.win._vault.unlocked:
            return None
        return self.win._vault.get_entry(vault_id)

    def profile_for_host(self, host: HostEntry) -> Optional[SessionProfile]:
        pid = host.metadata.get("default_session_profile_id")
        if not pid:
            return None
        return self.win._profiles.get(pid)

    def host_connection_params(self, host: HostEntry) -> Tuple[str, int, str]:
        return host_connection_params(host)

    def seed_password_for_host(self, host: HostEntry, password: str) -> None:
        hostname, port, user = self.host_connection_params(host)
        if not hostname:
            return
        try:
            self.win._secure_store.set(hostname, port, user, password)
        except SecureStoreError:
            pass

    def save_detected_password_change(self, host: HostEntry, password: str) -> bool:
        """Persist a password changed inside an active terminal session."""

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

        vault_entry = self.vault_entry_for_host(host)
        if vault_entry:
            required_sources_ok = False
            try:
                self.win._vault.update_entry(
                    VaultEntry(
                        vault_entry.id,
                        vault_entry.label,
                        vault_entry.username,
                        password,
                    )
                )
                saved_any = True
                required_sources_ok = True
            except VaultError as exc:
                LOGGER.warning("vault password update failed: %s", exc)

        return saved_any and required_sources_ok

    def host_with_resolved_identity(self, host: HostEntry) -> HostEntry:
        cred = self.credential_for_host(host)
        vault_entry = self.vault_entry_for_host(host)
        if not cred and not vault_entry:
            return host

        if vault_entry:
            return with_user(host, vault_entry.username)
        if cred:
            return with_user(host, cred.username)
        return host

    def credential_password_for_host(self, host: HostEntry) -> Optional[str]:
        cred = self.credential_for_host(host)
        vault_entry = self.vault_entry_for_host(host)
        if vault_entry:
            return vault_entry.password
        if cred:
            try:
                return self.win._credential_store.get_password(cred.id)
            except SecureStoreError as exc:
                self.win._notify(_("Credential '{name}' is unavailable: {error}").format(name=cred.name, error=exc), Level.WARNING)
        return None

    def resolve_host_credentials(self, host: HostEntry) -> HostEntry:
        resolved = self.host_with_resolved_identity(host)
        password = self.credential_password_for_host(host)
        if password:
            self.seed_password_for_host(resolved, password)
        return resolved

    def display_username_for_host(self, host: HostEntry) -> str:
        cred = self.credential_for_host(host)
        if cred:
            return cred.username
        vault_entry = self.vault_entry_for_host(host)
        if vault_entry:
            return vault_entry.username
        return host_option(host, "user") or ""

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
                "prefer_vault_for_passwords": True,
            }
        )
        if not self.host_has_plaintext_password(host):
            return True
        if host.metadata.get("vault_id"):
            return True
        allow_plain = bool(policy.get("allow_plaintext_host_passwords", True))
        warn_plain = bool(policy.get("warn_on_plaintext_host_passwords", True))
        prefer_vault = bool(policy.get("prefer_vault_for_passwords", True))

        if allow_plain:
            if warn_plain:
                message = (
                    "This host stores a password in plain text. Consider using the credential vault instead."
                    if prefer_vault
                    else "This host stores a password in plain text."
                )
                self.win._notify_warning(message)
            return True

        message = (
            "Plain-text passwords are disallowed by the current secrets policy. "
            "Use the credential vault instead or confirm to bypass."
        )
        if self.confirm_policy_override(message):
            return True
        self.win._notify(_("Host save cancelled by secrets policy."), Level.WARNING)
        return False
