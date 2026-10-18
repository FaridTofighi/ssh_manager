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
    READ_ONLY = 12  # host is read-only / shared / permission-restricted (lock overlay)


class Icons:
    """Icon names. ``sshm-*`` names come from the bundled outline set
    (``gtkui/assets/icons``); others fall back to the system icon theme."""

    # Session-tree family (see gtkui/tree_icons.py): folder / open folder for
    # directory groups, file-conf for *.conf groups, server for hosts.
    GROUP = "sshm-folder"
    FOLDER = "sshm-folder"
    FOLDER_OPEN = "sshm-folder-open"
    FILE_CONF = "sshm-file-conf"
    SUBGROUP = "sshm-file-conf"
    HOST = "sshm-server"
    SERVER = "sshm-server"
    # Overlays composited onto tree icons.
    BADGE_DOT = "sshm-badge-dot"
    BADGE_RING = "sshm-badge-ring"
    BADGE_FAILED = "sshm-badge-failed"
    BADGE_CUTOUT = "sshm-badge-cutout"
    BADGE_LOCK = "sshm-badge-lock"
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
    CONFIG_FILE = "sshm-file-conf"
    SHARED = "sshm-globe"
    GIT_UPDATE = "sshm-download"
    SHORTCUTS = "sshm-help"
    HELP = "sshm-help"
    ABOUT = "sshm-info"
    INFO = "sshm-info"
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
    OPEN_FOLDER = "sshm-folder-open"  # file chooser actions (Import…)
    TAG = "sshm-tag"
    CHECK = "sshm-check"
    INFO = "sshm-info"
    WARNING = "sshm-warning"
    ERROR = "sshm-error"
    EYE = "sshm-eye"
