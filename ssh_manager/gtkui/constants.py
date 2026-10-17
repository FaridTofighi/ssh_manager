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
    """Icon names. ``sshm-*`` names come from the bundled outline set
    (``gtkui/assets/icons``); others fall back to the system icon theme."""

    GROUP = "sshm-folder"
    SUBGROUP = "sshm-folder"
    FOLDER = "sshm-folder"
    HOST = "sshm-monitor"
    SERVER = "sshm-server"
    TERMINAL = "sshm-terminal"
    APP_MENU = "sshm-more"
    NEW = "sshm-plus"
    DROPDOWN = "sshm-chevron-down"
    CHEVRON_DOWN = "sshm-chevron-down"
    CHEVRON_RIGHT = "sshm-chevron-right"
    CHEVRON_LEFT = "sshm-chevron-left"
    SEARCH = "sshm-search"
    FILTER = "sshm-filter"
    SETTINGS = "sshm-settings"
    SIDEBAR = "sshm-sidebar"
    MAXIMIZE = "sshm-maximize"
    RESTORE = "sshm-restore"
    CREDENTIALS = "sshm-key"
    KEY = "sshm-key"
    VAULT = "sshm-lock"
    CONFIG_FILE = "sshm-clipboard"
    SHARED = "sshm-globe"
    GIT_UPDATE = "sshm-download"
    SHORTCUTS = "sshm-help"
    HELP = "sshm-help"
    ABOUT = "sshm-info"
    ACTIVITY = "sshm-activity"
    USER = "sshm-user"
    QUIT = "sshm-logout"
    CONNECT = "sshm-terminal"
    RECONNECT = "sshm-refresh"
    EDIT = "sshm-edit"
    DUPLICATE = "sshm-copy"
    COPY = "sshm-copy"
    MOVE = "sshm-move"
    COLOR = "sshm-palette"
    CLEAR = "sshm-close"
    DELETE = "sshm-trash"
    FORGET = "sshm-key"
    MORE = "sshm-more"
    CLOSE = "sshm-close"
    SPLIT = "sshm-split"
    SAVE = "sshm-download"
    PLAIN_TEXT = "sshm-clipboard"
    TILE = "sshm-grid"
    BROADCAST = "sshm-broadcast"
    STATS = "sshm-stats"
    CPU = "sshm-monitor"
    MEMORY = "sshm-stats"
    DISK = "sshm-server"
    INTERRUPT = "sshm-interrupt"
    DETAILS = "sshm-info"
    OPEN_FOLDER = "sshm-folder-open"
    TAG = "sshm-tag"
    CHECK = "sshm-check"
    INFO = "sshm-info"
    WARNING = "sshm-warning"
    ERROR = "sshm-error"
    EYE = "sshm-eye"
