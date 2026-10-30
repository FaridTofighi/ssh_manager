"""SSH keys in the window: the keys found, saved passphrases, generating, importing, which hosts use a key.

Saved passphrases live only in the app's password store (``SecureStore``,
the same place as host passwords, so the master password protects them),
under ``keyfile:<real path>``. Keys added by path (outside ``~/.ssh``) are
listed in ``ssh_keys.json`` next to the settings — paths only, nothing secret.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional

from ... import ssh_keys as K
from ..i18n import _
from ..theme.tokens import Level

if TYPE_CHECKING:  # pragma: no cover
    from ...gtk_app import SSHManagerWindow

LOGGER = logging.getLogger(__name__)
SERVICE = "ssh-manager"
EXTRA_FILE = "ssh_keys.json"


def passphrase_key(path: Path) -> str:
    return f"keyfile:{os.path.realpath(os.path.expanduser(str(path)))}"


def identity_path(host) -> str:
    value = host.option("identityfile") if hasattr(host, "option") else ""
    return os.path.realpath(os.path.expanduser(value)) if value else ""


class SshKeysController:
    def __init__(self, win: "SSHManagerWindow", ssh_dir: Optional[Path] = None) -> None:
        self.win = win
        self.ssh_dir = Path(ssh_dir) if ssh_dir is not None else K.default_ssh_dir()

    # --------------------------------------------------------------- keys
    @property
    def _extra_path(self) -> Path:
        return Path(self.win._settings.config_path).parent / EXTRA_FILE

    def extra_paths(self) -> List[str]:
        try:
            data = json.loads(self._extra_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [p for p in data.get("paths", []) if isinstance(p, str)] if isinstance(data, dict) else []

    def _save_extra(self, paths: List[str]) -> None:
        from ...persistence import write_bytes_private

        write_bytes_private(self._extra_path, json.dumps({"paths": sorted(set(paths))}, indent=1).encode("utf-8"))

    def keys(self) -> List[K.KeyInfo]:
        return K.scan_ssh_dir(self.ssh_dir, [Path(p) for p in self.extra_paths()])

    def hosts(self) -> list:
        from ...inventory.openssh import iter_hosts

        return list(iter_hosts(self.win.manager.tree))

    def usage(self) -> Dict[str, int]:
        """Real path of an identity file → how many inventory hosts use it."""

        counts: Dict[str, int] = {}
        for host in self.hosts():
            path = identity_path(host)
            if path:
                counts[path] = counts.get(path, 0) + 1
        return counts

    def missing_references(self) -> Dict[str, int]:
        """Identity files hosts point to that do not exist (shown with a warning)."""

        return {path: count for path, count in self.usage().items() if not os.path.exists(path)}

    # -------------------------------------------------------- passphrases
    def _store(self):
        return self.win._secure_store

    def saved_passphrase(self, path: Path) -> Optional[str]:
        try:
            return self._store().backend.get_password(SERVICE, passphrase_key(path)) or None
        except Exception as exc:  # a locked or unavailable store: as if none were saved
            LOGGER.info("ssh keys: the passphrase store is unavailable (%s)", type(exc).__name__)
            return None

    def remember_passphrase(self, path: Path, passphrase: str) -> bool:
        if getattr(self._store(), "saving_disabled", False):
            self.win._notify(_("Saving passphrases is turned off (Settings → Security)."), Level.WARNING)
            return False
        try:
            self._store().backend.set_password(SERVICE, passphrase_key(path), passphrase)
            return True
        except Exception as exc:
            LOGGER.warning("ssh keys: the passphrase was not saved (%s)", type(exc).__name__)
            self.win._notify(_("The passphrase could not be saved."), Level.WARNING)
            return False

    def forget_passphrase(self, path: Path) -> None:
        try:
            self._store().backend.delete_password(SERVICE, passphrase_key(path))
        except Exception:
            pass

    def has_saved_passphrase(self, path: Path) -> bool:
        return self.saved_passphrase(path) is not None

    def passphrase_for_host(self, host) -> str:
        """For Paramiko connections: the saved passphrase of the host's identity file ('' if none)."""

        path = identity_path(host)
        return (self.saved_passphrase(Path(path)) or "") if path else ""

    # ------------------------------------------------------------ actions
    def default_comment(self) -> str:
        import getpass
        import socket

        try:
            return f"{getpass.getuser()}@{socket.gethostname()}"
        except Exception:
            return ""

    def suggest(self, base: str = "id_ed25519") -> str:
        return K.suggest_name(self.ssh_dir, base)

    def write_generated(self, name: str, private, *, comment: str, passphrase: str, remember: bool) -> K.KeyInfo:
        info = K.write_private_key(self.ssh_dir / name, private, comment=comment, passphrase=passphrase)
        if passphrase and remember:
            self.remember_passphrase(info.path, passphrase)
        return info

    def import_copy(self, source: Path) -> K.KeyInfo:
        source = Path(source)
        if not K.is_private_key_file(source):
            raise K.KeyError_(_("{name} is not a private key file.").format(name=source.name))
        name = K.suggest_name(self.ssh_dir, source.name)
        K._ensure_dir(self.ssh_dir)
        K._write_new(self.ssh_dir / name, source.read_bytes(), 0o600)
        public = source.with_name(source.name + ".pub")
        if public.exists():
            K._write_new(self.ssh_dir / f"{name}.pub", public.read_bytes(), 0o644)
        return K.read_key_info(self.ssh_dir / name)

    def import_reference(self, source: Path) -> K.KeyInfo:
        source = Path(source).expanduser()
        if not K.is_private_key_file(source):
            raise K.KeyError_(_("{name} is not a private key file.").format(name=source.name))
        self._save_extra(self.extra_paths() + [str(source)])
        return K.read_key_info(source)

    def rename(self, info: K.KeyInfo, new_name: str) -> K.KeyInfo:
        new_name = new_name.strip()
        if not new_name or "/" in new_name or new_name.startswith(".") or new_name.endswith(".pub"):
            raise K.KeyError_(_("Choose a plain file name."))
        target = info.path.with_name(new_name)
        if target.exists() or target.with_name(new_name + ".pub").exists():
            raise K.KeyError_(_("{name} already exists.").format(name=new_name))
        saved = self.saved_passphrase(info.path)
        os.rename(info.path, target)
        public = info.path.with_name(info.path.name + ".pub")
        if public.exists():
            os.rename(public, target.with_name(new_name + ".pub"))
        if saved:
            self.forget_passphrase(info.path)
            self.remember_passphrase(target, saved)
        if str(info.path) in self.extra_paths():
            self._save_extra([p for p in self.extra_paths() if p != str(info.path)] + [str(target)])
        return K.read_key_info(target)

    def delete(self, info: K.KeyInfo) -> None:
        """The private key and its ``.pub`` (hosts that use it keep the option and show a warning)."""

        for path in (info.path, info.path.with_name(info.path.name + ".pub")):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        self.forget_passphrase(info.path)
        if str(info.path) in self.extra_paths():
            self._save_extra([p for p in self.extra_paths() if p != str(info.path)])

    def change_passphrase(self, info: K.KeyInfo, old: str, new: str, *, remember: bool) -> None:
        K.change_passphrase(info.path, old, new)
        if new and remember:
            self.remember_passphrase(info.path, new)
        else:
            self.forget_passphrase(info.path)

    def derive_public(self, info: K.KeyInfo, passphrase: str) -> str:
        line = K.derive_public_line(info.path, passphrase)
        if info.comment:
            line = f"{line} {info.comment}"
        public = info.path.with_name(info.path.name + ".pub")
        if not public.exists():
            K._write_new(public, (line + "\n").encode("ascii"), 0o644)
        return line

    def reveal(self, info: K.KeyInfo) -> bool:
        try:
            from gi.repository import Gio  # type: ignore

            return bool(Gio.AppInfo.launch_default_for_uri(info.path.parent.as_uri(), None))
        except Exception as exc:
            LOGGER.info("ssh keys: no file manager (%s)", type(exc).__name__)
            return False

    def open_dialog(self, *, select: str = "") -> None:
        from ..dialogs.ssh_keys import SshKeysDialog

        dialog = SshKeysDialog(self.win, self, select=select)
        dialog.deploy = lambda info: self.open_deploy(key=info, parent=dialog)
        dialog.ask()

    def open_deploy(self, *, key: Optional[K.KeyInfo] = None, hosts=(), parent=None) -> None:
        from ..dialogs.ssh_keys_deploy import DeployKeyDialog

        keys = [self.win.creds.credential_key(host) for host in hosts]
        DeployKeyDialog(parent or self.win, self.win, key=key, host_keys=keys).ask()

    def deploy_to_selection(self) -> None:
        self.open_deploy(hosts=self.win.tree.selected_hosts())

    def deploy_to_group(self) -> None:
        from ..constants import RowKind

        kind, data = self.win.tree.current_item()
        if kind == RowKind.PARENT.value:
            hosts = self.win.tree.hosts_in_parent_group(data["parent"])
        elif kind in (RowKind.CHILD.value, RowKind.CHILD_FOLDER.value):
            hosts = self.win.tree.hosts_in_child_group(data["parent"], data["child"])
        else:
            hosts = self.win.tree.selected_hosts()
        self.open_deploy(hosts=hosts)

    def which_ssh_add(self) -> Optional[str]:
        return shutil.which("ssh-add")


__all__ = ["SshKeysController", "passphrase_key", "identity_path"]
