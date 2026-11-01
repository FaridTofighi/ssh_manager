"""Install the application stylesheet and keep it in sync with preferences.

:class:`ThemeManager` owns the single ``Gtk.CssProvider`` of the app. It
resolves the effective variant from the user's theme mode (``system``,
``light`` or ``dark``), loads the matching stylesheet, mirrors the choice to
``gtk-application-prefer-dark-theme`` so stock widgets follow, and reloads when
the desktop color scheme or ``gtk-enable-animations`` change.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Callable, List, Optional

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # type: ignore  # noqa: E402

from . import css_dark, css_light  # noqa: E402
from . import density as density_tokens  # noqa: E402
from .tokens import Palette, enum_value, palette_for  # noqa: E402

LOGGER = logging.getLogger(__name__)

_INTERFACE_SCHEMA = "org.gnome.desktop.interface"


class ThemeMode(str, Enum):
    SYSTEM = "system"
    LIGHT = "light"
    DARK = "dark"

    @classmethod
    def coerce(cls, value: object, default: "ThemeMode" = None) -> "ThemeMode":  # type: ignore[assignment]
        try:
            return cls(enum_value(value))
        except ValueError:
            return default or cls.SYSTEM

    @classmethod
    def from_prefer_dark(cls, prefer_dark: object) -> "ThemeMode":
        """Map the legacy ``prefer_dark`` UI-state flag to a mode."""

        if prefer_dark is None:
            return cls.SYSTEM
        return cls.DARK if bool(prefer_dark) else cls.LIGHT


def _interface_settings() -> Optional[Gio.Settings]:
    source = Gio.SettingsSchemaSource.get_default()
    if source is None:
        return None
    schema = source.lookup(_INTERFACE_SCHEMA, True)
    if schema is None or not schema.has_key("color-scheme"):
        return None
    try:
        return Gio.Settings.new(_INTERFACE_SCHEMA)
    except Exception:  # pragma: no cover - environment specific
        return None


class ThemeManager:
    """Load and switch the application stylesheet at runtime."""

    def __init__(self) -> None:
        self._provider: Optional[Gtk.CssProvider] = None
        self._mode = ThemeMode.SYSTEM
        self._dark = False
        self._listeners: List[Callable[[bool], None]] = []
        self._font_pt = 11
        self._loaded_css = ""
        self._settings = Gtk.Settings.get_default()
        self._initial_prefer_dark = bool(
            self._settings.get_property("gtk-application-prefer-dark-theme")
        ) if self._settings else False
        self._interface = _interface_settings()
        self._signal_ids: List[tuple] = []
        if self._settings is not None:
            self._signal_ids.append(
                (self._settings, self._settings.connect("notify::gtk-enable-animations", self._on_external_change))
            )
            self._signal_ids.append(
                (self._settings, self._settings.connect("notify::gtk-theme-name", self._on_external_change))
            )
        if self._interface is not None:
            self._signal_ids.append(
                (self._interface, self._interface.connect("changed::color-scheme", self._on_external_change))
            )

    # ---------------------------------------------------------- properties
    @property
    def mode(self) -> ThemeMode:
        return self._mode

    @property
    def is_dark(self) -> bool:
        return self._dark

    @property
    def palette(self) -> Palette:
        return palette_for(self._dark)

    @property
    def animations_enabled(self) -> bool:
        if self._settings is None:
            return True
        return bool(self._settings.get_property("gtk-enable-animations"))

    def motion_ms(self, duration: int) -> int:
        """Return ``duration`` or 0 when the user disabled animations."""

        return duration if self.animations_enabled else 0

    # ------------------------------------------------------------- control
    def add_listener(self, callback: Callable[[bool], None]) -> None:
        """Call ``callback(is_dark)`` after every theme (re)load."""

        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[bool], None]) -> None:
        if callback in self._listeners:
            self._listeners.remove(callback)

    def system_prefers_dark(self) -> bool:
        if self._interface is not None:
            try:
                return self._interface.get_string("color-scheme") == "prefer-dark"
            except Exception:  # pragma: no cover
                pass
        if self._settings is not None:
            theme = str(self._settings.get_property("gtk-theme-name") or "")
            if theme.lower().endswith("-dark"):
                return True
        return self._initial_prefer_dark

    def resolve_dark(self, mode: ThemeMode) -> bool:
        if mode is ThemeMode.DARK:
            return True
        if mode is ThemeMode.LIGHT:
            return False
        return self.system_prefers_dark()

    def apply(self, mode: Optional[ThemeMode] = None) -> bool:
        """Load the stylesheet for ``mode`` (or the current mode). Returns ``is_dark``."""

        if mode is not None:
            self._mode = ThemeMode.coerce(mode)
        self._dark = self.resolve_dark(self._mode)
        if self._settings is not None:
            self._settings.set_property("gtk-application-prefer-dark-theme", self._dark)
        self._load_provider()
        for callback in list(self._listeners):
            try:
                callback(self._dark)
            except Exception as exc:  # pragma: no cover - listener bugs must not break theming
                LOGGER.warning("theme listener failed: %s", exc)
        return self._dark

    def stylesheet(self) -> str:
        module = css_dark if self._dark else css_light
        return module.css(animations=self.animations_enabled, tokens=density_tokens.active(), font_pt=self._font_pt)

    @property
    def font_pt(self) -> int:
        return self._font_pt

    def set_ui_font_size(self, points: int) -> None:
        """App font size: type is ``em``-relative to it and floored at 11px, so the CSS depends on it."""

        points = max(1, int(points))
        if points == self._font_pt:
            return
        self._font_pt = points
        if self._provider is not None:
            self._load_provider()

    def reload(self) -> None:
        """Regenerate the stylesheet (density switch) without changing the theme."""

        if self._provider is not None:
            self._load_provider()

    def shutdown(self) -> None:
        for obj, handler in self._signal_ids:
            try:
                obj.disconnect(handler)
            except Exception:
                pass
        self._signal_ids = []
        self._remove_provider()

    # ------------------------------------------------------------ internal
    def _remove_provider(self) -> None:
        screen = Gdk.Screen.get_default()
        if screen is not None and self._provider is not None:
            try:
                Gtk.StyleContext.remove_provider_for_screen(screen, self._provider)
            except Exception:
                pass
        self._provider = None

    def _load_provider(self) -> None:
        screen = Gdk.Screen.get_default()
        if screen is None:
            return
        css = self.stylesheet()
        if self._provider is not None and css == self._loaded_css:
            return  # unchanged: swapping providers would restyle every widget for nothing
        provider = Gtk.CssProvider()
        try:
            provider.load_from_data(css.encode("utf-8"))
        except GLib.Error as exc:
            LOGGER.warning("stylesheet failed to load: %s", exc)
            return
        self._remove_provider()
        Gtk.StyleContext.add_provider_for_screen(
            screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
        self._provider = provider
        self._loaded_css = css

    def _on_external_change(self, *_args) -> None:
        GLib.idle_add(self._reapply_idle)

    def _reapply_idle(self) -> bool:
        self.apply()
        return GLib.SOURCE_REMOVE


_MANAGER: Optional[ThemeManager] = None


def get_theme_manager() -> ThemeManager:
    """Return the process-wide :class:`ThemeManager`."""

    global _MANAGER
    if _MANAGER is None:
        _MANAGER = ThemeManager()
    return _MANAGER


def current_palette() -> Palette:
    """Palette of the active theme (light palette before the theme is applied)."""

    return get_theme_manager().palette if _MANAGER is not None else palette_for(False)
