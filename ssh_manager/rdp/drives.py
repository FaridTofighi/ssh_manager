"""Which local folders an RDP session may share, checked again right before every launch (pure Python).

A shared folder is resolved to its real path (``~`` expanded, symbolic links
followed) and must exist and be a folder. Some locations are **sensitive**:
the file system root, ``/etc``, ``~/.ssh``, ``~/.gnupg``, the home folder
itself and SSH Manager's own configuration and data folders — and anything
*containing* one of them, since sharing ``~`` exposes ``~/.ssh`` too. Those
are shared only when the user confirmed a red warning for exactly that
folder (``SharedFolder.allow_sensitive``); a symbolic link that points into
a sensitive place counts as that place.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional, Sequence

SYSTEM_SENSITIVE = ("/", "/etc", "/root", "/boot", "/proc", "/sys", "/dev", "/var/lib", "/usr")
HOME_SENSITIVE = (".ssh", ".gnupg", ".password-store", ".local/share/keyrings", ".aws", ".kube", ".docker",
                  ".config/ssh-manager", ".local/share/ssh-manager")


@dataclass(frozen=True)
class FolderCheck:
    path: str  # the resolved (real) path ("" when unusable)
    problem: str = ""  # why it cannot be shared ("" = fine)
    sensitive: str = ""  # which sensitive place it is or contains ("" = none)

    @property
    def ok(self) -> bool:
        return not self.problem


def sensitive_places(home: Optional[str] = None, extra: Iterable[str] = ()) -> List[str]:
    home_dir = os.path.realpath(home or os.path.expanduser("~"))
    places = [os.path.realpath(p) for p in SYSTEM_SENSITIVE]
    places.append(home_dir)
    places += [os.path.realpath(os.path.join(home_dir, rel)) for rel in HOME_SENSITIVE]
    for env, rel in (("XDG_CONFIG_HOME", "ssh-manager"), ("XDG_DATA_HOME", "ssh-manager")):
        base = os.environ.get(env)
        if base:
            places.append(os.path.realpath(os.path.join(base, rel)))
    places += [os.path.realpath(os.path.expanduser(p)) for p in extra]
    return sorted(set(places), key=len)


def _relation(path: str, place: str) -> str:
    """``"is"``, ``"inside"`` (path is within place), ``"contains"`` (place is within path) or ""."""

    if path == place:
        return "is"
    if place != "/" and path.startswith(place.rstrip("/") + "/"):
        return "inside"
    if path == "/" or place.startswith(path.rstrip("/") + "/"):
        return "contains"
    return ""


def check_folder(path: str, *, allow_sensitive: bool = False, home: Optional[str] = None,
                 extra_sensitive: Iterable[str] = ()) -> FolderCheck:
    """Resolve and check one shared folder."""

    text = (path or "").strip()
    if not text or "\x00" in text or "\n" in text:
        return FolderCheck("", "The folder is empty or contains a line break.")
    expanded = os.path.expanduser(text)
    if not os.path.isabs(expanded):
        return FolderCheck("", "Use an absolute path for a shared folder.")
    real = os.path.realpath(expanded)
    if not os.path.isdir(real):
        return FolderCheck("", "The folder does not exist (or is not a folder).")
    if "," in real:
        return FolderCheck("", "FreeRDP cannot share a folder whose path contains a comma.")
    home_dir = os.path.realpath(home or os.path.expanduser("~"))
    for place in sensitive_places(home_dir, extra_sensitive):
        relation = _relation(real, place)
        if relation == "inside" and place in (home_dir, "/"):
            continue  # an ordinary folder inside the home folder (or anywhere): fine
        if relation == "inside" and place == "/usr":
            continue  # /usr/share/… is harmless to read
        if relation:
            label = "~" + place[len(home_dir):] if place.startswith(home_dir) else place
            if allow_sensitive:
                return FolderCheck(real, "", label)
            what = {"is": "is", "inside": "is inside", "contains": "contains"}[relation]
            return FolderCheck(real, f"The folder {what} {label}, which holds private or system files; "
                                     "confirm sharing it in the host dialog first.", label)
    return FolderCheck(real)


def check_all(folders: Sequence, home: Optional[str] = None) -> List[FolderCheck]:
    return [check_folder(f.path, allow_sensitive=getattr(f, "allow_sensitive", False), home=home) for f in folders]


def home_drive_check(allow: bool, home: Optional[str] = None) -> FolderCheck:
    """``+home-drive`` shares the whole home folder (with ~/.ssh): allowed only after a confirmation."""

    home_dir = os.path.realpath(home or str(Path.home()))
    if not allow:
        return FolderCheck(home_dir, "Sharing the whole home folder needs a confirmation in the host dialog.", "~")
    return FolderCheck(home_dir, "", "~")


__all__ = ["check_folder", "check_all", "home_drive_check", "sensitive_places", "FolderCheck"]
