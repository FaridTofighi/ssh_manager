"""What to do at start-up, and the helpers of the unlock modes (no GTK).

:func:`inspect` looks at the config folder (never at secrets) and :func:`decide`
picks one of :class:`Startup`:

* ``FIRST_RUN`` — no key file and nothing to protect: set a master password,
  or start with passwords off;
* ``UNLOCK`` — a key file exists: ask for the master password (or use the
  data key remembered in the OS keyring);
* ``MIGRATE`` / ``RESUME`` — data of an older version is there (a master
  password hash, ``passwords.json`` v1/v2, ``macros.enc`` v1, credentials whose
  passwords may sit in the OS keyring): move it to the keybox;
* ``KEY_MISSING`` — encrypted stores exist but ``keybox.json`` does not: never
  create a new key silently; the user restores the file or starts without them;
* ``PASSWORDS_OFF`` — the user chose "Don't save passwords" (only possible
  while there is no key file; a key file always wins).

There is no setting that turns protection off: the obsolete
``master_password_hash`` / ``master_password_disabled`` only steer the migration.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional

REMEMBER_SERVICE = "ssh-manager"
MAX_DELAY_S = 30.0
FREE_ATTEMPTS = 5


class Startup(str, Enum):
    FIRST_RUN = "first_run"
    UNLOCK = "unlock"
    MIGRATE = "migrate"
    RESUME = "resume"
    KEY_MISSING = "key_missing"
    PASSWORDS_OFF = "passwords_off"


def json_version(path: Path) -> int:
    """``passwords.json`` / ``macros.enc`` format: 0 none, 1 plain / v1, 2, 3 …, -1 unreadable."""

    if not path.exists():
        return 0
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return -1
    if not isinstance(data, dict):
        return -1
    version = data.get("version")
    if isinstance(version, int) and not isinstance(version, bool) and "data" in data:
        return version
    return 1  # the plain {"service": {"key": "secret"}} file of the first versions


@dataclass
class Inspection:
    keybox_exists: bool
    passwords_version: int = 0
    macros_version: int = 0
    has_hash: bool = False
    disabled_flag: bool = False
    credentials: int = 0
    leftovers: List[Path] = field(default_factory=list)  # *.pre-keybox files of an unfinished migration
    passwords_off: bool = False

    @property
    def encrypted_stores(self) -> bool:
        return self.passwords_version >= 3 or self.macros_version >= 2

    @property
    def legacy_secrets(self) -> bool:
        """Secrets of an older version may exist (files, or the OS keyring for known credentials)."""

        return self.passwords_version in (1, 2, -1) or self.macros_version == 1 or self.credentials > 0

    @property
    def legacy(self) -> bool:
        return self.legacy_secrets or self.has_hash or self.disabled_flag


def inspect(config_dir: Path, settings) -> Inspection:
    config_dir = Path(config_dir)
    credentials = 0
    try:
        data = json.loads((config_dir / "credentials.json").read_text(encoding="utf-8"))
        credentials = len(data.get("credentials", [])) if isinstance(data, dict) else 0
    except (OSError, ValueError):
        credentials = 0
    leftovers = sorted(p for p in config_dir.glob("*.pre-keybox") if p.is_file()) if config_dir.exists() else []
    return Inspection(
        keybox_exists=(config_dir / "keybox.json").exists(),
        passwords_version=json_version(config_dir / "passwords.json"),
        macros_version=json_version(config_dir / "macros.enc"),
        has_hash=bool(getattr(settings, "master_password_hash", "")),
        disabled_flag=bool(getattr(settings, "master_password_disabled", False)),
        credentials=credentials,
        leftovers=leftovers,
        passwords_off=bool(getattr(settings, "passwords_off", False)),
    )


def decide(found: Inspection) -> Startup:
    if found.keybox_exists:
        unfinished = found.passwords_version in (1, 2) or found.macros_version == 1 or bool(found.leftovers)
        return Startup.RESUME if unfinished else Startup.UNLOCK
    if found.encrypted_stores:
        return Startup.KEY_MISSING
    if found.legacy:
        return Startup.MIGRATE
    if found.passwords_off:
        return Startup.PASSWORDS_OFF
    return Startup.FIRST_RUN


def unlock_delay(failures: int) -> float:
    """Seconds to wait before the next attempt: none for the first five, then 1, 2, 4 … at most 30."""

    if failures < FREE_ATTEMPTS:
        return 0.0
    return min(MAX_DELAY_S, float(2 ** (failures - FREE_ATTEMPTS)))


def orphan_stores(config_dir: Path) -> List[Path]:
    """Move stores that need a missing key file aside (``<name>.orphaned-<time>``); nothing is deleted."""

    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved: List[Path] = []
    for name in ("passwords.json", "passwords.json.bak", "macros.enc", "macros.enc.bak"):
        path = Path(config_dir) / name
        if path.exists():
            target = path.with_name(f"{name}.orphaned-{stamp}")
            path.rename(target)
            moved.append(target)
    return moved


# -------------------------------------------------------- remember mode
def remember_key(keybox_id: str) -> str:
    return f"keybox:{keybox_id}"


def usable_keyring():
    """The OS keyring backend, or ``None`` when there is none (then "Remember" is not offered)."""

    from .secure_store import _system_keyring

    return _system_keyring()


def remembered_dek(keyring, keybox_id: str) -> Optional[bytes]:
    if keyring is None:
        return None
    try:
        value = keyring.get_password(REMEMBER_SERVICE, remember_key(keybox_id))
        return base64.b64decode(value, validate=True) if value else None
    except Exception:  # a locked or broken keyring: ask instead
        return None


def remember(keyring, keybox) -> None:
    keyring.set_password(REMEMBER_SERVICE, remember_key(keybox.id), base64.b64encode(keybox.dek()).decode("ascii"))


def forget_remembered(keyring, keybox_id: str) -> None:
    if keyring is None:
        return
    try:
        keyring.delete_password(REMEMBER_SERVICE, remember_key(keybox_id))
    except Exception:
        pass


def is_remembered(keyring, keybox_id: str) -> bool:
    return remembered_dek(keyring, keybox_id) is not None


__all__ = ["Startup", "Inspection", "inspect", "decide", "unlock_delay", "orphan_stores", "usable_keyring",
           "remembered_dek", "remember", "forget_remembered", "is_remembered", "json_version"]
