"""Interface density: the geometry and type scale of the GTK front-end.

Two presets share one set of keys:

* :data:`COMFORTABLE` is the Relay reference (``docs/ui-target/relay-reference.png``);
  ``tools/compare_to_reference.py`` checks renders made with it.
* :data:`COMPACT` (the default) shrinks type, chrome and spacing so the
  terminal gets most of the window.

Views and the stylesheet never keep their own copies of these numbers: they
read the *active* preset (directly through :func:`active`, or through the live
``Space`` / ``Size`` / ``Layout`` / ``FontScale`` / ``Radius`` views in
``tokens.py``) when they lay themselves out, and lay themselves out again from
:func:`add_listener` callbacks when the density changes at runtime.

The module is pure Python so it can be imported and tested without GTK.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, fields
from enum import Enum
from typing import Callable, Dict, Iterator, List, Tuple

MIN_FONT_PX = 11.0  # accessibility floor for any rendered UI text
MIN_TARGET_PX = 24  # smallest click target
MIN_TREE_ROW_PX = 28
MIN_SIDEBAR_PX = 220  # the sidebar never gets narrower than this


class Density(str, Enum):
    COMPACT = "compact"
    COMFORTABLE = "comfortable"

    @classmethod
    def coerce(cls, value: object, default: "Density" = None) -> "Density":  # type: ignore[assignment]
        raw = value.value if isinstance(value, Enum) else value
        try:
            return cls(str(raw).strip().lower())
        except ValueError:
            return default or DEFAULT_DENSITY


class _Group:
    """Shared helpers for the token groups (frozen dataclasses)."""

    def items(self) -> Iterator[Tuple[str, object]]:
        for field in fields(self):  # type: ignore[arg-type]
            yield field.name, getattr(self, field.name)

    def keys(self) -> Tuple[str, ...]:
        return tuple(name for name, _value in self.items())


@dataclass(frozen=True)
class SpaceTokens(_Group):
    """Spacing scale (px). Only these values may be used for margins and gaps."""

    XS: int
    S: int
    M: int
    L: int
    XL: int
    XXL: int
    XXXL: int
    GUTTER: int  # horizontal padding of the host page bands

    def values(self) -> Tuple[int, ...]:
        return (self.XS, self.S, self.M, self.L, self.XL, self.XXL, self.XXXL, self.GUTTER)


@dataclass(frozen=True)
class RadiusTokens(_Group):
    SMALL: int  # small chips
    KEYCAP: int  # keyboard hint badges
    MEDIUM: int  # buttons, selected tree rows
    INPUT: int  # entries, logo tile
    LARGE: int  # cards, popovers, toasts, host icon tile
    CARD: int  # empty-state icon card
    PILL: int  # pills, badges, dots

    def values(self) -> Tuple[int, ...]:
        return (self.SMALL, self.KEYCAP, self.MEDIUM, self.INPUT, self.LARGE, self.CARD, self.PILL)


@dataclass(frozen=True)
class SizeTokens(_Group):
    """Component sizes (px). Band heights are content heights; bands add a 1px border."""

    TOPBAR: int
    SIDEBAR: int
    SIDEBAR_MIN: int
    SIDEBAR_MAX: int
    PAGE_BAR: int
    HOST_HEADER: int  # compact: the merged host bar (row A)
    SESSION_STRIP: int  # session tabs + toolbar row, total height incl. its 1px border
    INFO_BAR: int
    SIDEBAR_HEADER: int
    SIDEBAR_FOOTER: int
    TREE_ROW: int
    TREE_GROUP_ROW: int  # top-level group rows
    TREE_INSET: int
    TREE_INDENT: int
    LOGO_TILE: int
    LOGO_GLYPH: int
    AVATAR: int
    ICON_BUTTON: int
    TOOLBAR_BUTTON: int  # CSS min-height; + 2 × Space.XS padding + 2px border outside
    SEARCH: int
    FILTER_BUTTON: int
    BUTTON: int
    BUTTON_LARGE: int
    BUTTON_MIN: int  # min-height of ordinary buttons
    PRIMARY_MIN_WIDTH: int
    HERO_BUTTON: int
    ENTRY: int
    MENU_ROW: int
    HOST_TILE: int
    HOST_TILE_ICON: int
    EMPTY_CARD: int
    EMPTY_CARD_ICON: int
    BADGE: int
    ICON: int  # ordinary inline icons
    ICON_TOPBAR: int
    ICON_BUTTON_GLYPH: int  # icon inside text buttons (Add host, hero)
    ICON_FOOTER: int
    STATUS_DOT: int  # tree status, latency
    ICON_SMALL: int  # status-bar stat icons
    DOT_BADGE: int  # filter-active badge
    DOT_TAB: int  # session chips, tile headers
    DOT_LARGE: int  # health indicator, status bar
    POPOVER_WIDTH: int  # filter popover
    POPOVER_WIDE: int  # message history
    POPOVER_MAX_HEIGHT: int
    WORKSPACE_POPOVER: int
    SETTINGS_NAV: int  # settings / host dialog page list
    SIDE_PANEL: int  # config viewer issues column
    TERMINAL_PREVIEW: int  # preview terminal in Settings
    TAB_LABEL_MIN: int  # session tab label width (ellipsized)
    TAB_LABEL_MAX: int
    PANEL_WIDTH: int  # host details / activity slide-over
    DIALOG_PADDING: int
    DIALOG_GAP: int
    TERMINAL_PAD_X: int
    TERMINAL_PAD_Y: int
    WORKSPACE_PAD: int  # canvas padding around the terminal (0 = edge to edge)


@dataclass(frozen=True)
class TopbarTokens(_Group):
    BRAND_INSET: int  # window edge → logo tile
    WORDMARK_GAP: int  # logo tile → "SSH Manager"
    DIVIDER_HEIGHT: int
    BRAND_DIVIDER_OFFSET: int  # brand block = pane position + this (divider on the sidebar edge)
    WORKSPACE_GAP: int  # divider → workspace button
    WORKSPACE_NAME_GAP: int  # "WORKSPACE" → inventory name
    HEALTH_GAP: int  # health text → activity button
    HEALTH_DOT_GAP: int
    HELP_GAP: int  # activity → help button
    AVATAR_GAP: int  # help → avatar


@dataclass(frozen=True)
class SidebarTokens(_Group):
    SIDE: int  # left/right padding of the header and search row
    HEADER_TOP: int  # sidebar top → header block
    HEADER_HEIGHT: int
    TITLE_GAP: int  # eyebrow → "Hosts"
    TITLE_SLACK: int  # line-box slack under "Hosts" that the search row may overlap
    ADD_HOST_TOP: int  # header top → Add host
    ADD_HOST_PAD_START: int
    ADD_HOST_PAD_END: int
    SEARCH_TOP: int  # header bottom → search box
    SEARCH_GAP: int  # search box → filter button
    SEARCH_END: int  # filter button → sidebar edge
    SEARCH_ICON_INSET: int  # entry edge → search icon
    SEARCH_TEXT_INSET: int  # entry edge → placeholder/text
    TREE_TOP_GAP: int  # search box → first tree row
    FOOTER_GAP: int  # Settings → Keys


@dataclass(frozen=True)
class TreeTokens(_Group):
    GROUP_GAP: int  # extra space above every top-level group but the first
    LEAD: int  # x of the level-0 chevron box
    CHEVRON: int
    FOLDER: int
    HOST_ICON: int
    GROUP_ICON_X: int  # level-0 group: chevron x → folder icon
    GROUP_TEXT_X: int  # level-0 group: chevron x → label
    CHILD_TEXT_X: int  # nested group: chevron x → label
    HOST_ICON_X: int  # host: indent x → monitor icon
    HOST_TEXT_X: int  # host: indent x → label
    COUNT_RIGHT: int  # count right edge → row right edge
    DOT_RIGHT: int  # status-dot centre → row right edge
    DOT: int
    BAR_WIDTH: int
    BAR_HEIGHT: int
    RADIUS: int


@dataclass(frozen=True)
class PageTokens(_Group):
    CRUMB_GAP: int
    HEADER_GAP: int  # host tile → title block
    TITLE_GAP: int  # title → badge → address (compact host bar)
    STAT_GAP: int  # between header stats
    TAB_GAP: int  # between page tabs
    INFO_GAP: int  # between info/status-bar fields
    INFO_COMPACT_WIDTH: int  # live session + narrower bar: hide the secondary fields
    NARROW_WIDTH: int  # window width below which the narrow variant applies


@dataclass(frozen=True)
class ReadyTokens(_Group):
    BODY_WIDTH: int
    TITLE_TOP: int
    BODY_TOP: int
    BUTTON_TOP: int
    CAPTION_TOP: int


@dataclass(frozen=True)
class FontTokens(_Group):
    """Type scale in CSS px at the default application font (11 pt)."""

    CAPS: float
    BADGE: float
    CAPTION: float
    MONO: float
    BODY: float
    TREE: float
    TREE_COUNT: float
    SUBTITLE: float
    WORDMARK: float
    HERO_TITLE: float
    HOST_TITLE: float
    PAGE_TITLE: float
    TITLE: float
    DIALOG_TITLE: float
    PAGE_TITLE_WEIGHT: int
    HOST_TITLE_WEIGHT: int


@dataclass(frozen=True)
class LayoutVariant(_Group):
    """Structural switches that go with a density."""

    SINGLE_ROW_SIDEBAR_HEADER: bool  # "Hosts" + count + Add host on one row, no eyebrow
    MERGED_PAGE_HEADER: bool  # page bar + host header → one host bar; tabs share a row with sessions
    SINGLE_LINE_STATUS: bool  # one-line status bar instead of caption-over-value fields
    TERMINAL_CARD: bool  # rounded, bordered card around the terminal


@dataclass(frozen=True)
class DensityTokens:
    density: Density
    space: SpaceTokens
    radius: RadiusTokens
    size: SizeTokens
    topbar: TopbarTokens
    sidebar: SidebarTokens
    tree: TreeTokens
    page: PageTokens
    ready: ReadyTokens
    font: FontTokens
    variant: LayoutVariant
    caps_tracking: str
    badge_tracking: str
    dialog_scale: float  # default dialog sizes (dialogs stay resizable)
    # Body text relative to the app font: the window's base font is the app font
    # size × this, so unstyled text (session chips, dialog labels) gets the
    # density's body size, and ``em`` sizes are relative to that base.
    base_font_scale: float

    GROUPS = ("space", "radius", "size", "topbar", "sidebar", "tree", "page", "ready", "font", "variant")

    @property
    def compact(self) -> bool:
        return self.density is Density.COMPACT

    def groups(self) -> Iterator[Tuple[str, _Group]]:
        for name in self.GROUPS:
            yield name, getattr(self, name)

    def flat(self) -> Dict[str, object]:
        """``{"size.TOPBAR": 44, ...}`` for every token."""

        return {f"{group}.{key}": value for group, tokens in self.groups() for key, value in tokens.items()}


COMFORTABLE = DensityTokens(
    density=Density.COMFORTABLE,
    space=SpaceTokens(XS=4, S=8, M=12, L=16, XL=20, XXL=24, XXXL=32, GUTTER=36),
    radius=RadiusTokens(SMALL=4, KEYCAP=6, MEDIUM=8, INPUT=10, LARGE=12, CARD=18, PILL=999),
    size=SizeTokens(
        TOPBAR=76, SIDEBAR=408, SIDEBAR_MIN=300, SIDEBAR_MAX=520,
        PAGE_BAR=73, HOST_HEADER=123, SESSION_STRIP=48, INFO_BAR=69,
        SIDEBAR_HEADER=52, SIDEBAR_FOOTER=66,
        TREE_ROW=41, TREE_GROUP_ROW=41, TREE_INSET=12, TREE_INDENT=25,
        LOGO_TILE=42, LOGO_GLYPH=22, AVATAR=40, ICON_BUTTON=40, TOOLBAR_BUTTON=28,
        SEARCH=46, FILTER_BUTTON=32, BUTTON=40, BUTTON_LARGE=44, BUTTON_MIN=24, PRIMARY_MIN_WIDTH=110,
        HERO_BUTTON=51, ENTRY=32, MENU_ROW=36,
        HOST_TILE=54, HOST_TILE_ICON=24, EMPTY_CARD=72, EMPTY_CARD_ICON=32, BADGE=22,
        ICON=16, ICON_TOPBAR=20, ICON_BUTTON_GLYPH=20, ICON_FOOTER=18, STATUS_DOT=7,
        ICON_SMALL=14, DOT_BADGE=6, DOT_TAB=8, DOT_LARGE=9,
        POPOVER_WIDTH=320, POPOVER_WIDE=380, POPOVER_MAX_HEIGHT=360, WORKSPACE_POPOVER=280,
        SETTINGS_NAV=190, SIDE_PANEL=260, TERMINAL_PREVIEW=120,
        TAB_LABEL_MIN=96, TAB_LABEL_MAX=220, PANEL_WIDTH=360,
        DIALOG_PADDING=16, DIALOG_GAP=12,
        TERMINAL_PAD_X=8, TERMINAL_PAD_Y=8, WORKSPACE_PAD=16,
    ),
    topbar=TopbarTokens(
        BRAND_INSET=29, WORDMARK_GAP=11, DIVIDER_HEIGHT=28, BRAND_DIVIDER_OFFSET=2,
        WORKSPACE_GAP=22, WORKSPACE_NAME_GAP=16, HEALTH_GAP=19, HEALTH_DOT_GAP=10,
        HELP_GAP=12, AVATAR_GAP=17,
    ),
    sidebar=SidebarTokens(
        SIDE=24, HEADER_TOP=29, HEADER_HEIGHT=52, TITLE_GAP=5, TITLE_SLACK=9, ADD_HOST_TOP=12,
        ADD_HOST_PAD_START=13, ADD_HOST_PAD_END=15,
        SEARCH_TOP=22, SEARCH_GAP=11, SEARCH_END=25, SEARCH_ICON_INSET=15, SEARCH_TEXT_INSET=45, TREE_TOP_GAP=20, FOOTER_GAP=10,
    ),
    tree=TreeTokens(
        GROUP_GAP=7, LEAD=26, CHEVRON=14, FOLDER=18, HOST_ICON=16,
        GROUP_ICON_X=26, GROUP_TEXT_X=53, CHILD_TEXT_X=26, HOST_ICON_X=5, HOST_TEXT_X=32,
        COUNT_RIGHT=25, DOT_RIGHT=28, DOT=7, BAR_WIDTH=4, BAR_HEIGHT=24, RADIUS=8,
    ),
    page=PageTokens(
        CRUMB_GAP=10, HEADER_GAP=18, TITLE_GAP=12, STAT_GAP=40, TAB_GAP=29, INFO_GAP=48,
        INFO_COMPACT_WIDTH=1250, NARROW_WIDTH=1100,
    ),
    ready=ReadyTokens(BODY_WIDTH=520, TITLE_TOP=24, BODY_TOP=14, BUTTON_TOP=32, CAPTION_TOP=16),
    font=FontTokens(
        CAPS=12, BADGE=11, CAPTION=13, MONO=14, BODY=15, TREE=16, TREE_COUNT=13, SUBTITLE=16,
        WORDMARK=20, HERO_TITLE=22, HOST_TITLE=26, PAGE_TITLE=28, TITLE=20, DIALOG_TITLE=18,
        PAGE_TITLE_WEIGHT=400, HOST_TITLE_WEIGHT=400,
    ),
    variant=LayoutVariant(
        SINGLE_ROW_SIDEBAR_HEADER=False, MERGED_PAGE_HEADER=False, SINGLE_LINE_STATUS=False, TERMINAL_CARD=True,
    ),
    caps_tracking="0.12em",
    badge_tracking="0.06em",
    dialog_scale=1.0,
    base_font_scale=1.0,
)

COMPACT = DensityTokens(
    density=Density.COMPACT,
    space=SpaceTokens(XS=2, S=4, M=8, L=12, XL=16, XXL=16, XXXL=24, GUTTER=12),
    radius=RadiusTokens(SMALL=3, KEYCAP=4, MEDIUM=6, INPUT=8, LARGE=8, CARD=12, PILL=999),
    size=SizeTokens(
        # Band heights are content heights (+1px border): 44, 44 and 28 in total; the strip is 33.
        TOPBAR=43, SIDEBAR=284, SIDEBAR_MIN=220, SIDEBAR_MAX=480,
        PAGE_BAR=43, HOST_HEADER=43, SESSION_STRIP=33, INFO_BAR=27,
        SIDEBAR_HEADER=40, SIDEBAR_FOOTER=31,
        TREE_ROW=28, TREE_GROUP_ROW=30, TREE_INSET=6, TREE_INDENT=16,
        LOGO_TILE=28, LOGO_GLYPH=16, AVATAR=28, ICON_BUTTON=28, TOOLBAR_BUTTON=22,
        SEARCH=32, FILTER_BUTTON=32, BUTTON=28, BUTTON_LARGE=28, BUTTON_MIN=24, PRIMARY_MIN_WIDTH=88,
        HERO_BUTTON=34, ENTRY=30, MENU_ROW=28,
        HOST_TILE=28, HOST_TILE_ICON=16, EMPTY_CARD=48, EMPTY_CARD_ICON=24, BADGE=18,
        ICON=16, ICON_TOPBAR=16, ICON_BUTTON_GLYPH=14, ICON_FOOTER=16, STATUS_DOT=6,
        ICON_SMALL=12, DOT_BADGE=5, DOT_TAB=7, DOT_LARGE=8,
        POPOVER_WIDTH=280, POPOVER_WIDE=340, POPOVER_MAX_HEIGHT=320, WORKSPACE_POPOVER=240,
        SETTINGS_NAV=170, SIDE_PANEL=220, TERMINAL_PREVIEW=100,
        TAB_LABEL_MIN=96, TAB_LABEL_MAX=220, PANEL_WIDTH=360,
        DIALOG_PADDING=16, DIALOG_GAP=8,
        TERMINAL_PAD_X=6, TERMINAL_PAD_Y=4, WORKSPACE_PAD=0,
    ),
    topbar=TopbarTokens(
        BRAND_INSET=12, WORDMARK_GAP=8, DIVIDER_HEIGHT=20, BRAND_DIVIDER_OFFSET=2,
        WORKSPACE_GAP=8, WORKSPACE_NAME_GAP=8, HEALTH_GAP=8, HEALTH_DOT_GAP=6,
        HELP_GAP=4, AVATAR_GAP=8,
    ),
    sidebar=SidebarTokens(
        SIDE=12, HEADER_TOP=0, HEADER_HEIGHT=40, TITLE_GAP=0, TITLE_SLACK=0, ADD_HOST_TOP=0,
        ADD_HOST_PAD_START=8, ADD_HOST_PAD_END=10,
        SEARCH_TOP=0, SEARCH_GAP=6, SEARCH_END=12, SEARCH_ICON_INSET=10, SEARCH_TEXT_INSET=32, TREE_TOP_GAP=6, FOOTER_GAP=4,
    ),
    tree=TreeTokens(
        GROUP_GAP=3, LEAD=12, CHEVRON=12, FOLDER=16, HOST_ICON=14,
        GROUP_ICON_X=18, GROUP_TEXT_X=40, CHILD_TEXT_X=18, HOST_ICON_X=2, HOST_TEXT_X=22,
        COUNT_RIGHT=12, DOT_RIGHT=14, DOT=6, BAR_WIDTH=3, BAR_HEIGHT=16, RADIUS=6,
    ),
    page=PageTokens(
        CRUMB_GAP=4, HEADER_GAP=8, TITLE_GAP=8, STAT_GAP=12, TAB_GAP=20, INFO_GAP=8,
        INFO_COMPACT_WIDTH=900, NARROW_WIDTH=1100,
    ),
    ready=ReadyTokens(BODY_WIDTH=420, TITLE_TOP=12, BODY_TOP=6, BUTTON_TOP=16, CAPTION_TOP=8),
    font=FontTokens(
        # Caps 10.5 and badge 10 in the brief sit below the 11px floor; the floor wins.
        CAPS=11, BADGE=11, CAPTION=11.5, MONO=12, BODY=13, TREE=13, TREE_COUNT=11.5, SUBTITLE=13,
        WORDMARK=15, HERO_TITLE=17, HOST_TITLE=16, PAGE_TITLE=15, TITLE=15, DIALOG_TITLE=15,
        PAGE_TITLE_WEIGHT=600, HOST_TITLE_WEIGHT=600,
    ),
    variant=LayoutVariant(
        SINGLE_ROW_SIDEBAR_HEADER=True, MERGED_PAGE_HEADER=True, SINGLE_LINE_STATUS=True, TERMINAL_CARD=False,
    ),
    caps_tracking="0.08em",
    badge_tracking="0.04em",
    dialog_scale=0.85,
    base_font_scale=round(13 / (11 * 96 / 72), 4),  # 13px body at the default 11pt
)

PRESETS: Dict[Density, DensityTokens] = {Density.COMPACT: COMPACT, Density.COMFORTABLE: COMFORTABLE}
DEFAULT_DENSITY = Density.COMPACT


def preset(density: object) -> DensityTokens:
    return PRESETS[Density.coerce(density)]


# ------------------------------------------------------------------ active
_ACTIVE: DensityTokens = PRESETS[DEFAULT_DENSITY]
_LISTENERS: List[Callable[[DensityTokens], None]] = []


def active() -> DensityTokens:
    """The preset the UI is laid out with right now."""

    return _ACTIVE


def set_active(density: object, *, notify: bool = True) -> DensityTokens:
    """Make ``density`` the active preset; listeners run only when it changed."""

    global _ACTIVE
    tokens = preset(density)
    changed = tokens is not _ACTIVE
    _ACTIVE = tokens
    if changed and notify:
        for callback in list(_LISTENERS):
            callback(tokens)
    return tokens


def add_listener(callback: Callable[[DensityTokens], None]) -> None:
    if callback not in _LISTENERS:
        _LISTENERS.append(callback)


def remove_listener(callback: Callable[[DensityTokens], None]) -> None:
    if callback in _LISTENERS:
        _LISTENERS.remove(callback)


@contextmanager
def using(density: object, *, notify: bool = False):
    """Temporarily make ``density`` active (tests, snapshots); restores the previous preset."""

    previous = _ACTIVE.density
    set_active(density, notify=notify)
    try:
        yield _ACTIVE
    finally:
        set_active(previous, notify=notify)


def floor_violations(tokens: DensityTokens) -> List[str]:
    """Token values below the accessibility floor (empty = OK)."""

    problems = []
    for name, value in tokens.font.items():
        if not name.endswith("_WEIGHT") and float(value) < MIN_FONT_PX:
            problems.append(f"font.{name}={value} < {MIN_FONT_PX}px")
    size = tokens.size
    targets = {name: getattr(size, name) for name in (
        "ICON_BUTTON", "AVATAR", "BUTTON", "BUTTON_LARGE", "BUTTON_MIN", "HERO_BUTTON", "ENTRY", "SEARCH",
        "FILTER_BUTTON", "MENU_ROW",
    )}
    targets["TOOLBAR_BUTTON"] = size.TOOLBAR_BUTTON + 2 * tokens.space.XS + 2  # outer size
    for name, value in targets.items():
        if value < MIN_TARGET_PX:
            problems.append(f"size.{name}={value} < {MIN_TARGET_PX}px target")
    for name in ("TREE_ROW", "TREE_GROUP_ROW"):
        if getattr(size, name) < MIN_TREE_ROW_PX:
            problems.append(f"size.{name}={getattr(size, name)} < {MIN_TREE_ROW_PX}px row")
    return problems
