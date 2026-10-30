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
from typing import Dict, Optional, Set

from .density import MIN_FONT_PX, DensityTokens
from .density import active as active_density
from .tokens import (
    ENV_BADGES,
    MONO_FONT_FAMILIES,
    UI_FONT_FAMILIES,
    em,
    Motion,
    Palette,
)

DEFAULT_FONT_PT = 11

TOKEN_PREFIX = "sm_"

_TEMPLATE = Template(r"""
/* ===================================================================
 * SSH Manager stylesheet ($name theme, $density density) - generated from
 * tokens.py and density.py.
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

/* ---------------------------------------------------- stock surfaces */
/* GTK themes fill these containers with theme_base_color / theme_bg_color.
   Each gets an explicit token and no background-image, in every state, so
   neither the system theme nor :backdrop can show through. Never rely on
   "transparent" over a stock container. Region rules further down are more
   specific (or come later) and override these defaults. */
.sm-window list, .sm-dialog list,
.sm-window iconview, .sm-dialog iconview,
.sm-window textview, .sm-dialog textview,
.sm-window textview text, .sm-dialog textview text,
.sm-window list:backdrop, .sm-dialog list:backdrop,
.sm-window iconview:backdrop, .sm-dialog iconview:backdrop,
.sm-window textview:backdrop, .sm-dialog textview:backdrop,
.sm-window textview text:backdrop, .sm-dialog textview text:backdrop,
.sm-window notebook > stack, .sm-dialog notebook > stack,
.sm-window notebook > header, .sm-dialog notebook > header,
.sm-window searchbar > revealer > box, .sm-dialog searchbar > revealer > box,
.sm-window actionbar > revealer > box, .sm-dialog actionbar > revealer > box {
    background-color: @sm_bg_surface;
    background-image: none;
}
.sm-window textview text:disabled, .sm-dialog textview text:disabled { background-color: @sm_bg_canvas; }
.sm-window popover list, .sm-dialog popover list,
.sm-window popover viewport, .sm-dialog popover viewport,
.sm-window popover list:backdrop, .sm-dialog popover list:backdrop,
popover.sm-popover list, popover.sm-popover list:backdrop {
    background-color: @sm_bg_surface_raised;
    background-image: none;
}
.sm-window searchbar > revealer > box, .sm-dialog searchbar > revealer > box {
    border-color: @sm_border_subtle;
    box-shadow: none;
}

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
    min-height: ${button_min}px;
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
.sm-window button.toolbar-icon.broadcast-toggle:checked {
    background-color: @sm_broadcast;
    color: @sm_broadcast_bg;
}
button.toolbar-icon {
    padding: ${xs}px;
    min-width: ${toolbar_button}px;
    min-height: ${toolbar_button}px;
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
    min-height: ${entry}px;
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
    min-height: ${menu_row}px;
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
    min-height: ${menu_row}px;
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
    min-height: ${menu_row}px;
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
/* Every node between the pane and the rows gets the surface token explicitly.
   GTK themes fill scrolledwindow, viewport and treeview.view with
   theme_base_color, including the treeview.view.cell nodes TreeView pushes
   (style_context.save()) to paint each row and the empty area below the last
   row, so "transparent" on the widget's own node is not enough. */
.sidebar-pane,
.sidebar-pane stack,
.sidebar-pane scrolledwindow,
.sidebar-pane scrolledwindow viewport,
.sidebar-pane .empty-state,
.sidebar-pane treeview.view,
.sidebar-pane treeview.view.cell,
.sidebar-pane treeview.view:backdrop,
.sidebar-pane treeview.view:disabled,
.sidebar-pane treeview.view:focus,
.sidebar-pane treeview.view:hover,
.sidebar-pane treeview.view:selected,
.sidebar-pane treeview.view:selected:focus,
.sidebar-pane treeview.view:selected:backdrop,
.sidebar-pane treeview.view:hover:backdrop,
.sidebar-pane treeview.view.even,
.sidebar-pane treeview.view.odd {
    background-color: @sm_bg_surface;
    background-image: none;
    box-shadow: none;
}
.sidebar-pane scrolledwindow, .sidebar-pane scrolledwindow viewport { border: none; }
.sidebar-pane scrolledwindow junction {
    background-color: @sm_bg_surface;
    background-image: none;
    border: none;
}
/* Edge indicators are painted over the rows: no theme gradient, never opaque. */
.sidebar-pane scrolledwindow undershoot, .sidebar-pane scrolledwindow overshoot {
    background-color: transparent;
    background-image: none;
    border: none;
    box-shadow: none;
}
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
.sidebar-title {
    font-size: ${fs_page_title}em;
    font-weight: ${page_title_weight};
    color: @sm_text_primary;
    margin-bottom: -${title_slack}px;
}
.sm-window button.add-host { min-height: ${button_inner}px; padding: 0 ${add_host_pad_end}px 0 ${add_host_pad_start}px; font-size: ${fs_body}em; }
.sm-window entry.sidebar-search-entry {
    min-height: ${search_inner}px;
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
    min-width: ${filter_button}px;
    min-height: ${search_inner}px;
    padding: 0;
    border-radius: ${r_md}px;
    background-color: @sm_bg_surface;
    color: @sm_text_secondary;
}
.filter-dot { color: @sm_accent; }
/* "scrolledwindow >" pins the font size to the widget's own node: the nodes
   TreeView pushes with style_context.save() while drawing would otherwise
   match again and apply the em scale twice. */
.sidebar-pane scrolledwindow > treeview.view {
    color: @sm_text_primary;
    font-size: ${fs_tree}em;
}
/* Selected and hovered rows keep the surface behind them: the row renderer
   paints the inset rounded highlight and the accent bar itself. */
.sidebar-pane treeview.view:selected,
.sidebar-pane treeview.view:selected:focus,
.sidebar-pane treeview.view:hover {
    color: @sm_text_primary;
}
.sidebar-pane treeview.view.expander { color: transparent; }
.sidebar-pane treeview.view, .sidebar-pane treeview.view:focus {
    outline-width: 0;
    -gtk-outline-radius: 0;
}
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
.sm-window button.page-more { min-width: ${button}px; min-height: ${button_large_inner}px; padding: 0; color: @sm_text_muted; }
.sm-window button.page-primary { min-height: ${button_large_inner}px; min-width: ${primary_min_width}px; padding: 0 ${l}px; font-size: ${fs_body}em; }
.host-header {
    background-color: @sm_bg_surface;
    border-bottom: 1px solid @sm_border_subtle;
    min-height: ${host_header}px;
    padding: 0 ${gutter}px;
}
.host-tile {
    background-color: @sm_accent_tint;
    color: @sm_icon_default;
    border-radius: ${r_lg}px;
    min-width: ${host_tile}px;
    min-height: ${host_tile}px;
}
.host-title { font-size: ${fs_host_title}em; font-weight: ${host_title_weight}; color: @sm_text_primary; }
.host-address { font-family: $mono_fonts; font-size: ${fs_mono}em; color: @sm_text_secondary; }
.env-badge {
    border-radius: ${r_pill}px;
    padding: 0 ${s}px;
    min-height: ${badge}px;
    font-size: ${fs_badge}em;
    font-weight: 700;
    letter-spacing: $badge_tracking;
}
$env_badges
.latency-dot.good, .latency-good { color: @sm_success; }
.latency-dot.warn, .latency-warn { color: @sm_warning; }
.latency-dot.bad, .latency-bad { color: @sm_danger; }
.host-tile image, .host-tile { color: @sm_icon_default; }
.sm-window button.info-copy { color: @sm_text_muted; }
.stat-label { font-size: ${fs_caps}em; font-weight: 500; letter-spacing: $caps_tracking; color: @sm_text_muted; }
.stat-value { font-size: ${fs_body}em; font-weight: 600; color: @sm_text_primary; }
.stat-good { color: @sm_success; }
.stat-warn { color: @sm_warning; font-weight: 600; }
.stat-critical, .stat-bad { color: @sm_danger; font-weight: 600; }

.sm-window button.link-button {
    background: transparent;
    border-color: transparent;
    color: @sm_accent_text;
    font-weight: 500;
}
.sm-window button.link-button:hover { background-color: @sm_bg_hover; }

/* Host details / activity slide-over (an overlay: never resizes the terminal). */
.host-panel {
    background-color: @sm_bg_surface_raised;
    border-left: 1px solid @sm_border_subtle;
    box-shadow: -8px 0 24px @sm_shadow_popover;
}
.host-panel stack, .host-panel scrolledwindow, .host-panel scrolledwindow viewport, .host-panel .panel-body {
    background-color: @sm_bg_surface_raised;
    background-image: none;
    border: none;
}
.host-panel-header { padding: ${s}px ${s}px ${s}px ${l}px; border-bottom: 1px solid @sm_border_subtle; }
.host-panel-switcher { padding: ${s}px ${l}px 0 ${l}px; }
.sm-window button.panel-tab {
    background-color: @sm_bg_surface_raised;
    border-color: transparent;
    color: @sm_tab_inactive;
    padding: ${xs}px ${m}px;
}
.sm-window button.panel-tab:hover { background-color: @sm_bg_hover; color: @sm_text_primary; }
.sm-window button.panel-tab:checked { background-color: @sm_accent_tint; color: @sm_accent_text; border-color: @sm_accent_soft_border; }
.host-panel-actions { padding: ${s}px ${l}px; border-top: 1px solid @sm_border_subtle; }

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
    min-width: ${empty_card_inner}px;
    min-height: ${empty_card_inner}px;
    box-shadow: 0 1px 2px @sm_shadow;
    color: @sm_text_secondary;
}
.empty-state .empty-icon { color: @sm_icon_default; }
.empty-state .empty-title { font-size: ${fs_hero_title}em; font-weight: 400; color: @sm_text_primary; }
.empty-state .empty-description { color: @sm_text_secondary; font-size: ${fs_subtitle}em; }
.empty-state .empty-caption { color: @sm_text_faint; font-size: ${fs_caption}em; }
.sm-window button.hero { min-height: ${hero_inner}px; padding: 0 ${xxl}px; font-size: ${fs_subtitle}em; }

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
/* Session strip: session tabs, + after the last one, the toolbar at the end. */
.session-strip {
    background-color: @sm_bg_surface;
    border-bottom: 1px solid @sm_border_subtle;
    min-height: ${session_strip_inner}px;
    padding: 0 ${s}px 0 ${gutter}px;
}
.session-strip scrolledwindow, .session-strip scrolledwindow viewport, .session-tabs-row {
    background-color: @sm_bg_surface;
    background-image: none;
    border: none;
}
.session-tab-box {
    padding: 0 ${s}px;
    margin-right: ${xs}px;
    border-bottom: 2px solid @sm_bg_surface;
    color: @sm_tab_inactive;
    font-weight: 500;
    font-size: ${fs_body}em;
}
.session-tab-box.hover { background-color: @sm_bg_hover; color: @sm_text_primary; border-bottom-color: @sm_bg_hover; }
.session-tab-box.current { color: @sm_accent_text; border-bottom-color: @sm_accent; }
.session-tab:focus > .session-tab-box { box-shadow: inset 0 0 0 2px alpha(@sm_focus_ring, 0.8); }
.session-tab-box button.toolbar-icon { margin: 0; min-width: 16px; min-height: 16px; padding: 2px; }
.session-toolbar { background-color: @sm_bg_surface; }
notebook.session-tabs { background-color: @sm_bg_canvas; }
notebook.session-tabs > stack {
    background-color: @sm_bg_canvas;
    padding: ${xs}px ${l}px ${l}px ${l}px;
}
.terminal-card {
    border: 1px solid @sm_border_subtle;
    border-radius: ${r_lg}px;
    background-color: @sm_bg_surface;
    padding: ${s}px;
}
.session-toolbar-row {
    background-color: @sm_bg_surface;
    border-bottom: 1px solid @sm_border_subtle;
    padding: 0 ${s}px 0 ${gutter}px;
}
.session-strip-actions { padding-right: ${xs}px; }
.info-live .statusbar { padding: 0; font-size: ${fs_mono}em; }
.tab-activity { font-weight: bold; color: @sm_accent_text; }
.macro-recording-dot, .macro-recording-status { color: @sm_danger; font-weight: bold; }
.sm-window button.macro-recording { color: @sm_danger; }
.statusbar {
    background-color: transparent;
    color: @sm_text_secondary;
    padding: 2px ${s}px;
    font-size: ${fs_caption}em;
    min-height: ${icon_small}px; /* one height whatever it shows: the terminal above never resizes */
}
.statusbar spinner { min-width: ${icon_small}px; min-height: ${icon_small}px; }
.statusbar .stat-warn { color: @sm_warning; font-weight: bold; }
.statusbar .stat-critical { color: @sm_danger; font-weight: bold; }
.broadcast-strip {
    background-color: @sm_broadcast_bg;
    color: @sm_broadcast_fg;
    padding: ${xs}px ${m}px;
    font-weight: 600;
    border-bottom: 1px solid alpha(@sm_broadcast, 0.35);
}
/* Broadcast is violet, never the red of errors (its Stop button is not destructive either). */
.broadcast-strip button.destructive-action {
    background-color: @sm_broadcast;
    background-image: none;
    border-color: @sm_broadcast;
    color: @sm_broadcast_bg;
}
.broadcast-active { border: 2px solid @sm_pane_border_broadcast; border-radius: ${r_md}px; }

/* Panes shown together (split pane, tile mode): border on every side, a canvas
   gutter between panes, a header and a footer per pane. A plain pane has a 1px
   border + 1px padding over the gutter color; focused/broadcast panes use a 2px
   border and no padding, so the content never moves. */
.pane-gutter, .pane-gutter viewport, .terminal-card.split {
    background-color: @sm_pane_gutter;
    background-image: none;
}
.terminal-card.split { padding: ${pane_gutter}px; border: none; border-radius: 0; }
.pane-frame {
    background-color: @sm_pane_gutter;
    background-image: none;
    border: 1px solid @sm_pane_border;
    border-radius: ${pane_radius}px;
    padding: 1px;
}
.pane-frame.disconnected { border-color: @sm_warning; }
.pane-frame.failed { border-color: @sm_danger; }
.pane-frame.focused { border: 2px solid @sm_pane_border_focus; padding: 0; }
.pane-frame.broadcast { border: 2px solid @sm_pane_border_broadcast; padding: 0; }
/* Focused and broadcasting: broadcast border with an accent inner ring (the padding). */
.pane-frame.broadcast.focused {
    border: 1px solid @sm_pane_border_broadcast;
    padding: 1px;
    background-color: @sm_pane_border_focus;
}
.pane-frame .terminal-card { border: none; border-radius: 0; }
.pane-body vte-terminal { padding: ${terminal_pad_y}px ${terminal_pad_x}px; }
.pane-header {
    background-color: @sm_pane_header_bg;
    background-image: none;
    min-height: ${pane_header}px;
    padding: 0 ${xs}px 0 ${s}px;
    border-bottom: 1px solid @sm_border_subtle;
    border-radius: ${pane_inner_radius}px ${pane_inner_radius}px 0 0;
    color: @sm_text_primary;
}
.pane-header.hover { background-color: @sm_bg_hover; }
.pane-header.focused { background-color: @sm_pane_header_bg_focus; }
.pane-header.focused .pane-title { color: @sm_accent_text; }
.pane-title { font-weight: 600; font-size: ${fs_body}em; }
.pane-address { font-family: $mono_fonts; font-size: ${fs_caption}em; color: @sm_text_muted; }
.pane-badge {
    background-color: alpha(@sm_broadcast, 0.12);
    color: @sm_broadcast;
    border-radius: ${r_pill}px;
    padding: 0 ${s}px;
    font-size: ${fs_badge}em;
    font-weight: 700;
    letter-spacing: $badge_tracking;
}
.pane-header button.toolbar-icon { min-width: 18px; min-height: 18px; padding: 2px; }
.sm-window button.broadcast-chip {
    min-height: 0;
    padding: 0 ${s}px;
    border-radius: ${r_pill}px;
    background-color: @sm_pane_header_bg;
    border-color: @sm_border_input;
    color: @sm_text_secondary;
    font-size: ${fs_caption}em;
    font-weight: 500;
}
.sm-window button.broadcast-chip:hover { background-color: @sm_bg_hover; }
/* Checked = included in broadcast; it turns violet only while broadcast is on. */
.sm-window button.broadcast-chip:checked { background-color: @sm_bg_hover; color: @sm_text_primary; }
.sm-window .pane-frame.broadcasting button.broadcast-chip:checked {
    background-color: alpha(@sm_broadcast, 0.14);
    border-color: alpha(@sm_broadcast, 0.5);
    color: @sm_broadcast;
}
.pane-footer {
    background-color: @sm_pane_header_bg;
    min-height: ${pane_footer}px;
    padding: 0 ${s}px;
    border-top: 1px solid @sm_border_subtle;
    border-radius: 0 0 ${pane_inner_radius}px ${pane_inner_radius}px;
    color: @sm_text_secondary;
    font-size: ${fs_caption}em;
}
.pane-dim { background-color: @sm_pane_dim; }
/* Split divider: a gutter-wide handle with a centred 2 x 24 grip (the only
   background-image on a surface: it draws the grip). */
paned.pane-split > separator, paned.pane-split > separator.wide {
    min-width: ${pane_gutter}px;
    min-height: ${pane_gutter}px;
    margin: 0;
    padding: 0;
    border: none;
    -gtk-icon-source: none;
    background-color: @sm_pane_gutter;
    background-image: linear-gradient(@sm_border_strong, @sm_border_strong);
    background-size: 2px 24px;
    background-repeat: no-repeat;
    background-position: center;
}
paned.pane-split.vertical > separator { background-size: 24px 2px; }
paned.pane-split > separator:hover, paned.pane-split > separator:active {
    background-image: linear-gradient(@sm_accent, @sm_accent);
}

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

$density_rules

$motion
""")

_MOTION_ENABLED = Template("""
/* ------------------------------------------------------------ motion */
button, .chip, .toast, .session-tab-box {
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


def _env_badges(palette: Palette) -> str:
    rules = []
    for kind, (background, foreground) in ENV_BADGES["dark" if palette.name == "dark" else "light"].items():
        rules.append(f".env-badge.env-{kind} {{ background-color: {background}; color: {foreground}; }}")
    return "\n".join(rules)


_COMPACT_RULES = Template("""
/* ------------------------------------------------- compact density */
/* Sidebar: one header row ("Hosts" + count, Add host), no eyebrow. */
.sidebar-header { min-height: ${sidebar_header}px; }
.host-count {
    background-color: @sm_chip_bg;
    color: @sm_text_secondary;
    border-radius: ${r_pill}px;
    padding: 0 6px;
    font-size: ${fs_caps}em;
    font-weight: 500;
}
.sidebar-footer { padding: 0 ${s}px; }
.sm-window button.footer-link { padding: ${xs}px ${s}px; }

/* Host bar (row A): page bar + host header merged into one line. */
.host-header.host-bar { padding: 0 ${gutter}px; }
.host-header.host-bar .host-address { color: @sm_text_muted; }
.host-stat-inline { font-size: ${fs_caption}em; color: @sm_text_secondary; }
.crumb-prefix, .sm-window button.breadcrumb-link.crumb-prefix { font-size: ${fs_caption}em; color: @sm_text_muted; }
.sm-window button.breadcrumb-link { padding: 0 ${xs}px; min-height: 0; }
.session-toolbar-row { min-height: ${session_strip}px; }
button.toolbar-icon { padding: ${xs}px; }
.info-bar button.toolbar-icon { min-width: 20px; min-height: 20px; padding: 1px; }

/* The terminal keeps the app font size, not the density's smaller base font
   (a custom terminal font from Settings sets its own size anyway). Absolute,
   so nested terminals (split panes, tiles) cannot compound it. */
vte-terminal { font-size: ${terminal_font}pt; }

/* Terminal edge to edge: no card, no canvas margin. */
notebook.session-tabs > stack { padding: 0; }
.terminal-card {
    border: none;
    border-radius: 0;
    padding: ${terminal_pad_y}px ${terminal_pad_x}px;
}

/* Single-line status bar. */
.info-bar { padding: 0 ${gutter}px; }
.status-field { font-size: ${fs_caption}em; color: @sm_text_muted; }
.status-field.mono { font-family: $mono_fonts; }
.status-sep { font-size: ${fs_caption}em; color: @sm_text_faint; }
.info-live .statusbar { font-size: ${fs_caption}em; }

.card { padding: ${l}px; }
.broadcast-strip { padding: ${xs}px ${m}px; }
.broadcast-strip button { min-height: 20px; padding: 0 ${m}px; }
.sm-window button.workspace-switcher { padding: ${xs}px ${xs}px; }
""")


def _density_rules(tokens: DensityTokens, values: Dict[str, object]) -> str:
    """Rules that only exist in a density (empty for Comfortable, the reference)."""

    if not tokens.compact:
        return ""
    extra = dict(values)
    size = tokens.size
    extra.update(
        sidebar_header=size.SIDEBAR_HEADER, session_strip=size.SESSION_STRIP - 1, terminal_pad_x=size.TERMINAL_PAD_X, terminal_pad_y=size.TERMINAL_PAD_Y,
        terminal_font=values["font_pt"],
    )
    return _COMPACT_RULES.substitute(extra)


def font_floor_em(font_pt: float, base_font_scale: float = 1.0) -> float:
    """Smallest ``em`` that still renders :data:`MIN_FONT_PX` at an app font of ``font_pt``.

    The window's base font is ``font_pt × base_font_scale`` (see ``DensityTokens``).
    """

    return MIN_FONT_PX / (float(font_pt) * base_font_scale * 96 / 72)


def _values(palette: Palette, motion: str, tokens: DensityTokens, font_pt: float) -> Dict[str, object]:
    space, radius, size, font = tokens.space, tokens.radius, tokens.size, tokens.font
    scale = tokens.base_font_scale
    floor = font_floor_em(font_pt, scale)

    def fs(px: float) -> float:
        # Type scales with the app font size (em of the density's base font)
        # but never renders below the floor.
        return max(em(px / scale), round(floor + 0.0005, 3))

    return {
        "env_badges": _env_badges(palette),
        "name": palette.name,
        "density": tokens.density.value,
        "font_pt": font_pt,
        "defines": _defines(palette),
        "motion": motion,
        "ui_fonts": _families(UI_FONT_FAMILIES) + ", sans-serif",
        "mono_fonts": _families(MONO_FONT_FAMILIES) + ", monospace",
        "caps_tracking": tokens.caps_tracking,
        "badge_tracking": tokens.badge_tracking,
        "xs": space.XS, "s": space.S, "m": space.M, "l": space.L, "xl": space.XL,
        "xxl": space.XXL, "xxxl": space.XXXL, "gutter": space.GUTTER,
        "r_sm": radius.SMALL, "r_keycap": radius.KEYCAP, "r_md": radius.MEDIUM, "r_input": radius.INPUT,
        "r_lg": radius.LARGE, "r_card": radius.CARD, "r_pill": radius.PILL,
        "topbar": size.TOPBAR, "page_bar": size.PAGE_BAR, "host_header": size.HOST_HEADER,
        "info_bar": size.INFO_BAR, "sidebar_footer": size.SIDEBAR_FOOTER, "logo": size.LOGO_TILE,
        "avatar": size.AVATAR, "icon_button": size.ICON_BUTTON, "search": size.SEARCH, "button": size.BUTTON,
        "button_large": size.BUTTON_LARGE, "hero": size.HERO_BUTTON, "host_tile": size.HOST_TILE,
        "empty_card": size.EMPTY_CARD, "badge": size.BADGE,
        "button_min": size.BUTTON_MIN, "toolbar_button": size.TOOLBAR_BUTTON, "entry": size.ENTRY,
        "menu_row": size.MENU_ROW, "filter_button": size.FILTER_BUTTON, "icon_small": size.ICON_SMALL,
        "primary_min_width": size.PRIMARY_MIN_WIDTH,
        "add_host_pad_start": tokens.sidebar.ADD_HOST_PAD_START, "add_host_pad_end": tokens.sidebar.ADD_HOST_PAD_END,
        "search_text_inset": tokens.sidebar.SEARCH_TEXT_INSET,
        "session_strip_inner": size.SESSION_STRIP - 1,  # + 1px bottom border
        "pane_gutter": size.PANE_GUTTER, "pane_radius": size.PANE_RADIUS,
        "pane_inner_radius": max(0, size.PANE_RADIUS - 2),
        "pane_header": size.PANE_HEADER - 1, "pane_footer": size.PANE_FOOTER - 1,  # + 1px border
        "terminal_pad_x": size.TERMINAL_PAD_X, "terminal_pad_y": size.TERMINAL_PAD_Y,
        # GTK's min-height excludes the 1px borders: tokens are outer sizes.
        "button_inner": size.BUTTON - 2, "button_large_inner": size.BUTTON_LARGE - 2,
        "search_inner": size.SEARCH - 2, "hero_inner": size.HERO_BUTTON - 2,
        "empty_card_inner": size.EMPTY_CARD - 2,
        "title_slack": tokens.sidebar.TITLE_SLACK,
        "page_title_weight": font.PAGE_TITLE_WEIGHT, "host_title_weight": font.HOST_TITLE_WEIGHT,
        "fs_caps": fs(font.CAPS), "fs_caps_14": fs(font.MONO), "fs_badge": fs(font.BADGE),
        "fs_caption": fs(font.CAPTION), "fs_mono": fs(font.MONO), "fs_body": fs(font.BODY),
        "fs_tree": fs(font.TREE), "fs_subtitle": fs(font.SUBTITLE), "fs_wordmark": fs(font.WORDMARK),
        "fs_hero_title": fs(font.HERO_TITLE), "fs_host_title": fs(font.HOST_TITLE),
        "fs_page_title": fs(font.PAGE_TITLE), "fs_title": fs(font.TITLE),
        "fs_dialog_title": fs(font.DIALOG_TITLE),
    }


def build_css(
    palette: Palette,
    *,
    animations: bool = True,
    tokens: Optional[DensityTokens] = None,
    font_pt: float = DEFAULT_FONT_PT,
) -> str:
    """Return the complete stylesheet for ``palette`` and a density (default: the active one).

    ``font_pt`` is the application font size: type is relative to it (``em``)
    and floored at :data:`MIN_FONT_PX`.
    """

    motion = (
        _MOTION_ENABLED.substitute(fast=Motion.FAST, easing=Motion.EASING) if animations else _MOTION_DISABLED
    )
    tokens = tokens or active_density()
    values = _values(palette, motion, tokens, font_pt)
    values["density_rules"] = _density_rules(tokens, values)
    return _TEMPLATE.substitute(values)


_COLOR_REF = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")
_DEFINE = re.compile(r"@define-color\s+([A-Za-z_][A-Za-z0-9_]*)\s")


def defined_colors(css: str) -> Set[str]:
    return set(_DEFINE.findall(css))


def unresolved_color_refs(css: str) -> Set[str]:
    """Return ``@name`` references that are not defined in ``css`` itself."""

    defined = defined_colors(css)
    refs = {name for name in _COLOR_REF.findall(css) if name != "define"}
    return refs - defined
