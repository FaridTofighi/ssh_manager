"""Generate the application stylesheet from design tokens.

The stylesheet reproduces the "Relay" reference design on top of GTK 3: it
defines ``@define-color sm_*`` tokens, styles the application's own regions
(top bar, sidebar, host page, workspace) and restyles stock controls
(buttons, entries, checks, switches, menus, popovers, scrollbars) so that no
default theme accent shows through. Sizes come from ``tokens.Size``,
``Space`` and ``Radius``; font sizes are relative (``em``) so the
*Application font size* setting scales the whole UI.
"""

from __future__ import annotations

import re
from string import Template
from typing import Dict, Set

from .tokens import (
    BADGE_TRACKING,
    CAPS_TRACKING,
    MONO_FONT_FAMILIES,
    UI_FONT_FAMILIES,
    em,
    FontScale,
    Motion,
    Palette,
    Radius,
    Size,
    Space,
)

TOKEN_PREFIX = "sm_"

_TEMPLATE = Template(r"""
/* ===================================================================
 * SSH Manager stylesheet ($name theme) - generated from tokens.py.
 * Do not edit by hand: change the tokens or stylesheet.py instead.
 * =================================================================== */

$defines

/* -------------------------------------------------------------- base */
.sm-window, .sm-dialog {
    font-family: $ui_fonts;
    background-color: @sm_bg_window;
    color: @sm_text_primary;
}

.sm-window *:selected, .sm-dialog *:selected,
.sm-window selection, .sm-dialog selection,
.sm-window entry selection, .sm-dialog entry selection,
.sm-window textview text selection, .sm-dialog textview text selection {
    background-color: @sm_accent_tint;
    color: @sm_accent_text;
}

.mono { font-family: $mono_fonts; }

.caps {
    font-size: ${fs_caps}em;
    font-weight: 500;
    letter-spacing: $caps_tracking;
    color: @sm_text_muted;
}

.caption { font-size: ${fs_caption}em; color: @sm_text_faint; }
.subtitle { font-size: ${fs_subtitle}em; }
.title-1 { font-size: ${fs_title}em; font-weight: 600; }
.section-title { font-weight: 600; font-size: ${fs_body}em; color: @sm_text_primary; }
.section-description, .sm-window .dim-label, .sm-dialog .dim-label {
    color: @sm_text_secondary;
    opacity: 1;
}
.form-error { color: @sm_danger; font-size: ${fs_caption}em; }
.form-help { color: @sm_text_secondary; font-size: ${fs_caption}em; }
.level-info { color: @sm_info; }
.level-success { color: @sm_success; }
.level-warning { color: @sm_warning; }
.level-error { color: @sm_danger; }

/* -------------------------------------------------------- focus ring */
.sm-window *:focus, .sm-dialog *:focus {
    outline-color: alpha(@sm_focus_ring, 0.35);
    outline-width: 2px;
    outline-style: solid;
    outline-offset: 1px;
    -gtk-outline-radius: ${r_md}px;
}

/* ----------------------------------------------------------- buttons */
.sm-window button, .sm-dialog button {
    background-image: none;
    background-color: @sm_bg_surface;
    color: @sm_text_primary;
    border: 1px solid @sm_border_input;
    border-radius: ${r_md}px;
    box-shadow: none;
    text-shadow: none;
    -gtk-icon-shadow: none;
    padding: ${xs}px ${m}px;
    min-height: 24px;
    font-weight: 500;
}
.sm-window button:hover, .sm-dialog button:hover { background-color: @sm_bg_hover; }
.sm-window button:active, .sm-dialog button:active,
.sm-window button:checked, .sm-dialog button:checked {
    background-color: @sm_accent_tint;
    color: @sm_accent_text;
    border-color: @sm_accent_soft_border;
}
.sm-window button:disabled, .sm-dialog button:disabled {
    color: @sm_text_disabled;
    background-color: @sm_bg_surface;
}

.sm-window button.flat, .sm-dialog button.flat,
.sm-window button.toolbar-icon, .sm-dialog button.toolbar-icon {
    background-color: transparent;
    border-color: transparent;
    color: @sm_text_secondary;
}
.sm-window button.flat:hover, .sm-dialog button.flat:hover,
.sm-window button.toolbar-icon:hover, .sm-dialog button.toolbar-icon:hover {
    background-color: @sm_bg_hover;
    color: @sm_text_primary;
}
.sm-window button.toolbar-icon:checked, .sm-dialog button.toolbar-icon:checked {
    background-color: @sm_accent_tint;
    color: @sm_accent_text;
}
.sm-window button.toolbar-icon.danger-toggle:checked {
    background-color: @sm_danger_strong;
    color: @sm_on_danger;
}
button.toolbar-icon {
    padding: ${xs}px;
    min-width: 28px;
    min-height: 28px;
}
button.icon-40 { min-width: ${icon_button}px; min-height: ${icon_button}px; padding: 0; }

.sm-window button.outlined, .sm-dialog button.outlined {
    background-color: @sm_bg_surface;
    border-color: @sm_border_input;
    color: @sm_text_primary;
}

.sm-window button.suggested-action, .sm-dialog button.suggested-action,
.sm-window button.primary, .sm-dialog button.primary {
    background-color: @sm_accent;
    color: @sm_on_accent;
    border-color: @sm_accent;
    font-weight: 600;
    box-shadow: 0 1px 2px @sm_shadow;
    padding: ${xs}px ${l}px;
}
.sm-window button.suggested-action:hover, .sm-dialog button.suggested-action:hover,
.sm-window button.primary:hover, .sm-dialog button.primary:hover {
    background-color: @sm_accent_hover;
    border-color: @sm_accent_hover;
}
.sm-window button.suggested-action:active, .sm-dialog button.suggested-action:active,
.sm-window button.primary:active, .sm-dialog button.primary:active {
    background-color: @sm_accent_pressed;
    border-color: @sm_accent_pressed;
    color: @sm_on_accent;
}
.sm-window button.suggested-action:disabled, .sm-dialog button.suggested-action:disabled,
.sm-window button.primary:disabled, .sm-dialog button.primary:disabled {
    background-color: alpha(@sm_accent, 0.4);
    border-color: transparent;
    color: @sm_on_accent;
    box-shadow: none;
}
.sm-window button.destructive-action, .sm-dialog button.destructive-action {
    background-color: @sm_danger_strong;
    border-color: @sm_danger_strong;
    color: @sm_on_danger;
    font-weight: 600;
}
.sm-window button.destructive-action:disabled, .sm-dialog button.destructive-action:disabled {
    background-color: alpha(@sm_danger_strong, 0.4);
    border-color: transparent;
}

/* ------------------------------------------------- entries & controls */
.sm-window entry, .sm-dialog entry,
.sm-window spinbutton, .sm-dialog spinbutton {
    background-image: none;
    background-color: @sm_bg_input;
    color: @sm_text_primary;
    border: 1px solid @sm_border_input;
    border-radius: ${r_input}px;
    box-shadow: none;
    min-height: 32px;
    padding: 0 ${m}px;
}
.sm-window spinbutton entry, .sm-dialog spinbutton entry { border: none; background: transparent; }
.sm-window entry:focus, .sm-dialog entry:focus,
.sm-window spinbutton:focus, .sm-dialog spinbutton:focus {
    border-color: @sm_accent;
    box-shadow: 0 0 0 2px alpha(@sm_accent, 0.35);
    outline-width: 0;
}
.sm-window entry image, .sm-dialog entry image { color: @sm_text_muted; }
.sm-window entry placeholder, .sm-dialog entry placeholder { color: @sm_text_muted; }
entry.invalid { border-color: @sm_danger; box-shadow: inset 0 0 0 1px @sm_danger; }

.sm-window combobox button, .sm-dialog combobox button { padding: ${xs}px ${m}px; }

.sm-window check, .sm-dialog check,
.sm-window radio, .sm-dialog radio {
    background-image: none;
    background-color: @sm_bg_surface;
    border: 1px solid @sm_border_strong;
    box-shadow: none;
    color: transparent;
}
.sm-window check, .sm-dialog check { border-radius: ${r_sm}px; }
.sm-window check:checked, .sm-dialog check:checked,
.sm-window radio:checked, .sm-dialog radio:checked {
    background-color: @sm_accent;
    border-color: @sm_accent;
    color: @sm_on_accent;
}

.sm-window switch, .sm-dialog switch {
    background-image: none;
    background-color: @sm_border_input;
    border: 1px solid @sm_border_input;
    border-radius: ${r_pill}px;
}
.sm-window switch:checked, .sm-dialog switch:checked {
    background-color: @sm_accent;
    border-color: @sm_accent;
}
.sm-window switch slider, .sm-dialog switch slider {
    background-image: none;
    background-color: @sm_bg_surface;
    border: 1px solid @sm_border_input;
    border-radius: ${r_pill}px;
    box-shadow: none;
}

.sm-window levelbar block.filled, .sm-dialog levelbar block.filled { background-color: @sm_accent; border-color: @sm_accent; }
.sm-window progressbar progress, .sm-dialog progressbar progress { background-color: @sm_accent; border-color: @sm_accent; }
.sm-window scale highlight, .sm-dialog scale highlight { background-color: @sm_accent; }
.sm-window link, .sm-dialog link, .sm-window *:link { color: @sm_accent_text; }

/* -------------------------------------------------------- scrollbars */
.sm-window scrollbar, .sm-dialog scrollbar { background-color: transparent; border: none; }
.sm-window scrollbar slider, .sm-dialog scrollbar slider {
    min-width: 8px;
    min-height: 8px;
    border-radius: ${r_pill}px;
    background-color: @sm_scrollbar;
    border: none;
}

/* ------------------------------------------------- menus & popovers */
.sm-window popover, .sm-dialog popover, popover.sm-popover {
    background-color: @sm_bg_surface_raised;
    border: 1px solid @sm_border_subtle;
    border-radius: ${r_lg}px;
    box-shadow: 0 8px 24px @sm_shadow_popover;
    padding: ${s}px;
}
.sm-window popover modelbutton, .sm-dialog popover modelbutton {
    min-height: 36px;
    border-radius: ${r_md}px;
    padding: 0 ${m}px;
    color: @sm_text_primary;
}
.sm-window popover modelbutton:hover, .sm-dialog popover modelbutton:hover { background-color: @sm_accent_tint; }

menu, .menu, .context-menu {
    background-color: @sm_bg_surface_raised;
    border: 1px solid @sm_border_subtle;
    border-radius: ${r_lg}px;
    padding: ${s}px;
    color: @sm_text_primary;
}
menu menuitem {
    min-height: 36px;
    border-radius: ${r_md}px;
    padding: 0 ${m}px;
    color: @sm_text_primary;
}
menu menuitem:hover {
    background-color: @sm_accent_tint;
    color: @sm_accent_text;
}
menu separator { background-color: @sm_border_subtle; margin: ${xs}px 0; }
menuitem .menu-icon { color: @sm_text_muted; }
menuitem .accel-hint, modelbutton .accel-hint { color: @sm_text_faint; }
menuitem.destructive label, modelbutton.destructive { color: @sm_danger; }

.popover-menu-item {
    min-height: 36px;
    border-radius: ${r_md}px;
    padding: 0 ${m}px;
    border-color: transparent;
    background-color: transparent;
}
.popover-menu-item:hover { background-color: @sm_accent_tint; }
.popover-menu-item.destructive label { color: @sm_danger; }

/* ------------------------------------------------------------ top bar */
headerbar.sm-topbar {
    background-image: none;
    background-color: @sm_bg_surface;
    border: none;
    border-bottom: 1px solid @sm_border_subtle;
    box-shadow: none;
    min-height: ${topbar}px;
    padding: 0 ${m}px 0 0;
    border-radius: 0;
}
headerbar.sm-topbar windowcontrols button, headerbar.sm-topbar > box > button.titlebutton,
headerbar.sm-topbar button.titlebutton {
    min-width: 24px;
    min-height: 24px;
    padding: ${xs}px;
    border: none;
    background: transparent;
    color: @sm_text_muted;
}
.topbar-divider { background-color: @sm_border_subtle; min-width: 1px; margin: 0; }
.accent-icon { color: @sm_accent; }
.workspace-chevron { color: @sm_text_secondary; }
.sm-window button.avatar.attention { box-shadow: 0 0 0 2px @sm_bg_surface, 0 0 0 4px @sm_warning; }
.logo-tile {
    background-color: @sm_accent;
    color: @sm_on_accent;
    border-radius: ${r_input}px;
    min-width: ${logo}px;
    min-height: ${logo}px;
    box-shadow: 0 1px 2px @sm_shadow;
}
.wordmark { font-size: ${fs_wordmark}em; font-weight: 600; color: @sm_text_primary; }
.workspace-label { font-size: ${fs_caps_14}em; letter-spacing: $caps_tracking; color: @sm_text_muted; font-weight: 500; }
.workspace-name { font-size: ${fs_wordmark}em; font-weight: 500; color: @sm_text_primary; }
.sm-window button.workspace-switcher {
    background: transparent;
    border-color: transparent;
    padding: ${xs}px ${s}px;
}
.sm-window button.workspace-switcher:hover { background-color: @sm_bg_hover; }
.sm-window button.health { color: @sm_text_muted; font-size: ${fs_caps_14}em; font-weight: 400; padding: ${xs}px ${s}px; }
.sm-window button.health:hover { color: @sm_text_primary; }
.health-dot { color: @sm_success; }
.health-dot.warning { color: @sm_warning; }
.health-dot.error { color: @sm_danger; }
.sm-window button.topbar-icon {
    background: transparent;
    border-color: transparent;
    color: @sm_text_secondary;
    min-width: ${icon_button}px;
    min-height: ${icon_button}px;
    padding: 0;
    border-radius: ${r_md}px;
}
.sm-window button.topbar-icon:hover { background-color: @sm_bg_hover; color: @sm_text_primary; }
.sm-window button.avatar {
    background-color: @sm_avatar_bg;
    color: @sm_on_accent;
    border: none;
    border-radius: ${r_pill}px;
    min-width: ${avatar}px;
    min-height: ${avatar}px;
    padding: 0;
    font-weight: 600;
    font-size: ${fs_caps_14}em;
}
.sm-window button.avatar:hover { background-color: shade(@sm_avatar_bg, 1.15); }
.badge {
    background-color: @sm_accent;
    color: @sm_on_accent;
    border-radius: ${r_pill}px;
    padding: 0 ${xs}px;
    min-width: 8px;
    font-size: ${fs_badge}em;
    font-weight: 700;
}
.badge.subtle { background-color: @sm_chip_bg; color: @sm_text_secondary; }

/* ------------------------------------------------------------ sidebar */
.sidebar-pane { background-color: @sm_bg_surface; }
paned.main-paned > separator, paned.main-paned > separator.wide {
    background-image: none;
    background-color: @sm_border_subtle;
    -gtk-icon-source: none;
    border: none;
    margin: 0;
    padding: 0;
    min-width: 1px;
    min-height: 1px;
}
.sidebar-eyebrow { font-size: ${fs_caps}em; font-weight: 500; letter-spacing: $caps_tracking; color: @sm_text_muted; }
.sidebar-title { font-size: ${fs_page_title}em; font-weight: 400; color: @sm_text_primary; }
.sm-window button.add-host { min-height: ${button}px; padding: 0 ${l}px; font-size: ${fs_body}em; }
.sm-window entry.sidebar-search-entry {
    min-height: ${search}px;
    background-color: @sm_bg_input;
    border-radius: ${r_input}px;
    font-size: ${fs_body}em;
    padding-left: ${search_text_inset}px;
    padding-right: ${m}px;
}
.sidebar-search .search-icon { color: @sm_text_muted; }
.keycap {
    border: 1px solid @sm_border_input;
    border-radius: ${r_keycap}px;
    padding: 0 ${xs}px;
    font-size: ${fs_caps}em;
    color: @sm_text_muted;
    background-color: @sm_bg_surface;
}
.sm-window button.filter-button {
    min-width: 34px;
    min-height: ${search}px;
    padding: 0;
    border-radius: ${r_md}px;
    background-color: @sm_bg_surface;
    color: @sm_text_secondary;
}
.filter-dot { color: @sm_accent; }
.sidebar-pane treeview.view {
    background-color: transparent;
    color: @sm_text_primary;
    font-size: ${fs_tree}em;
}
.sidebar-pane treeview.view:selected,
.sidebar-pane treeview.view:selected:focus,
.sidebar-pane treeview.view:hover {
    background-color: transparent;
    color: @sm_text_primary;
}
.sidebar-pane treeview.view.expander { color: transparent; }
.sidebar-pane treeview.view:drop(active) { border: 1px solid @sm_accent; }
.sidebar-footer {
    border-top: 1px solid @sm_border_subtle;
    min-height: ${sidebar_footer}px;
    padding: 0 ${l}px;
}
.sm-window button.footer-icon { color: @sm_text_muted; }
.sm-window button.footer-link {
    background: transparent;
    border-color: transparent;
    color: @sm_text_secondary;
    font-weight: 400;
    font-size: ${fs_caps_14}em;
    padding: ${xs}px ${s}px;
}
.sm-window button.footer-link:hover { color: @sm_text_primary; background-color: @sm_bg_hover; }
.bulk-bar {
    background-color: @sm_accent_tint;
    border-top: 1px solid @sm_accent_soft_border;
    padding: ${s}px ${l}px;
    color: @sm_accent_text;
}

/* ---------------------------------------------------------- host page */
.page-canvas { background-color: @sm_bg_canvas; }
.page-bar {
    background-color: @sm_bg_page_bar;
    border-bottom: 1px solid @sm_border_subtle;
    min-height: ${page_bar}px;
    padding: 0 ${gutter}px;
}
.breadcrumb-parent { color: @sm_text_secondary; font-size: ${fs_body}em; }
.breadcrumb-current { color: @sm_text_primary; font-size: ${fs_body}em; font-weight: 600; }
.breadcrumb-sep { color: @sm_text_faint; }
.sm-window button.breadcrumb-link {
    background: transparent;
    border-color: transparent;
    padding: ${xs}px;
    font-weight: 400;
}
.sm-window button.breadcrumb-link:hover { background-color: @sm_bg_hover; }
.sm-window button.page-more { min-width: 40px; min-height: ${button_large}px; padding: 0; color: @sm_text_muted; }
.sm-window button.page-primary { min-height: ${button_large}px; min-width: 144px; font-size: ${fs_body}em; }
.host-header {
    background-color: @sm_bg_surface;
    border-bottom: 1px solid @sm_border_subtle;
    min-height: ${host_header}px;
    padding: 0 ${gutter}px;
}
.host-tile {
    background-color: @sm_accent_tint;
    color: @sm_accent_text;
    border-radius: ${r_lg}px;
    min-width: ${host_tile}px;
    min-height: ${host_tile}px;
}
.host-title { font-size: ${fs_host_title}em; font-weight: 400; color: @sm_text_primary; }
.host-address { font-family: $mono_fonts; font-size: ${fs_mono}em; color: @sm_text_secondary; }
.env-badge {
    border-radius: ${r_pill}px;
    padding: 0 ${s}px;
    min-height: ${badge}px;
    font-size: ${fs_badge}em;
    font-weight: 700;
    letter-spacing: $badge_tracking;
}
.stat-label { font-size: ${fs_caps}em; font-weight: 500; letter-spacing: $caps_tracking; color: @sm_text_muted; }
.stat-value { font-size: ${fs_body}em; font-weight: 600; color: @sm_text_primary; }
.stat-good { color: @sm_success; }
.stat-warn { color: @sm_warning; font-weight: 600; }
.stat-critical, .stat-bad { color: @sm_danger; font-weight: 600; }

.page-tabs {
    background-color: @sm_bg_surface;
    border-bottom: 1px solid @sm_border_subtle;
    min-height: ${tabs}px;
    padding: 0 ${gutter}px;
}
.sm-window button.page-tab {
    background: transparent;
    border: none;
    border-radius: 0;
    border-bottom: 2px solid transparent;
    padding: 0 0;
    margin: 0 ${m}px 0 0;
    color: @sm_tab_inactive;
    font-weight: 500;
    font-size: ${fs_body}em;
    box-shadow: none;
}
.sm-window button.page-tab:hover { color: @sm_text_primary; background: transparent; }
.sm-window button.page-tab:checked {
    color: @sm_accent_text;
    border-bottom-color: @sm_accent;
    background: transparent;
}

.info-bar {
    background-color: @sm_bg_surface;
    border-top: 1px solid @sm_border_subtle;
    min-height: ${info_bar}px;
    padding: 0 ${gutter}px;
}
.info-label { font-size: ${fs_badge}em; font-weight: 500; letter-spacing: $caps_tracking; color: @sm_text_muted; }
.info-value { font-size: ${fs_mono}em; font-weight: 600; color: @sm_text_primary; }
.info-value.mono { font-family: $mono_fonts; }

/* -------------------------------------------------------- empty state */
.empty-card {
    background-color: @sm_bg_surface;
    border: 1px solid @sm_border_subtle;
    border-radius: ${r_card}px;
    min-width: ${empty_card}px;
    min-height: ${empty_card}px;
    box-shadow: 0 1px 2px @sm_shadow;
    color: @sm_text_secondary;
}
.empty-state .empty-icon { color: @sm_text_secondary; }
.empty-state .empty-title { font-size: ${fs_hero_title}em; font-weight: 400; color: @sm_text_primary; }
.empty-state .empty-description { color: @sm_text_secondary; font-size: ${fs_subtitle}em; }
.empty-state .empty-caption { color: @sm_text_faint; font-size: ${fs_caption}em; }
.sm-window button.hero { min-height: ${hero}px; padding: 0 ${xxl}px; font-size: ${fs_subtitle}em; }

/* ------------------------------------------------------------- cards */
.card {
    background-color: @sm_bg_surface;
    border: 1px solid @sm_border_subtle;
    border-radius: ${r_lg}px;
    padding: ${xxl}px;
}
.detail-key { font-size: ${fs_caps}em; font-weight: 500; letter-spacing: $caps_tracking; color: @sm_text_muted; }
.detail-value { font-size: ${fs_body}em; color: @sm_text_primary; }
.activity-row { padding: ${m}px 0; border-bottom: 1px solid @sm_border_subtle; }
.activity-time { color: @sm_text_faint; font-size: ${fs_caption}em; }

/* --------------------------------------------------------------- chips */
.chip {
    background-color: @sm_chip_bg;
    color: @sm_text_primary;
    border: 1px solid @sm_border_subtle;
    border-radius: ${r_pill}px;
    padding: 0 ${s}px;
    font-size: ${fs_caption}em;
}
.chip.accent { background-color: @sm_accent_tint; border-color: @sm_accent_soft_border; color: @sm_accent_text; }

/* -------------------------------------------------------- status dots */
.status-dot { color: @sm_text_disabled; }
.status-dot.connected { color: @sm_success; }
.status-dot.connecting { color: @sm_warning; }
.status-dot.failed { color: @sm_danger; }
.status-dot.unknown { color: @sm_text_disabled; }

/* ---------------------------------------------------------- workspace */
.session-strip {
    background-color: @sm_bg_canvas;
    padding: ${s}px ${l}px 0 ${l}px;
}
.session-toolbar { background-color: transparent; }
notebook.session-tabs { background-color: transparent; }
notebook.session-tabs > header {
    background-color: @sm_bg_canvas;
    border: none;
    box-shadow: none;
    padding: ${s}px ${l}px 0 ${l}px;
}
notebook.session-tabs > header > tabs > tab {
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: ${r_md}px;
    padding: ${xs}px ${m}px;
    margin-right: ${xs}px;
    color: @sm_text_secondary;
    box-shadow: none;
    min-height: 0;
}
notebook.session-tabs > header > tabs > tab:hover { background-color: @sm_bg_hover; color: @sm_text_primary; }
notebook.session-tabs > header > tabs > tab:checked {
    background-color: @sm_bg_surface;
    border-color: @sm_border_subtle;
    color: @sm_text_primary;
    box-shadow: none;
}
notebook.session-tabs > stack {
    background-color: @sm_bg_canvas;
    padding: ${l}px;
}
.terminal-card {
    border: 1px solid @sm_border_subtle;
    border-radius: ${r_lg}px;
    background-color: @sm_bg_surface;
}
.tab-activity { font-weight: bold; color: @sm_accent_text; }
.statusbar {
    background-color: transparent;
    color: @sm_text_secondary;
    padding: 2px ${s}px;
    font-size: ${fs_caption}em;
}
.statusbar .stat-warn { color: @sm_warning; font-weight: bold; }
.statusbar .stat-critical { color: @sm_danger; font-weight: bold; }
.broadcast-strip {
    background-color: @sm_broadcast_bg;
    color: @sm_broadcast_fg;
    padding: ${xs}px ${m}px;
    font-weight: 600;
    border-bottom: 1px solid alpha(@sm_danger, 0.3);
}
.broadcast-active { border: 2px solid @sm_danger; border-radius: ${r_md}px; }
.tile-frame {
    border: 1px solid @sm_border_subtle;
    border-radius: ${r_lg}px;
    background-color: @sm_bg_surface;
}
.tile-header { padding: ${xs}px ${s}px; border-bottom: 1px solid @sm_border_subtle; }

/* ------------------------------------------------------------- toasts */
.toast {
    background-color: @sm_bg_surface_raised;
    color: @sm_text_primary;
    border: 1px solid @sm_border_subtle;
    border-radius: ${r_lg}px;
    padding: ${s}px ${m}px;
    margin: ${m}px;
    box-shadow: 0 8px 24px @sm_shadow_popover;
}
.toast-info { border-left: 4px solid @sm_info; }
.toast-success { border-left: 4px solid @sm_success; }
.toast-warning { border-left: 4px solid @sm_warning; }
.toast-error { border-left: 4px solid @sm_danger; }
.toast-info:dir(rtl), .toast-success:dir(rtl), .toast-warning:dir(rtl), .toast-error:dir(rtl) {
    border-left-width: 1px;
    border-right-width: 4px;
}
.toast-info:dir(rtl) { border-right-color: @sm_info; }
.toast-success:dir(rtl) { border-right-color: @sm_success; }
.toast-warning:dir(rtl) { border-right-color: @sm_warning; }
.toast-error:dir(rtl) { border-right-color: @sm_danger; }

/* ------------------------------------------------------------ dialogs */
.sm-dialog headerbar, .sm-dialog .titlebar {
    background-image: none;
    background-color: @sm_bg_surface;
    border-bottom: 1px solid @sm_border_subtle;
    box-shadow: none;
}
.sm-dialog headerbar .title { font-size: ${fs_dialog_title}em; font-weight: 600; }
.sm-dialog .settings-sidebar { background-color: @sm_bg_canvas; border-right: 1px solid @sm_border_subtle; }
.sm-dialog .settings-sidebar row { padding: ${s}px ${m}px; border-radius: ${r_md}px; }
.sm-dialog .settings-sidebar row:selected { background-color: @sm_accent_tint; color: @sm_accent_text; }
.sm-dialog list, .sm-dialog treeview.view { background-color: @sm_bg_surface; }
.sm-dialog treeview.view:selected, .sm-dialog row:selected { background-color: @sm_accent_tint; color: @sm_accent_text; }
.sm-dialog .form-section { margin-bottom: ${l}px; }
.sm-dialog infobar box { background-image: none; }
.sm-window infobar.warning > revealer > box, .sm-dialog infobar.warning > revealer > box {
    background-color: alpha(@sm_warning, 0.12);
    color: @sm_text_primary;
    border-bottom: 1px solid alpha(@sm_warning, 0.4);
}
.sm-window infobar.error > revealer > box, .sm-dialog infobar.error > revealer > box {
    background-color: alpha(@sm_danger, 0.10);
    color: @sm_text_primary;
    border-bottom: 1px solid alpha(@sm_danger, 0.4);
}
.sm-window infobar.info > revealer > box, .sm-dialog infobar.info > revealer > box {
    background-color: @sm_accent_tint;
    color: @sm_text_primary;
}

$motion
""")

_MOTION_ENABLED = Template("""
/* ------------------------------------------------------------ motion */
button, .chip, .toast, notebook.session-tabs > header > tabs > tab {
    transition: background-color ${fast}ms $easing, color ${fast}ms $easing, border-color ${fast}ms $easing;
}
""")

_MOTION_DISABLED = """
/* ------------------------------------------------------------ motion */
/* gtk-enable-animations is off: no transitions. */
"""


def _defines(palette: Palette) -> str:
    return "\n".join(
        f"@define-color {TOKEN_PREFIX}{role} {value};" for role, value in palette.items()
    )


def _families(names) -> str:
    quoted = ", ".join(f'"{name}"' for name in names)
    return quoted


def _values(palette: Palette, motion: str) -> Dict[str, object]:
    return {
        "name": palette.name,
        "defines": _defines(palette),
        "motion": motion,
        "ui_fonts": _families(UI_FONT_FAMILIES) + ", sans-serif",
        "mono_fonts": _families(MONO_FONT_FAMILIES) + ", monospace",
        "caps_tracking": CAPS_TRACKING,
        "badge_tracking": BADGE_TRACKING,
        "xs": Space.XS, "s": Space.S, "m": Space.M, "l": Space.L, "xl": Space.XL,
        "xxl": Space.XXL, "xxxl": Space.XXXL, "gutter": Space.GUTTER,
        "r_sm": Radius.SMALL, "r_keycap": Radius.KEYCAP, "r_md": Radius.MEDIUM, "r_input": Radius.INPUT,
        "r_lg": Radius.LARGE, "r_card": Radius.CARD, "r_pill": Radius.PILL,
        "topbar": Size.TOPBAR, "page_bar": Size.PAGE_BAR, "host_header": Size.HOST_HEADER, "tabs": Size.TABS,
        "info_bar": Size.INFO_BAR, "sidebar_footer": Size.SIDEBAR_FOOTER, "logo": Size.LOGO_TILE,
        "avatar": Size.AVATAR, "icon_button": Size.ICON_BUTTON, "search": Size.SEARCH, "button": Size.BUTTON,
        "button_large": Size.BUTTON_LARGE, "hero": Size.HERO_BUTTON, "host_tile": Size.HOST_TILE,
        "empty_card": Size.EMPTY_CARD, "badge": Size.BADGE,
        "search_text_inset": Space.GUTTER + Space.S + 1,
        "fs_caps": FontScale.CAPS, "fs_caps_14": FontScale.MONO, "fs_badge": FontScale.BADGE,
        "fs_caption": FontScale.CAPTION, "fs_mono": FontScale.MONO, "fs_body": FontScale.BODY,
        "fs_tree": FontScale.TREE, "fs_subtitle": FontScale.SUBTITLE, "fs_wordmark": FontScale.WORDMARK,
        "fs_hero_title": FontScale.HERO_TITLE, "fs_host_title": FontScale.HOST_TITLE,
        "fs_page_title": FontScale.PAGE_TITLE, "fs_title": FontScale.TITLE,
        "fs_dialog_title": em(18),
    }


def build_css(palette: Palette, *, animations: bool = True) -> str:
    """Return the complete stylesheet for ``palette``."""

    motion = (
        _MOTION_ENABLED.substitute(fast=Motion.FAST, easing=Motion.EASING) if animations else _MOTION_DISABLED
    )
    return _TEMPLATE.substitute(_values(palette, motion))


_COLOR_REF = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")
_DEFINE = re.compile(r"@define-color\s+([A-Za-z_][A-Za-z0-9_]*)\s")


def defined_colors(css: str) -> Set[str]:
    return set(_DEFINE.findall(css))


def unresolved_color_refs(css: str) -> Set[str]:
    """Return ``@name`` references that are not defined in ``css`` itself."""

    defined = defined_colors(css)
    refs = {name for name in _COLOR_REF.findall(css) if name != "define"}
    return refs - defined
