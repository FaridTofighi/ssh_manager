"""An in-memory RemoteFileBackend for GTK tests (no sockets, synthetic names)."""

from __future__ import annotations

import io
import posixpath
import threading
import time
from typing import Dict, List, Optional

from ophion.transfer.backend import (
    F_CHMOD, NEVER_CANCELLED, AlreadyExists, CancelToken, EntryKind, NotFound, RemoteEntry, RemoteFileBackend,
    ServerInfo,
)


class _Writer(io.BytesIO):
    def __init__(self, backend: "FakeBackend", path: str, prefix: bytes = b"") -> None:
        super().__init__()
        self.write(prefix)
        self._backend = backend
        self._path = path

    def close(self) -> None:
        if not self.closed:
            self._backend.files[self._path] = self.getvalue()
        super().close()


class FakeBackend(RemoteFileBackend):
    protocol = "sftp"

    def __init__(self, files: Optional[Dict[str, bytes]] = None, dirs: Optional[List[str]] = None, *,
                 home: str = "/home/alice") -> None:
        self.files: Dict[str, bytes] = dict(files or {})
        self.dirs = {"/", home, *(dirs or [])}
        for path in list(self.files) + list(self.dirs):
            parent = posixpath.dirname(path)
            while parent and parent not in self.dirs:
                self.dirs.add(parent)
                parent = posixpath.dirname(parent)
        self.links: Dict[str, str] = {}
        self._home = home
        self.connect_errors: List[BaseException] = []
        self.connected = False
        self.calls: List[str] = []
        self.block: Dict[str, threading.Event] = {}  # list(path) waits for this event
        self.list_delay = 0.0
        self.chmods: List[tuple] = []

    def connect(self) -> ServerInfo:
        self.calls.append("connect")
        if self.connect_errors:
            raise self.connect_errors.pop(0)
        self.connected = True
        return ServerInfo(protocol=self.protocol, banner="fake", home=self._home, features={F_CHMOD})

    def close(self) -> None:
        self.calls.append("close")
        self.connected = False

    def is_alive(self) -> bool:
        return self.connected

    def keepalive(self) -> None:
        self.calls.append("keepalive")

    def home(self) -> str:
        return self._home

    def pwd(self) -> str:
        return self._home

    def features(self):
        return {F_CHMOD}

    def server_info(self) -> ServerInfo:
        return ServerInfo(protocol=self.protocol)

    def _entry(self, path: str) -> RemoteEntry:
        name = posixpath.basename(path) or "/"
        if path in self.links:
            return RemoteEntry(name, path, EntryKind.SYMLINK, link_target=self.links[path])
        if path in self.dirs:
            return RemoteEntry(name, path, EntryKind.DIR, mtime=1_700_000_000.0, mode=0o755, owner="alice")
        if path in self.files:
            return RemoteEntry(name, path, EntryKind.FILE, size=len(self.files[path]), mtime=1_700_000_000.0,
                               mode=0o644, owner="alice", group="staff")
        raise NotFound(f"{path} not found")

    def list(self, path: str, cancel: CancelToken = NEVER_CANCELLED):
        self.calls.append(f"list {path}")
        event = self.block.get(path)
        if event is not None:
            event.wait(5)
        if self.list_delay:
            time.sleep(self.list_delay)
        if path not in self.dirs:
            raise NotFound(f"{path} not found")
        children = sorted({p for p in list(self.dirs) + list(self.files) + list(self.links)
                           if p != path and posixpath.dirname(p) == path})
        for child in children:
            cancel.raise_if_cancelled()
            yield self._entry(child)

    def stat(self, path: str) -> RemoteEntry:
        return self._entry(path)

    def is_dir(self, path: str) -> bool:
        target = self.links.get(path, path)
        return target in self.dirs

    def mkdir(self, path: str, parents: bool = False) -> None:
        if path in self.dirs or path in self.files:
            raise AlreadyExists()
        self.dirs.add(path)

    def rename(self, old: str, new: str) -> None:
        self.calls.append(f"rename {old} {new}")
        if old in self.files:
            self.files[new] = self.files.pop(old)
        elif old in self.dirs:
            for item in sorted(p for p in self.dirs if p == old or p.startswith(old + "/")):
                self.dirs.discard(item)
                self.dirs.add(new + item[len(old):])
            for item in [p for p in self.files if p.startswith(old + "/")]:
                self.files[new + item[len(old):]] = self.files.pop(item)
        else:
            raise NotFound()

    def delete_file(self, path: str) -> None:
        self.calls.append(f"delete {path}")
        if path in self.links:
            del self.links[path]
            return
        if path not in self.files:
            raise NotFound()
        del self.files[path]

    def delete_dir(self, path: str, recursive: bool = False, cancel: CancelToken = NEVER_CANCELLED) -> None:
        self.calls.append(f"rmdir {path}")
        for item in [p for p in self.files if p.startswith(path + "/")]:
            del self.files[item]
        for item in [p for p in self.dirs if p == path or p.startswith(path + "/")]:
            self.dirs.discard(item)

    def chmod(self, path: str, mode: int) -> None:
        self.chmods.append((path, mode))

    def open_read(self, path: str, offset: int = 0, cancel: CancelToken = NEVER_CANCELLED, **_kw):
        if path not in self.files:
            raise NotFound()
        return io.BytesIO(self.files[path][offset:])

    def open_write(self, path: str, offset: int = 0, append: bool = False,
                   cancel: CancelToken = NEVER_CANCELLED, **_kw):
        prefix = self.files.get(path, b"")[:offset] if offset or append else b""
        if append:
            prefix = self.files.get(path, b"")
        return _Writer(self, path, prefix)
