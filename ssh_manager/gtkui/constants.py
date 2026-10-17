"""Shared constants for the GTK front-end (row kinds, tree columns, icons)."""

from __future__ import annotations

from enum import Enum, IntEnum


class RowKind(str, Enum):
    """Kind of a row in the session tree (stored in the model as a string)."""

    PARENT = "parent"
    CHILD = "child"
    CHILD_FOLDER = "child-folder"
    HOST = "host"

    @classmethod
    def coerce(cls, value: object) -> "RowKind | None":
        raw = value.value if isinstance(value, Enum) else value
        try:
            return cls(str(raw))
        except ValueError:
            return None

    @property
    def is_group(self) -> bool:
        return self is not RowKind.HOST


GROUP_KINDS = {RowKind.PARENT.value, RowKind.CHILD.value, RowKind.CHILD_FOLDER.value}


class TreeColumn(IntEnum):
    """Column indexes of the session-tree ``Gtk.TreeStore``."""

    ICON = 0
    TEXT = 1
    KIND = 2
    PARENT = 3
    CHILD = 4
    ALIAS = 5
    HOST = 6
    SEARCH = 7
    COLOR = 8
    STATUS = 9
    COUNT = 10
    TOOLTIP = 11


class Icons:
    """Themed symbolic icon names used across the UI."""

    GROUP = "folder-symbolic"
    SUBGROUP = "folder-open-symbolic"
    FOLDER = "folder-symbolic"
    HOST = "computer-symbolic"
    SERVER = "network-server-symbolic"
    TERMINAL = "utilities-terminal-symbolic"
    APP_MENU = "open-menu-symbolic"
    NEW = "list-add-symbolic"
    DROPDOWN = "pan-down-symbolic"
    SEARCH = "system-search-symbolic"
    FILTER = "view-list-symbolic"
    SETTINGS = "preferences-system-symbolic"
    SIDEBAR = "sidebar-show-symbolic"
    MAXIMIZE = "view-fullscreen-symbolic"
    RESTORE = "view-restore-symbolic"
    CREDENTIALS = "dialog-password-symbolic"
    VAULT = "channel-secure-symbolic"
    CONFIG_FILE = "text-x-generic-symbolic"
    SHARED = "folder-remote-symbolic"
    GIT_UPDATE = "software-update-available-symbolic"
    SHORTCUTS = "preferences-desktop-keyboard-shortcuts-symbolic"
    ABOUT = "help-about-symbolic"
    CONNECT = "media-playback-start-symbolic"
    RECONNECT = "view-refresh-symbolic"
    EDIT = "document-edit-symbolic"
    DUPLICATE = "edit-copy-symbolic"
    COPY = "edit-copy-symbolic"
    MOVE = "document-send-symbolic"
    COLOR = "preferences-color-symbolic"
    CLEAR = "edit-clear-symbolic"
    DELETE = "edit-delete-symbolic"
    FORGET = "dialog-password-symbolic"
    MORE = "view-more-symbolic"
    CLOSE = "window-close-symbolic"
    SPLIT = "view-dual-symbolic"
    SAVE = "document-save-symbolic"
    PLAIN_TEXT = "edit-paste-symbolic"
    TILE = "view-grid-symbolic"
    BROADCAST = "network-transmit-receive-symbolic"
    STATS = "drive-harddisk-symbolic"
    CPU = "computer-symbolic"
    MEMORY = "media-flash-symbolic"
    DISK = "drive-harddisk-symbolic"
    INTERRUPT = "process-stop-symbolic"
    DETAILS = "view-paged-symbolic"
    OPEN_FOLDER = "document-open-symbolic"
    TAG = "bookmark-new-symbolic"
