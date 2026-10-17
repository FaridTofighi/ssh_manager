"""Generate the application stylesheet from design tokens.

The stylesheet is layered over the system theme (Adwaita or a compatible
theme): it defines ``@define-color sm_*`` tokens and styles application-owned
classes, leaving stock widgets to the system theme except where states must be
made distinguishable. All sizes come from the scales in ``tokens.py`` and font
sizes are relative (``em``) so the "Application font size" setting scales the
whole UI.
"""

from __future__ import annotations

import re
from typing import Set

from .tokens import FontScale, Motion, Palette, Radius, Space

TOKEN_PREFIX = "sm_"

_TEMPLATE = """
/* ===================================================================
 * SSH Manager stylesheet ({name} theme) - generated from tokens.py.
 * Do not edit by hand: change the tokens or stylesheet.py instead.
 * =================================================================== */

{defines}

/* ---------------------------------------------------------- surfaces */
.sm-window,
.sm-window .background-surface {{
    background-color: @sm_bg_window;
    color: @sm_text_primary;
}}

.surface {{
    background-color: @sm_bg_surface;
    color: @sm_text_primary;
}}

.surface-raised {{
    background-color: @sm_bg_surface_raised;
    color: @sm_text_primary;
    border: 1px solid @sm_border_subtle;
    border-radius: {r_lg}px;
}}

.sidebar-pane {{
    background-color: @sm_bg_surface;
    border-right: 1px solid @sm_border_subtle;
}}

.sidebar-pane:dir(rtl) {{
    border-right: none;
    border-left: 1px solid @sm_border_subtle;
}}

.sidebar-pane treeview.view {{
    background-color: @sm_bg_surface;
    color: @sm_text_primary;
}}

.sidebar-pane treeview.view:hover {{
    background-color: @sm_bg_hover;
}}

.sidebar-pane treeview.view:selected,
.sidebar-pane treeview.view:selected:focus {{
    background-color: @sm_bg_selected;
    color: @sm_text_primary;
}}

.sm-window paned > separator {{
    background-color: @sm_border_subtle;
    min-width: 1px;
    min-height: 1px;
}}

/* -------------------------------------------------------- typography */
.title-1 {{
    font-size: {fs_title}em;
    font-weight: bold;
}}

.subtitle {{
    font-size: {fs_subtitle}em;
}}

.caption {{
    font-size: {fs_caption}em;
}}

.section-title {{
    font-weight: bold;
    font-size: {fs_subtitle}em;
    color: @sm_text_primary;
}}

.section-description,
.sm-window .dim-label,
.sm-dialog .dim-label {{
    color: @sm_text_secondary;
    opacity: 1;
}}

.form-error {{
    color: @sm_danger;
    font-size: {fs_caption}em;
}}

.form-help {{
    color: @sm_text_secondary;
    font-size: {fs_caption}em;
}}

entry.invalid {{
    border-color: @sm_danger;
    box-shadow: inset 0 0 0 1px @sm_danger;
}}

/* ------------------------------------------------------------- focus */
.sm-window *:focus,
.sm-dialog *:focus {{
    outline-color: @sm_focus_ring;
    outline-width: 2px;
    outline-style: solid;
    outline-offset: -2px;
}}

/* ----------------------------------------------------------- buttons */
button.toolbar-icon {{
    padding: {xs}px;
    min-width: 28px;
    min-height: 28px;
    border-radius: {r_md}px;
}}

button.toolbar-icon:checked {{
    background-color: @sm_bg_selected;
    color: @sm_text_primary;
}}

button.toolbar-icon.danger-toggle:checked {{
    background-color: @sm_danger_strong;
    color: @sm_on_danger;
}}

button.suggested-action {{
    background-image: none;
    background-color: @sm_accent;
    color: @sm_on_accent;
    border-color: @sm_accent;
}}

button.suggested-action:hover {{
    background-color: @sm_accent_hover;
}}

button.destructive-action {{
    background-image: none;
    background-color: @sm_danger_strong;
    color: @sm_on_danger;
    border-color: @sm_danger_strong;
}}

button.suggested-action:disabled,
button.destructive-action:disabled {{
    background-color: @sm_chip_bg;
    color: @sm_text_disabled;
    border-color: @sm_border_subtle;
}}

/* -------------------------------------------------------------- chips */
.chip {{
    background-color: @sm_chip_bg;
    color: @sm_text_primary;
    border: 1px solid @sm_border_subtle;
    border-radius: {r_pill}px;
    padding: 0 {s}px;
    font-size: {fs_caption}em;
}}

.chip.accent {{
    background-color: @sm_bg_selected;
    border-color: @sm_accent;
}}

.badge {{
    background-color: @sm_accent;
    color: @sm_on_accent;
    border-radius: {r_pill}px;
    padding: 0 {xs}px;
    min-width: 8px;
    font-size: 0.75em;
    font-weight: bold;
}}

.badge.subtle {{
    background-color: @sm_chip_bg;
    color: @sm_text_secondary;
}}

/* -------------------------------------------------------- status dots */
.status-dot {{ color: @sm_text_disabled; }}
.status-dot.connected {{ color: @sm_success; }}
.status-dot.connecting {{ color: @sm_info; }}
.status-dot.failed {{ color: @sm_danger; }}
.status-dot.unknown {{ color: @sm_text_disabled; }}

/* ---------------------------------------------------------- headerbar */
headerbar.sm-headerbar {{
    padding-left: {s}px;
    padding-right: {s}px;
}}

headerbar.sm-headerbar .badge {{
    margin: 0;
}}

/* ------------------------------------------------------------ sidebar */
.sidebar-search {{
    padding: {s}px;
    border-bottom: 1px solid @sm_border_subtle;
}}

/* ----------------------------------------------------------- details */
.details-drawer {{
    background-color: @sm_bg_surface;
    border-top: 1px solid @sm_border_subtle;
    padding: {m}px {l}px;
}}

.details-drawer .detail-key {{
    color: @sm_text_secondary;
}}

/* --------------------------------------------------------- workspace */
.session-toolbar {{
    background-color: @sm_bg_surface;
    border-bottom: 1px solid @sm_border_subtle;
    padding: {xs}px {s}px;
}}

notebook.session-tabs > header {{
    background-color: @sm_bg_window;
    border-bottom: 1px solid @sm_border_subtle;
}}

notebook.session-tabs > header > tabs > tab {{
    padding: {xs}px {s}px;
    border-radius: {r_md}px {r_md}px 0 0;
    color: @sm_text_secondary;
}}

notebook.session-tabs > header > tabs > tab:hover {{
    background-color: @sm_bg_hover;
    color: @sm_text_primary;
}}

notebook.session-tabs > header > tabs > tab:checked {{
    background-color: @sm_bg_surface;
    color: @sm_text_primary;
    box-shadow: inset 0 -2px @sm_accent;
}}

.tab-activity {{
    font-weight: bold;
    color: @sm_accent;
}}

.statusbar {{
    background-color: @sm_bg_surface;
    color: @sm_text_secondary;
    border-top: 1px solid @sm_border_subtle;
    padding: 2px {s}px;
    font-size: {fs_caption}em;
}}

.statusbar .stat-warn {{ color: @sm_warning; font-weight: bold; }}
.statusbar .stat-critical {{ color: @sm_danger; font-weight: bold; }}

.broadcast-strip {{
    background-color: @sm_broadcast_bg;
    color: @sm_broadcast_fg;
    padding: {xs}px {m}px;
    font-weight: bold;
}}

.broadcast-active {{
    border: 2px solid @sm_danger;
    border-radius: {r_sm}px;
}}

.tile-frame {{
    border: 1px solid @sm_border_subtle;
    border-radius: {r_md}px;
    background-color: @sm_bg_surface;
}}

.tile-header {{
    padding: {xs}px {s}px;
    border-bottom: 1px solid @sm_border_subtle;
}}

/* -------------------------------------------------------- empty state */
.empty-state .empty-icon {{
    color: @sm_text_secondary;
}}

.empty-state .empty-title {{
    font-size: {fs_title}em;
    font-weight: bold;
}}

.empty-state .empty-description {{
    color: @sm_text_secondary;
}}

/* -------------------------------------------------------------- toasts */
.toast {{
    background-color: @sm_bg_surface_raised;
    color: @sm_text_primary;
    border: 1px solid @sm_border_strong;
    border-radius: {r_md}px;
    padding: {s}px {m}px;
    margin: {m}px;
    box-shadow: 0 4px 12px @sm_shadow;
}}

.toast-info {{ border-left: 4px solid @sm_info; }}
.toast-success {{ border-left: 4px solid @sm_success; }}
.toast-warning {{ border-left: 4px solid @sm_warning; }}
.toast-error {{ border-left: 4px solid @sm_danger; }}

.toast-info:dir(rtl),
.toast-success:dir(rtl),
.toast-warning:dir(rtl),
.toast-error:dir(rtl) {{
    border-left-width: 1px;
    border-right-width: 4px;
}}
.toast-info:dir(rtl) {{ border-right-color: @sm_info; }}
.toast-success:dir(rtl) {{ border-right-color: @sm_success; }}
.toast-warning:dir(rtl) {{ border-right-color: @sm_warning; }}
.toast-error:dir(rtl) {{ border-right-color: @sm_danger; }}

.level-info {{ color: @sm_info; }}
.level-success {{ color: @sm_success; }}
.level-warning {{ color: @sm_warning; }}
.level-error {{ color: @sm_danger; }}

/* ------------------------------------------------------------- menus */
menuitem.destructive label,
modelbutton.destructive {{
    color: @sm_danger;
}}

menuitem .accel-hint,
modelbutton .accel-hint {{
    color: @sm_text_secondary;
}}

/* --------------------------------------------------------- dialogs */
.sm-dialog .settings-sidebar {{
    background-color: @sm_bg_surface;
    border-right: 1px solid @sm_border_subtle;
}}

.sm-dialog .settings-sidebar row {{
    padding: {s}px {m}px;
}}

.sm-dialog .settings-sidebar row:selected {{
    background-color: @sm_bg_selected;
    color: @sm_text_primary;
}}

.sm-dialog .form-section {{
    margin-bottom: {l}px;
}}

{motion}
"""

_MOTION_ENABLED = """
/* ------------------------------------------------------------ motion */
button,
notebook.session-tabs > header > tabs > tab,
.chip,
.toast {{
    transition: background-color {fast}ms {easing}, color {fast}ms {easing}, border-color {fast}ms {easing};
}}
"""

_MOTION_DISABLED = """
/* ------------------------------------------------------------ motion */
/* gtk-enable-animations is off: no transitions. */
"""


def _defines(palette: Palette) -> str:
    return "\n".join(
        f"@define-color {TOKEN_PREFIX}{role} {value};" for role, value in palette.items()
    )


def build_css(palette: Palette, *, animations: bool = True) -> str:
    """Return the complete stylesheet for ``palette``."""

    motion = (_MOTION_ENABLED if animations else _MOTION_DISABLED).format(
        fast=Motion.FAST,
        easing=Motion.EASING,
    )
    return _TEMPLATE.format(
        name=palette.name,
        defines=_defines(palette),
        motion=motion,
        xs=Space.XS,
        s=Space.S,
        m=Space.M,
        l=Space.L,
        xl=Space.XL,
        r_sm=Radius.SMALL,
        r_md=Radius.MEDIUM,
        r_lg=Radius.LARGE,
        r_pill=Radius.PILL,
        fs_caption=FontScale.CAPTION,
        fs_subtitle=FontScale.SUBTITLE,
        fs_title=FontScale.TITLE,
    )


_COLOR_REF = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")
_DEFINE = re.compile(r"@define-color\s+([A-Za-z_][A-Za-z0-9_]*)\s")


def defined_colors(css: str) -> Set[str]:
    return set(_DEFINE.findall(css))


def unresolved_color_refs(css: str) -> Set[str]:
    """Return ``@name`` references that are not defined in ``css`` itself."""

    defined = defined_colors(css)
    refs = {name for name in _COLOR_REF.findall(css) if name != "define"}
    return refs - defined
