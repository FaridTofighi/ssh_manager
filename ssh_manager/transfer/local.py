"""The local file system behind the same interface as the remote backends (no GTK).

The local pane and the transfer engine use it like a remote backend, so both
sides of a transfer are handled by one code path. Symlinks are listed as
symlinks and never followed when deleting.
"""

from __future__ import annotations

import errno
import os
import shutil
import stat as statmod
from pathlib import Path
from typing import BinaryIO, Iterator, Set

from .backend import (
    F_CHMOD, F_MDTM, F_OWNER, F_REST, F_SIZE, F_SYMLINK, NEVER_CANCELLED, AlreadyExists, BackendError, CancelToken,
    DiskFull, EntryKind, NotFound, PermissionDenied, RemoteEntry, RemoteFileBackend, ServerInfo,
)


def _owner(uid: int) -> str:
    try:
        import pwd

        return pwd.getpwuid(uid).pw_name
    except (ImportError, KeyError):
        return str(uid)


def _group(gid: int) -> str:
    try:
        import grp

        return grp.getgrgid(gid).gr_name
    except (ImportError, KeyError):
        return str(gid)


class LocalBackend(RemoteFileBackend):
    protocol = "local"

    def __init__(self, home: str = "") -> None:
        self._home = home or str(Path.home())

    def connect(self) -> ServerInfo:
        return ServerInfo(protocol="local", home=self._home)

    def close(self) -> None:
        pass

    def is_alive(self) -> bool:
        return True

    def keepalive(self) -> None:
        pass

    def home(self) -> str:
        return self._home

    def pwd(self) -> str:
        return os.getcwd()

    def features(self) -> Set[str]:
        return {F_REST, F_CHMOD, F_SYMLINK, F_SIZE, F_MDTM, F_OWNER}

    def server_info(self) -> ServerInfo:
        return ServerInfo(protocol="local", home=self._home)

    def _entry(self, path: str, name: str) -> RemoteEntry:
        info = os.lstat(path)
        mode = info.st_mode
        link_target = ""
        if statmod.S_ISLNK(mode):
            kind = EntryKind.SYMLINK
            try:
                link_target = os.readlink(path)
            except OSError:
                link_target = ""
        elif statmod.S_ISDIR(mode):
            kind = EntryKind.DIR
        elif statmod.S_ISREG(mode):
            kind = EntryKind.FILE
        else:
            kind = EntryKind.OTHER
        return RemoteEntry(name, path, kind, size=None if kind is EntryKind.DIR else info.st_size,
                           mtime=info.st_mtime, mode=statmod.S_IMODE(mode), perms=statmod.filemode(mode)[1:],
                           owner=_owner(info.st_uid), group=_group(info.st_gid), link_target=link_target)

    def list(self, path: str, cancel: CancelToken = NEVER_CANCELLED) -> Iterator[RemoteEntry]:
        try:
            with os.scandir(path) as iterator:
                for item in iterator:
                    cancel.raise_if_cancelled()
                    try:
                        yield self._entry(item.path, item.name)
                    except FileNotFoundError:
                        continue  # removed while listing
        except OSError as exc:
            raise _map(exc)

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def stat(self, path: str) -> RemoteEntry:
        try:
            return self._entry(path, os.path.basename(path.rstrip(os.sep)) or os.sep)
        except OSError as exc:
            raise _map(exc)

    def mkdir(self, path: str, parents: bool = False) -> None:
        try:
            if parents:
                os.makedirs(path, exist_ok=True)
            else:
                os.mkdir(path)
        except OSError as exc:
            raise _map(exc)

    def rename(self, old: str, new: str) -> None:
        try:
            os.replace(old, new)
        except OSError as exc:
            raise _map(exc)

    def delete_file(self, path: str) -> None:
        try:
            os.unlink(path)
        except OSError as exc:
            raise _map(exc)

    def delete_dir(self, path: str, recursive: bool = False, cancel: CancelToken = NEVER_CANCELLED) -> None:
        try:
            if os.path.islink(path):
                os.unlink(path)  # never follow a link into another tree
            elif recursive:
                shutil.rmtree(path)
            else:
                os.rmdir(path)
        except OSError as exc:
            raise _map(exc)

    def chmod(self, path: str, mode: int) -> None:
        try:
            os.chmod(path, mode & 0o7777)
        except OSError as exc:
            raise _map(exc)

    def set_mtime(self, path: str, mtime: float) -> None:
        try:
            os.utime(path, (mtime, mtime))
        except OSError as exc:
            raise _map(exc)

    def open_read(self, path: str, offset: int = 0, cancel: CancelToken = NEVER_CANCELLED, **_kw) -> BinaryIO:
        try:
            handle = open(path, "rb")
            if offset:
                handle.seek(offset)
            return handle
        except OSError as exc:
            raise _map(exc)

    def open_write(self, path: str, offset: int = 0, append: bool = False,
                   cancel: CancelToken = NEVER_CANCELLED, **_kw) -> BinaryIO:
        try:
            if append:
                return open(path, "ab")
            if offset:
                handle = open(path, "r+b")
                handle.seek(offset)
                handle.truncate()
                return handle
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            return os.fdopen(fd, "wb")
        except OSError as exc:
            raise _map(exc)


def _map(exc: OSError) -> BackendError:
    technical = f"{type(exc).__name__}: {exc}"
    if exc.errno == errno.ENOENT:
        return NotFound(technical=technical)
    if exc.errno in (errno.EACCES, errno.EPERM):
        return PermissionDenied(technical=technical)
    if exc.errno == errno.EEXIST:
        return AlreadyExists(technical=technical)
    if exc.errno == errno.ENOSPC:
        return DiskFull("There is not enough space on this computer.", technical)
    if exc.errno == errno.ENOTEMPTY:
        return BackendError("The folder is not empty.", technical)
    return BackendError(f"{exc.strerror or exc}", technical)
